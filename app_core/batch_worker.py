import logging
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from app_core.context_rules import obter_contexto_voz
from app_core.filesystem import localizar_arquivo_equivalente_por_idioma, relpath_display
from app_core.glossary import obter_glossario_completo, verificar_termos_faltantes
from app_core.output_packaging import gerar_zip_da_saida, salvar_xml_traduzido
from app_core.run_state import load_run_state, save_run_state
from app_core.settings import criar_client, obter_api_key, obter_model_name
from app_core.text_sanitize import strip_bidi_controls
from app_core.translator import TranslationAPIError, processar_lote_entrada, normalizar_traducao_feminina
from app_core.validation import precisa_auditoria, validar_traducao_com_es
from translation_progress import ProgressManager

logger = logging.getLogger(__name__)


class TranslationWorker:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._status = {
            "running": False,
            "state": "idle",
            "error": "",
            "total_files": 0,
            "completed_files": 0,
            "queue_index": 0,
            "current_file": "",
            "current_relpath": "",
            "entry_total": 0,
            "entry_idx": 0,
            "chunk_start": 0,
            "chunk_end": 0,
            "started_at": 0.0,
            "updated_at": 0.0,
            "stop_requested": False,
        }

    def get_status(self):
        with self._lock:
            return dict(self._status)

    def is_running(self):
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            return bool(self._status.get("running")) and alive

    def request_stop(self):
        self._stop_event.set()
        self._set_status(stop_requested=True, state="stopping")
        self._write_worker_state()

    def start(self, config: dict):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "Worker ja esta em execucao."
            queue = list(config.get("queue") or [])
            if not queue:
                return False, "Fila vazia."
            self._stop_event.clear()
            now = time.time()
            self._status.update(
                {
                    "running": True,
                    "state": "running",
                    "error": "",
                    "total_files": len(queue),
                    "completed_files": 0,
                    "queue_index": 0,
                    "current_file": "",
                    "current_relpath": "",
                    "entry_total": 0,
                    "entry_idx": 0,
                    "chunk_start": 0,
                    "chunk_end": 0,
                    "started_at": now,
                    "updated_at": now,
                    "stop_requested": False,
                }
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(dict(config),),
                name="translation-batch-worker",
                daemon=False,
            )
            self._thread.start()
        self._write_worker_state()
        return True, "Worker iniciado."

    def _set_status(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)
            self._status["updated_at"] = time.time()

    def _write_worker_state(self, extra: dict | None = None):
        state = load_run_state()
        payload = dict(self.get_status())
        if extra:
            payload.update(extra)
        state["worker"] = payload
        save_run_state(state)

    def _run(self, config: dict):
        source_en_root = str(config.get("source_en_root") or "")
        if not source_en_root:
            source_en_root = str(load_run_state().get("source_en_root") or "")
        source_es_root = str(config.get("source_es_root") or "")
        output_root = str(config.get("output_root") or "")
        queue = list(config.get("queue") or [])
        batch_size = int(config.get("batch_size") or 20)
        generate_zip = bool(config.get("generate_zip", True))
        progresso = ProgressManager()
        failed_files: list[dict] = []

        known_progress_keys = set(str(k) for k in getattr(progresso, "progress", {}).keys())

        def resolve_progress_key(source_path: Path, rel_key: str) -> str:
            if rel_key in known_progress_keys:
                return rel_key
            abs_key = str(source_path)
            if abs_key in known_progress_keys:
                return abs_key
            rel_low = rel_key.lower()
            direct_suffix = [k for k in known_progress_keys if k.lower().endswith(rel_low)]
            if len(direct_suffix) == 1:
                return direct_suffix[0]
            contained_suffix = [k for k in known_progress_keys if rel_low.endswith(k.lower())]
            if len(contained_suffix) == 1:
                return contained_suffix[0]
            # fallback: evita inflar o contador com caminhos absolutos inconsistentes
            return rel_key

        try:
            api_key = obter_api_key()
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY nao encontrada.")
            client = criar_client(api_key)
            model_name = obter_model_name()
            glossario = obter_glossario_completo()

            completed = 0
            for file_pos, source_file in enumerate(queue, start=1):
                if self._stop_event.is_set():
                    self._set_status(running=False, state="stopped")
                    self._write_worker_state()
                    return

                source_path = Path(source_file)
                rel_key = relpath_display(source_path, source_en_root) if source_en_root else str(source_path)
                progress_key = resolve_progress_key(source_path, rel_key)

                # Evita retraduzir/sobrescrever arquivos ja concluidos em execucoes anteriores.
                if bool(getattr(progresso, "progress", {}).get(progress_key, False)):
                    completed += 1
                    self._set_status(
                        current_file=str(source_path),
                        current_relpath=progress_key,
                        queue_index=file_pos,
                        entry_total=0,
                        entry_idx=0,
                        chunk_start=0,
                        chunk_end=0,
                        completed_files=completed,
                    )
                    self._write_worker_state({"last_completed_file": progress_key})
                    continue

                tree = ET.parse(source_path)
                root = tree.getroot()
                entries = root.findall(".//Entry")
                self._set_status(
                    current_file=str(source_path),
                    current_relpath=progress_key,
                    queue_index=file_pos,
                    entry_total=len(entries),
                    entry_idx=0,
                    chunk_start=0,
                    chunk_end=0,
                )
                self._write_worker_state()

                es_entries = None
                if source_es_root and source_en_root:
                    es_file = localizar_arquivo_equivalente_por_idioma(
                        source_file=str(source_path),
                        source_root_origem=source_en_root,
                        source_root_destino=source_es_root,
                    )
                    if es_file:
                        try:
                            es_tree = ET.parse(es_file)
                            es_entries = es_tree.getroot().findall(".//Entry")
                        except Exception:
                            es_entries = None

                idx = 0
                while idx < len(entries):
                    if self._stop_event.is_set():
                        self._set_status(running=False, state="stopped")
                        self._write_worker_state()
                        return

                    chunk_end = min(len(entries), idx + batch_size)
                    self._set_status(
                        chunk_start=idx + 1,
                        chunk_end=chunk_end,
                        entry_idx=idx,
                    )
                    self._write_worker_state()
                    textos_en = []
                    textos_es = []
                    for i in range(idx, chunk_end):
                        en_node = entries[i].find("DefaultText")
                        txt_en = strip_bidi_controls(en_node.text if en_node is not None and en_node.text else "")
                        textos_en.append(txt_en)
                        txt_es = ""
                        if es_entries and i < len(es_entries):
                            es_node = es_entries[i].find("DefaultText")
                            txt_es = strip_bidi_controls(es_node.text if es_node is not None and es_node.text else "")
                        textos_es.append(txt_es)

                    instrucoes = obter_contexto_voz(source_path.name)
                    lote_res = None
                    last_exc = None
                    for attempt in range(1, 6):
                        if self._stop_event.is_set():
                            self._set_status(running=False, state="stopped")
                            self._write_worker_state()
                            return
                        try:
                            lote_res = processar_lote_entrada(
                                client=client,
                                model_name=model_name,
                                textos_en=textos_en,
                                textos_es=textos_es,
                                instrucoes_voz=instrucoes,
                                glossario=glossario,
                            )
                            break
                        except TranslationAPIError as exc:
                            last_exc = exc
                            delay_s = 1.5 * (2 ** (attempt - 1))
                            time.sleep(min(delay_s, 12.0))
                        except Exception as exc:
                            last_exc = exc
                            delay_s = 1.5 * (2 ** (attempt - 1))
                            time.sleep(min(delay_s, 12.0))
                    if lote_res is None:
                        failed_files.append(
                            {
                                "file": progress_key,
                                "error": str(last_exc),
                                "at_entry": idx,
                            }
                        )
                        self._write_worker_state(
                            {
                                "failed_files": failed_files,
                                "last_warning": f"Falha de rede/API em {progress_key}; arquivo pulado.",
                            }
                        )
                        logger.warning(
                            "Falha de rede/API no arquivo %s (entrada %s). Pulando arquivo. erro=%s",
                            progress_key,
                            idx,
                            last_exc,
                        )
                        break

                    for offset, i in enumerate(range(idx, chunk_end)):
                        res = lote_res[offset]
                        entry = entries[i]
                        en_node = entry.find("DefaultText")
                        txt_en = strip_bidi_controls(en_node.text if en_node is not None and en_node.text else "")
                        txt_es = textos_es[offset]
                        trad_padrao = strip_bidi_controls(res.get("traducao_padrao", ""))
                        trad_feminina = strip_bidi_controls(res.get("traducao_feminina", ""))
                        faltantes = verificar_termos_faltantes(txt_en)
                        validacao = validar_traducao_com_es(
                            texto_en=txt_en,
                            texto_es=txt_es,
                            texto_pt=trad_padrao,
                        )
                        needs_audit = precisa_auditoria(
                            int(res.get("confianca", 0) or 0),
                            validacao.get("status", "ok"),
                        ) or bool(faltantes)

                        if en_node is not None:
                            en_node.text = trad_padrao
                        fem_node = entry.find("FemaleText")
                        if fem_node is not None:
                            fem_final = normalizar_traducao_feminina(
                                trad_padrao,
                                trad_feminina,
                            )
                            fem_node.text = fem_final if fem_final else None

                        if needs_audit:
                            progresso.add_or_update_audit_item(
                                {
                                    "file": progress_key,
                                    "entry_idx": i,
                                    "needs_audit": True,
                                    "confidence": int(res.get("confianca", 0) or 0),
                                    "validation_status": validacao.get("status"),
                                    "issues": validacao.get("issues", []),
                                    "missing_terms": faltantes,
                                    "original_en": txt_en,
                                    "reference_es": txt_es,
                                    "translated_pt": trad_padrao,
                                    "translated_feminine": trad_feminina,
                                }
                            )
                        else:
                            progresso.clear_audit_item(progress_key, i)
                    idx = chunk_end
                    self._set_status(entry_idx=idx)
                    self._write_worker_state()

                if failed_files and failed_files[-1].get("file") == progress_key and idx < len(entries):
                    # arquivo pulado por falha de rede/API; segue para o proximo
                    continue

                salvar_xml_traduzido(
                    tree=tree,
                    source_file=str(source_path),
                    source_en_root=source_en_root,
                    output_root=output_root,
                )
                progresso.update_status(progress_key, True)
                completed += 1
                self._set_status(completed_files=completed, entry_idx=len(entries))
                self._write_worker_state({"last_completed_file": progress_key, "failed_files": failed_files})

            if generate_zip:
                try:
                    zip_path = gerar_zip_da_saida(output_root=output_root, zip_name="traducao_ptbr.zip")
                    self._write_worker_state({"batch_zip_path": str(zip_path)})
                except Exception:
                    logger.exception("Nao foi possivel gerar zip final do worker.")

            self._set_status(
                running=False,
                state="completed",
                current_file="",
                current_relpath="",
                chunk_start=0,
                chunk_end=0,
            )
            self._write_worker_state({"failed_files": failed_files})
        except Exception as exc:
            logger.exception("Erro no TranslationWorker")
            self._set_status(running=False, state="error", error=str(exc))
            self._write_worker_state({"failed_files": failed_files})
