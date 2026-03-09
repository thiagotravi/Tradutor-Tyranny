import logging
from pathlib import Path
import xml.etree.ElementTree as ET

import streamlit as st
import streamlit.components.v1 as components

from app_core.instance_lock import InstanceAlreadyRunningError, ensure_single_instance
from app_core.context_rules import obter_contexto_voz
from app_core.filesystem import (
    descobrir_arquivos_stringtable,
    localizar_arquivo_equivalente_por_idioma,
    relpath_display,
    resolver_source_language_root,
)
from app_core.glossary import verificar_termos_faltantes
from app_core.output_packaging import caminho_saida_espelho, gerar_zip_da_saida, salvar_xml_traduzido
from app_core.settings import criar_client, obter_api_key, obter_model_name
from app_core.translator import (
    TranslationAPIError,
    TranslationResponseError,
    normalizar_traducao_feminina,
    processar_lote_entrada,
    sugerir_traducao_glossario,
)
from app_core.validation import precisa_auditoria, validar_traducao_com_es
from app_core.run_state import load_run_state, save_run_state
from app_core.batch_worker import TranslationWorker
from translation_progress import ProgressManager
from app_core.glossary import (
    carregar_glossario_usuario,
    coletar_contexto_termos_glossario_em_arquivos,
    coletar_termos_glossario_em_arquivos,
    obter_glossario_completo,
    resetar_glossario_usuario,
    salvar_glossario_usuario,
)

logger = logging.getLogger(__name__)
BATCH_SIZE = 20


@st.cache_resource
def get_app_instance_lock():
    return ensure_single_instance()


if hasattr(st, "dialog"):
    @st.dialog("Relatório Final do Lote")
    def _show_final_report_dialog(report: dict):
        st.write(f"Arquivos concluídos: **{report.get('completed', 0)} / {report.get('total', 0)}**")
        st.write(f"Pendentes de auditoria: **{report.get('audit_pending', 0)}**")
        st.write(f"Arquivos pulados por falha de rede/API: **{report.get('failed_count', 0)}**")
        zip_path = report.get("zip_path")
        if zip_path:
            st.caption(f"Pacote ZIP: `{zip_path}`")
        failed_files = report.get("failed_files") or []
        if failed_files:
            st.markdown("**Arquivos com falha (amostra):**")
            for item in failed_files[:10]:
                st.write(f"- {item.get('file')} (entrada {item.get('at_entry', '-')})")


def init_translation_client():
    api_key = obter_api_key()
    if not api_key:
        st.error("Erro: GEMINI_API_KEY nao encontrada.")
        st.info("Configure a chave no arquivo `.env` ou em `.streamlit/secrets.toml`.")
        st.stop()

    try:
        client = criar_client(api_key)
        model_name = obter_model_name()
    except Exception as exc:
        st.error("Nao foi possivel inicializar o cliente Gemini.")
        st.caption(f"Detalhe tecnico: {exc}")
        st.stop()
    return client, model_name


def reset_translation_state(target_id: str):
    st.session_state.idx = 0
    st.session_state.cache = {}
    st.session_state.last_target = target_id


def load_tree_from_target(source_mode: str, selected_path: str, uploaded_file, source_root: str = ""):
    if source_mode == "Diretorio":
        tree = ET.parse(selected_path)
        root = tree.getroot()
        display_name = Path(selected_path).name
        progress_key = relpath_display(Path(selected_path), source_root) if source_root else selected_path
    else:
        tree = ET.parse(uploaded_file)
        root = tree.getroot()
        display_name = uploaded_file.name
        progress_key = uploaded_file.name
    return tree, root, display_name, progress_key


def carregar_entradas_es_para_arquivo(selected_path: str):
    source_es_root = st.session_state.get("source_es_root")
    source_en_root = st.session_state.get("source_en_root")
    if not source_es_root or not source_en_root:
        return None, None

    es_file = localizar_arquivo_equivalente_por_idioma(
        source_file=selected_path,
        source_root_origem=source_en_root,
        source_root_destino=source_es_root,
    )
    if not es_file:
        return None, None

    try:
        es_tree = ET.parse(es_file)
        es_root = es_tree.getroot()
        return es_root.findall(".//Entry"), str(es_file)
    except Exception:
        return None, None


def reset_glossary_step():
    st.session_state.glossary_scan_done = False
    st.session_state.glossary_ready = False
    st.session_state.glossary_pending_terms = []
    st.session_state.glossary_cursor = 0
    st.session_state.glossary_suggestions = {}
    st.session_state.glossary_term_contexts = {}


def persist_run_state():
    state = {
        "source_root": st.session_state.get("source_root", ""),
        "source_en_root": st.session_state.get("source_en_root", ""),
        "source_es_input": st.session_state.get("source_es_input", ""),
        "source_es_root": st.session_state.get("source_es_root", ""),
        "output_root": st.session_state.get("output_root", ""),
        "source_mode": st.session_state.get("source_mode", "Diretorio"),
        "discovered_files": st.session_state.get("discovered_files", []),
        "selected_file_path": st.session_state.get("selected_file_path"),
        "last_target": st.session_state.get("last_target"),
        "progress_key": st.session_state.get("progress_key"),
        "idx": st.session_state.get("idx", 0),
        "batch_active": st.session_state.get("batch_active", False),
        "batch_queue": st.session_state.get("batch_queue", []),
        "batch_cursor": st.session_state.get("batch_cursor", 0),
        "run_mode": st.session_state.get("run_mode", "standby"),
        "stop_requested": st.session_state.get("stop_requested", False),
        "glossary_scan_done": st.session_state.get("glossary_scan_done", False),
        "glossary_ready": st.session_state.get("glossary_ready", False),
        "glossary_pending_terms": st.session_state.get("glossary_pending_terms", []),
        "glossary_cursor": st.session_state.get("glossary_cursor", 0),
    }
    has_meaningful_state = any(
        [
            state.get("source_root"),
            state.get("source_en_root"),
            state.get("source_es_input"),
            state.get("discovered_files"),
            state.get("selected_file_path"),
            state.get("last_target"),
            int(state.get("idx", 0) or 0) > 0,
            state.get("glossary_scan_done"),
            state.get("batch_active"),
        ]
    )
    if not has_meaningful_state:
        return
    merged = load_run_state()
    merged.update(state)
    save_run_state(merged)
    st.session_state._saved_run_state = merged


@st.cache_resource
def get_translation_worker():
    return TranslationWorker()


def render_worker_monitor(worker: TranslationWorker):
    state = load_run_state().get("worker", {})
    status = worker.get_status()
    state = {**state, **status}
    running = state.get("state") in {"running", "stopping"} and bool(state.get("running", False))

    if running:
        total = int(state.get("total_files", 0) or 0)
        completed = int(state.get("completed_files", 0) or 0)
        queue_index = int(state.get("queue_index", 0) or 0)
        current = state.get("current_relpath") or state.get("current_file") or "-"
        entry_total = int(state.get("entry_total", 0) or 0)
        entry_idx = int(state.get("entry_idx", 0) or 0)
        chunk_start = int(state.get("chunk_start", 0) or 0)
        chunk_end = int(state.get("chunk_end", 0) or 0)
        st.info("Tradução em Andamento...")
        st.caption(f"Arquivo atual ({queue_index}/{max(total, 1)}): `{current}`")
        if entry_total > 0:
            if chunk_start > 0 and chunk_end > 0:
                st.caption(
                    f"Lote atual de entradas: `{chunk_start}-{chunk_end}` "
                    f"(progresso no arquivo: {entry_idx}/{entry_total})"
                )
            else:
                st.caption(f"Progresso no arquivo: `{entry_idx}/{entry_total}`")
        st.progress((completed / max(total, 1)) if total else 0.0)
        st.caption(f"{completed} de {total} arquivo(s) finalizado(s)")
        failed = state.get("failed_files") or []
        if failed:
            st.warning(f"Arquivos com falha de rede/API ate agora: {len(failed)} (o worker continua processando).")
            if state.get("last_warning"):
                st.caption(f"Ultimo aviso: {state.get('last_warning')}")
        if st.button("Parar", key="worker_stop_button"):
            worker.request_stop()
            st.rerun()
        col_refresh_now, col_refresh_auto = st.columns(2)
        with col_refresh_now:
            if st.button("Atualizar status agora", key="worker_refresh_now"):
                st.rerun()
        with col_refresh_auto:
            auto_refresh = st.checkbox(
                "Atualizacao automatica (5s)",
                value=False,
                key="worker_auto_refresh",
            )
        if auto_refresh:
            components.html(
                """
                <script>
                  setTimeout(function() {
                    window.parent.location.reload();
                  }, 5000);
                </script>
                """,
                height=0,
            )
            st.caption("Atualizacao automatica ativa.")
    elif state.get("state") == "error":
        st.error("Worker finalizado com erro.")
        st.caption(f"Detalhe tecnico: {state.get('error', '-')}")
    elif state.get("state") == "completed":
        st.success("Worker concluido.")
        failed = state.get("failed_files") or []
        if failed:
            st.warning(f"Concluido com {len(failed)} arquivo(s) pulado(s) por falha de rede/API.")
        total, completed, _ = st.session_state.progresso.get_stats()
        report_token = f"{state.get('started_at')}-{state.get('updated_at')}-{completed}-{total}"
        if st.session_state.get("_last_report_token") != report_token:
            st.session_state._last_report_token = report_token
            report = {
                "total": total,
                "completed": completed,
                "audit_pending": len(st.session_state.progresso.get_audit_items()),
                "failed_count": len(failed),
                "failed_files": failed,
                "zip_path": state.get("batch_zip_path"),
            }
            if hasattr(st, "dialog"):
                _show_final_report_dialog(report)
            else:
                st.info("Relatório Final do Lote")
                st.write(f"Arquivos concluídos: {completed}/{total}")
                st.write(f"Pendentes de auditoria: {report['audit_pending']}")
                st.write(f"Arquivos pulados por falha de rede/API: {report['failed_count']}")


def _aplicar_estado(saved: dict, force: bool = False):
    if not saved:
        return
    default_map = {
        "source_root": "",
        "source_en_root": "",
        "source_es_input": "",
        "source_es_root": "",
        "output_root": str(Path.cwd() / "build" / "pt-BR"),
        "source_mode": "Diretorio",
        "discovered_files": [],
        "selected_file_path": None,
        "last_target": None,
        "progress_key": None,
        "idx": 0,
        "batch_active": False,
        "batch_queue": [],
        "batch_cursor": 0,
        "run_mode": "standby",
        "stop_requested": False,
        "glossary_scan_done": False,
        "glossary_ready": False,
        "glossary_pending_terms": [],
        "glossary_cursor": 0,
    }
    for key, default in default_map.items():
        if force or key not in st.session_state or st.session_state.get(key) in (None, "", [], False):
            st.session_state[key] = saved.get(key, default)

    # Sincroniza widgets-chave para refletir estado retomado.
    st.session_state["source_root_input"] = st.session_state.get("source_root", "")
    st.session_state["source_es_input"] = st.session_state.get("source_es_input", "")
    st.session_state["output_root_input"] = st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR"))


def aplicar_estado_salvo_no_session():
    if st.session_state.get("_run_state_loaded"):
        return
    saved = load_run_state()
    st.session_state._saved_run_state = saved
    _aplicar_estado(saved, force=False)
    # Ao abrir o app, sempre inicia em espera; retomar exige comando explicito.
    st.session_state.run_mode = "standby"
    st.session_state.stop_requested = False
    st.session_state._run_state_loaded = True


def render_resume_controls(worker: TranslationWorker):
    saved = st.session_state.get("_saved_run_state", {})
    if not saved:
        return

    source_root = saved.get("source_root", "")
    selected_file = saved.get("selected_file_path")
    idx = int(saved.get("idx", 0) or 0)
    glossary_done = bool(saved.get("glossary_ready", False))
    batch_active = bool(saved.get("batch_active", False))
    saved_mode = saved.get("run_mode", "standby")
    st.info("Processo anterior detectado. Voce pode retomar ou reiniciar.")
    st.caption(f"EN root: `{source_root or '-'}`")
    st.caption(f"ES root: `{saved.get('source_es_root') or '-'}`")
    st.caption(f"Arquivo alvo: `{selected_file or '-'}` | Entrada: `{idx}`")
    st.caption(f"Glossario concluido: `{glossary_done}` | Lote ativo: `{batch_active}` | Modo salvo: `{saved_mode}`")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Retomar de onde parou", key="resume_previous_run"):
            _aplicar_estado(saved, force=True)
            st.session_state.run_mode = "standby"
            st.session_state.stop_requested = False
            if st.session_state.get("source_mode", "Diretorio") == "Diretorio":
                saved_queue = list(saved.get("batch_queue") or [])
                saved_cursor = int(saved.get("batch_cursor", 0) or 0)
                if saved_queue and saved_cursor < len(saved_queue):
                    queue = saved_queue[saved_cursor:]
                else:
                    queue = saved_queue

                config = {
                    "source_en_root": saved.get("source_en_root", st.session_state.get("source_en_root", "")),
                    "source_es_root": saved.get("source_es_root", st.session_state.get("source_es_root", "")),
                    "output_root": saved.get("output_root", st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR"))),
                    "queue": queue,
                    "batch_size": BATCH_SIZE,
                    "generate_zip": True,
                }
                started, msg = worker.start(config)
                if started:
                    st.success("Retomada iniciada em background.")
                else:
                    st.warning(msg)
            st.session_state._resume_applied = True
            persist_run_state()
            st.rerun()
    with col2:
        if st.button("Reiniciar do comeco (manter configuracoes)", key="restart_current_settings"):
            st.session_state.idx = 0
            st.session_state.cache = {}
            st.session_state.last_target = None
            st.session_state.batch_cursor = 0
            st.session_state.batch_active = False
            st.session_state.batch_queue = []
            st.session_state.run_mode = "standby"
            st.session_state.stop_requested = False
            st.session_state.selected_file_path = (
                st.session_state.get("discovered_files", [None])[0]
                if st.session_state.get("discovered_files")
                else st.session_state.get("selected_file_path")
            )
            persist_run_state()
            st.rerun()

def render_glossary_step(client, model_name: str):
    discovered_files = st.session_state.get("discovered_files", [])
    if not discovered_files:
        st.info("Carregue os arquivos para iniciar a etapa de glossario.")
        return False

    st.subheader("Etapa 1: Construir glossario (obrigatorio)")
    if not st.session_state.get("glossary_scan_done"):
        if st.button("Escanear termos de glossario", key="scan_glossary_terms"):
            termos_encontrados = coletar_termos_glossario_em_arquivos(discovered_files)
            contextos = coletar_contexto_termos_glossario_em_arquivos(discovered_files)
            glossario = obter_glossario_completo()
            existing_low = {k.lower() for k in glossario.keys()}
            pendentes = [t for t in termos_encontrados if t.lower() not in existing_low]
            st.session_state.glossary_pending_terms = pendentes
            st.session_state.glossary_term_contexts = contextos
            st.session_state.glossary_cursor = 0
            st.session_state.glossary_scan_done = True
            st.session_state.glossary_ready = len(pendentes) == 0
            st.rerun()
        st.info("Escaneie os arquivos para identificar termos faltantes em [url=glossary:...].")
        return False

    pendentes = st.session_state.get("glossary_pending_terms", [])
    if not pendentes:
        st.success("Glossario pronto. Nenhum termo pendente.")
        st.session_state.glossary_ready = True
        return True

    cursor = st.session_state.get("glossary_cursor", 0)
    if cursor >= len(pendentes):
        st.session_state.glossary_ready = True
        st.success("Glossario concluido.")
        return True

    termo = pendentes[cursor]
    st.warning(f"Termo pendente {cursor + 1}/{len(pendentes)}: `{termo}`")
    st.text_input(
        "Termo original (EN)",
        value=termo,
        disabled=True,
        key=f"term_en_{cursor}_{termo}",
    )
    contextos = st.session_state.get("glossary_term_contexts", {}).get(termo, [])
    if contextos:
        source_en_root = st.session_state.get("source_en_root", "")
        with st.expander("Contexto de ocorrencia (EN)", expanded=True):
            for i, item in enumerate(contextos, start=1):
                file_full = item.get("file", "")
                file_rel = relpath_display(Path(file_full), source_en_root) if source_en_root else file_full
                line_no = item.get("line", "?")
                excerpt = item.get("excerpt", "")
                st.markdown(f"**{i}.** `{file_rel}` (linha {line_no})")
                st.code(excerpt, language="text")
    suggestions = st.session_state.get("glossary_suggestions", {})
    sugestao = suggestions.get(termo, "")
    if not sugestao:
        with st.spinner("Gerando sugestao de traducao..."):
            sugestao = sugerir_traducao_glossario(client, model_name, termo_en=termo)
        suggestions[termo] = sugestao
        st.session_state.glossary_suggestions = suggestions

    st.text_input("Sugestao do tradutor", value=sugestao, disabled=True, key=f"sugg_{cursor}_{termo}")
    termo_custom = st.text_input(
        "Traducao final (edite se necessario)",
        value=sugestao,
        key=f"custom_{cursor}_{termo}",
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Salvar termo e continuar", key=f"save_term_{cursor}_{termo}"):
            glossario_user = carregar_glossario_usuario()
            glossario_user[termo] = termo_custom.strip() or sugestao or termo
            salvar_glossario_usuario(glossario_user)
            st.session_state.glossary_cursor = cursor + 1
            st.rerun()
    with col_b:
        if st.button("Pular termo (manter original)", key=f"skip_term_{cursor}_{termo}"):
            glossario_user = carregar_glossario_usuario()
            glossario_user[termo] = termo
            salvar_glossario_usuario(glossario_user)
            st.session_state.glossary_cursor = cursor + 1
            st.rerun()

    return False


def render_directory_selector():
    st.subheader("Fonte de Arquivos")
    root_path = st.text_input(
        "Diretorio raiz (ex.: ...\\localized ou ...\\localized\\en\\text)",
        value=st.session_state.get("source_root", ""),
        key="source_root_input",
    )

    if st.button("Carregar arquivos do diretorio"):
        st.session_state.source_root = root_path.strip()
        try:
            source_en_root, arquivos = descobrir_arquivos_stringtable(st.session_state.source_root)
            st.session_state.source_en_root = str(source_en_root)
            st.session_state.discovered_files = [str(p) for p in arquivos]
            st.session_state.selected_file_path = st.session_state.discovered_files[0] if arquivos else None
            reset_glossary_step()
            if not st.session_state.get("output_root"):
                st.session_state.output_root = str(Path.cwd() / "build" / "pt-BR")
        except ValueError as exc:
            st.error(str(exc))
            st.session_state.discovered_files = []
            st.session_state.source_en_root = None

    arquivos = st.session_state.get("discovered_files", [])
    source_en_root = st.session_state.get("source_en_root")
    if not arquivos:
        st.info("Informe um diretorio valido e clique em carregar.")
        return None

    # Reconstrói checklist de progresso com base no conjunto atual de arquivos EN.
    rebuild_sig = (
        str(source_en_root or ""),
        len(arquivos),
        str(arquivos[0]) if arquivos else "",
        str(arquivos[-1]) if arquivos else "",
    )
    if st.session_state.get("_progress_rebuild_sig") != rebuild_sig:
        st.session_state.progresso.rebuild_from_discovered_files(arquivos, source_en_root or "")
        st.session_state._progress_rebuild_sig = rebuild_sig

    st.caption(f"Source EN detectado: `{source_en_root}`")
    st.caption(f"Arquivos encontrados em EN: {len(arquivos)}")
    source_es_input = st.text_input(
        "Diretorio raiz ES para validacao (opcional)",
        value=st.session_state.get("source_es_input", ""),
        key="source_es_input",
        help="Aceita localized, localized/es ou localized/es/text.",
    )
    if source_es_input.strip():
        try:
            resolved_es_root = resolver_source_language_root(source_es_input.strip(), "es")
            st.session_state.source_es_root = str(resolved_es_root)
        except ValueError:
            st.session_state.source_es_root = None
    else:
        st.session_state.source_es_root = None

    if st.session_state.get("source_es_root"):
        st.caption(f"Source ES detectado: `{st.session_state.get('source_es_root')}`")
    elif source_es_input.strip():
        st.warning("Source ES invalido para validacao.")

    output_root = st.text_input(
        "Diretorio de saida espelho",
        value=st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR")),
        key="output_root_input",
    )
    st.session_state.output_root = output_root.strip()

    filtro = st.text_input("Filtrar arquivos", value="", key="dir_file_filter")
    opcoes = []
    for full in arquivos:
        rel = relpath_display(Path(full), source_en_root)
        if filtro.lower() in rel.lower():
            opcoes.append((rel, full))

    if not opcoes:
        st.warning("Nenhum arquivo corresponde ao filtro.")
        return None

    labels = [o[0] for o in opcoes]
    st.session_state.filtered_file_paths = [full for _, full in opcoes]
    selected_file_saved = st.session_state.get("selected_file_path")
    if selected_file_saved:
        for rel_label, full_path in opcoes:
            if full_path == selected_file_saved:
                st.session_state["selected_relpath"] = rel_label
                break
    selected_label = st.selectbox("Arquivo para traduzir", labels, key="selected_relpath")
    selected_map = dict(opcoes)
    selected_file_path = selected_map[selected_label]

    st.session_state.selected_file_path = selected_file_path
    return selected_file_path


def render_sidebar_progress():
    with st.sidebar:
        # Sincroniza contadores com atualizações de worker em background.
        st.session_state.progresso.reload_from_disk()
        st.header("Progresso")
        total, concluidos, percent = st.session_state.progresso.get_stats()
        st.progress(percent / 100)
        st.write(f"**{concluidos}** de **{total}** arquivos")
        st.write(f"**Auditoria pendente:** {len(st.session_state.progresso.get_audit_items())}")

        col_reset_g, col_reset_p = st.columns(2)
        with col_reset_g:
            if st.button("Resetar glossario"):
                resetar_glossario_usuario()
                reset_glossary_step()
                persist_run_state()
                st.success("Glossario de usuario resetado.")
                st.rerun()
        with col_reset_p:
            if st.button("Resetar progresso"):
                st.session_state.progresso.reset_all_status()
                persist_run_state()
                st.success("Relatorio de progresso resetado.")
                st.rerun()

        st.divider()
        with st.expander("Checklist de Arquivos"):
            busca = st.text_input("Buscar no checklist")
            if hasattr(st.session_state.progresso, "progress"):
                lista = list(st.session_state.progresso.progress.items())
                if busca:
                    lista = [(n, s) for n, s in lista if busca.lower() in n.lower()]
                for idx, (arq_nome, status) in enumerate(lista):
                    col_t, col_b = st.columns([0.8, 0.2])
                    col_t.write(f"{'OK' if status else '...'} {arq_nome}")
                    if col_b.button("Trocar", key=f"btn_{idx}_{arq_nome}"):
                        st.session_state.progresso.update_status(arq_nome, not status)
                        st.rerun()


def _resolver_arquivo_por_progress_key(progress_key: str):
    if not progress_key:
        return None
    source_en_root = st.session_state.get("source_en_root")
    discovered = st.session_state.get("discovered_files", [])
    for full in discovered:
        if str(full) == str(progress_key):
            return str(full)
        if source_en_root:
            rel = relpath_display(Path(full), source_en_root)
            if rel == progress_key:
                return str(full)
    return None


def _carregar_snapshot_auditoria(progress_key: str, entry_idx: int):
    full_path = _resolver_arquivo_por_progress_key(progress_key)
    if not full_path:
        return {"error": "Arquivo EN correspondente nao encontrado no conjunto carregado."}

    try:
        tree = ET.parse(full_path)
        root = tree.getroot()
        entries = root.findall(".//Entry")
    except Exception as exc:
        return {"error": f"Falha ao carregar arquivo para auditoria: {exc}"}

    if not entries:
        return {"error": "Arquivo nao contem entradas para auditoria."}

    safe_idx = max(0, min(int(entry_idx), len(entries) - 1))
    entry = entries[safe_idx]
    txt_node = entry.find("DefaultText")
    texto_en = txt_node.text if txt_node is not None and txt_node.text else ""

    texto_es = ""
    es_entries, _ = carregar_entradas_es_para_arquivo(full_path)
    if es_entries and safe_idx < len(es_entries):
        es_txt_node = es_entries[safe_idx].find("DefaultText")
        texto_es = es_txt_node.text if es_txt_node is not None and es_txt_node.text else ""

    return {
        "file_path": full_path,
        "entry_idx": safe_idx,
        "texto_en": texto_en,
        "texto_es": texto_es,
    }


def _aplicar_edicao_auditoria(selected: dict, texto_pt: str, texto_fem: str):
    source_file = selected.get("file_path")
    source_en_root = st.session_state.get("source_en_root", "")
    output_root = st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR"))
    entry_idx = int(selected.get("entry_idx", 0) or 0)
    if not source_file or not source_en_root:
        raise ValueError("Nao foi possivel resolver caminhos para salvar a auditoria.")

    is_current_loaded_file = (
        st.session_state.get("progress_key") == selected.get("file")
        and st.session_state.get("tree") is not None
        and st.session_state.get("entries") is not None
    )

    if is_current_loaded_file:
        entries = st.session_state.entries
        safe_idx = max(0, min(entry_idx, len(entries) - 1))
        entry = entries[safe_idx]
        txt_node = entry.find("DefaultText")
        if txt_node is not None:
            txt_node.text = texto_pt
        f_node = entry.find("FemaleText")
        if f_node is not None:
            fem_final = normalizar_traducao_feminina(texto_pt, texto_fem)
            f_node.text = fem_final if fem_final else None
        if safe_idx in st.session_state.cache:
            st.session_state.cache[safe_idx]["traducao_padrao"] = texto_pt
            st.session_state.cache[safe_idx]["traducao_feminina"] = texto_fem
        return salvar_xml_traduzido(
            tree=st.session_state.tree,
            source_file=source_file,
            source_en_root=source_en_root,
            output_root=output_root,
        )

    output_path = caminho_saida_espelho(source_file, source_en_root, output_root)
    base_path = output_path if output_path.exists() else Path(source_file)
    tree = ET.parse(base_path)
    root = tree.getroot()
    entries = root.findall(".//Entry")
    if not entries:
        raise ValueError("Arquivo de auditoria nao contem entradas.")
    safe_idx = max(0, min(entry_idx, len(entries) - 1))
    entry = entries[safe_idx]
    txt_node = entry.find("DefaultText")
    if txt_node is not None:
        txt_node.text = texto_pt
    f_node = entry.find("FemaleText")
    if f_node is not None:
        fem_final = normalizar_traducao_feminina(texto_pt, texto_fem)
        f_node.text = fem_final if fem_final else None
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path


def render_audit_queue_section():
    audit_items = st.session_state.progresso.get_audit_items()
    run_mode = st.session_state.get("run_mode", "standby")
    running = run_mode == "running"
    selected = st.session_state.get("audit_selected_case")

    if selected:
        st.subheader("Caso selecionado para auditoria")
        st.caption(f"Arquivo: `{selected.get('file', '-')}` | Entrada: `{selected.get('entry_idx', '-')}`")
        st.caption(
            f"Confianca: `{selected.get('confidence', '-')}` | "
            f"Status de validacao: `{selected.get('validation_status', '-')}`"
        )
        issues_sel = selected.get("issues", []) or []
        faltantes_sel = selected.get("missing_terms", []) or []
        if issues_sel or faltantes_sel:
            st.markdown("**Motivo da auditoria**")
            for issue in issues_sel:
                st.write(f"- [{issue.get('severity')}] {issue.get('message')}")
            if faltantes_sel:
                st.write(f"- Termos faltantes: {', '.join(faltantes_sel)}")
        if selected.get("error"):
            st.error(selected["error"])
            if st.button("Voltar para fila de auditoria", key="audit_back_from_error"):
                st.session_state.audit_selected_case = None
                st.rerun()
            return

        if running:
            st.warning("Pausa necessaria: finalize/pare a traducao para editar e aprovar este caso.")

        st.text_area("Original (EN)", value=selected.get("texto_en", ""), height=160, disabled=True)
        st.text_area("Referencia (ES)", value=selected.get("texto_es", ""), height=160, disabled=True)
        edit_pt = st.text_area(
            "Tradução (PT) - edite antes de aprovar",
            value=selected.get("translated_pt", ""),
            height=180,
            disabled=running,
            key=f"audit_edit_pt_{selected.get('file')}_{selected.get('entry_idx')}",
        )
        edit_fem = st.text_area(
            "Tradução feminina (PT) - opcional",
            value=selected.get("translated_feminine", ""),
            height=140,
            disabled=running,
            key=f"audit_edit_fem_{selected.get('file')}_{selected.get('entry_idx')}",
        )

        col_apply, col_cancel = st.columns(2)
        with col_apply:
            if st.button("Aprovar auditoria e salvar", key="audit_apply_selected", disabled=running):
                try:
                    destino = _aplicar_edicao_auditoria(selected, edit_pt, edit_fem)
                    st.session_state.progresso.clear_audit_item(
                        selected.get("file"),
                        selected.get("entry_idx"),
                    )
                    st.session_state.audit_selected_case = None
                    st.session_state._prefer_audit_tab_once = True
                    st.success(f"Auditoria aplicada e salva em: `{destino}`")
                    st.rerun()
                except Exception as exc:
                    st.error("Falha ao salvar auditoria.")
                    st.caption(f"Detalhe tecnico: {exc}")
        with col_cancel:
            if st.button("Cancelar e voltar para fila", key="audit_cancel_selected"):
                st.session_state.audit_selected_case = None
                st.session_state._prefer_audit_tab_once = True
                st.rerun()
        return

    if not audit_items:
        st.success("Nenhuma entrada pendente de auditoria.")
        return

    st.caption(f"Itens pendentes: {len(audit_items)}")
    if running:
        st.warning("A auditoria fica visivel em tempo real, mas a interacao exige processo pausado/standby.")

    filtro = st.text_input("Filtrar auditoria por arquivo", value="", key="audit_filter")
    current_file_only = st.checkbox("Somente arquivo atual", value=False, key="audit_current_only")
    current_key = st.session_state.get("progress_key")

    rows = audit_items
    if current_file_only and current_key:
        rows = [r for r in rows if r.get("file") == current_key]
    if filtro:
        rows = [r for r in rows if filtro.lower() in str(r.get("file", "")).lower()]

    if not rows:
        st.info("Nenhum item corresponde ao filtro.")
        return

    for i, item in enumerate(rows, start=1):
        file_name = item.get("file", "-")
        entry_idx = item.get("entry_idx", "-")
        conf = item.get("confidence", "-")
        status = item.get("validation_status", "-")
        issues = item.get("issues", [])
        faltantes = item.get("missing_terms", [])
        with st.container():
            st.markdown(f"**{i}.** `{file_name}` | entrada `{entry_idx}` | conf `{conf}` | status `{status}`")
            if issues:
                for issue in issues:
                    st.write(f"- [{issue.get('severity')}] {issue.get('message')}")
            if faltantes:
                st.write(f"- Termos faltantes: {', '.join(faltantes)}")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Auditar caso", key=f"audit_open_{file_name}_{entry_idx}_{i}", disabled=running):
                    try:
                        idx_int = int(entry_idx)
                    except Exception:
                        idx_int = 0
                    snapshot = _carregar_snapshot_auditoria(file_name, idx_int)
                    if not snapshot.get("texto_es"):
                        snapshot["texto_es"] = item.get("reference_es", "") or ""
                    st.session_state.audit_selected_case = {
                        **item,
                        **snapshot,
                    }
                    st.rerun()
            with col2:
                if st.button("Marcar como auditado", key=f"audit_done_{file_name}_{entry_idx}_{i}", disabled=running):
                    st.session_state.progresso.clear_audit_item(file_name, entry_idx)
                    if (
                        st.session_state.get("audit_selected_case")
                        and st.session_state.audit_selected_case.get("file") == file_name
                        and st.session_state.audit_selected_case.get("entry_idx") == entry_idx
                    ):
                        st.session_state.audit_selected_case = None
                    st.rerun()


st.set_page_config(page_title="Tyranny Localizer v0.7", layout="wide")
st.title("Estacao Seladestinos v0.7")

try:
    get_app_instance_lock()
except InstanceAlreadyRunningError as exc:
    st.error("Outra instância do app já está ativa.")
    st.caption(f"PID ativo: `{exc.pid}`")
    st.caption("Feche a outra instância antes de iniciar uma nova.")
    st.stop()
except Exception as exc:
    st.error("Falha ao validar lock de instância única.")
    st.caption(f"Detalhe tecnico: {exc}")
    st.stop()

if "progresso" not in st.session_state:
    st.session_state.progresso = ProgressManager()
if "cache" not in st.session_state:
    st.session_state.cache = {}
if "batch_active" not in st.session_state:
    st.session_state.batch_active = False
if "batch_queue" not in st.session_state:
    st.session_state.batch_queue = []
if "batch_cursor" not in st.session_state:
    st.session_state.batch_cursor = 0
if "batch_zip_path" not in st.session_state:
    st.session_state.batch_zip_path = ""
if "run_mode" not in st.session_state:
    st.session_state.run_mode = "standby"
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False
if "es_entries" not in st.session_state:
    st.session_state.es_entries = None
if "es_file_path" not in st.session_state:
    st.session_state.es_file_path = None
if "glossary_scan_done" not in st.session_state:
    st.session_state.glossary_scan_done = False
if "glossary_ready" not in st.session_state:
    st.session_state.glossary_ready = False
if "glossary_pending_terms" not in st.session_state:
    st.session_state.glossary_pending_terms = []
if "glossary_cursor" not in st.session_state:
    st.session_state.glossary_cursor = 0
if "glossary_suggestions" not in st.session_state:
    st.session_state.glossary_suggestions = {}
if "glossary_term_contexts" not in st.session_state:
    st.session_state.glossary_term_contexts = {}
if "_run_state_loaded" not in st.session_state:
    st.session_state._run_state_loaded = False
if "_saved_run_state" not in st.session_state:
    st.session_state._saved_run_state = {}
if "_resume_applied" not in st.session_state:
    st.session_state._resume_applied = False
if "audit_selected_case" not in st.session_state:
    st.session_state.audit_selected_case = None
if "_prefer_audit_tab_once" not in st.session_state:
    st.session_state._prefer_audit_tab_once = False
if "_progress_rebuild_sig" not in st.session_state:
    st.session_state._progress_rebuild_sig = None
if "_last_report_token" not in st.session_state:
    st.session_state._last_report_token = ""

aplicar_estado_salvo_no_session()

worker = get_translation_worker()
render_sidebar_progress()
render_resume_controls(worker)
client, model_name = init_translation_client()

audit_pending = len(st.session_state.progresso.get_audit_items())
if st.session_state.get("_prefer_audit_tab_once"):
    tab_audit, tab_translate = st.tabs(
        [f"🔍 Fila de Auditoria ({audit_pending})", "Tradução"]
    )
    st.session_state._prefer_audit_tab_once = False
else:
    tab_translate, tab_audit = st.tabs(
        ["Tradução", f"🔍 Fila de Auditoria ({audit_pending})"]
    )

with tab_audit:
    render_audit_queue_section()

def render_translation_workspace(client, model_name, worker: TranslationWorker):
    source_mode = st.radio(
        "Modo de entrada",
        options=["Diretorio", "Arquivo unico (legado)"],
        horizontal=True,
        key="source_mode",
    )

    selected_path = None
    uploaded_file = None
    target_id = None
    display_name = ""

    if source_mode == "Diretorio":
        selected_path = render_directory_selector()
        if selected_path:
            target_id = f"path::{selected_path}"
    else:
        uploaded_file = st.file_uploader("Suba o arquivo .stringtable/.xml", type=["stringtable", "xml"])
        if uploaded_file:
            target_id = f"upload::{uploaded_file.name}"

    if not target_id:
        persist_run_state()
        st.stop()

    if source_mode == "Diretorio":
        if not render_glossary_step(client, model_name):
            persist_run_state()
            st.stop()

        worker_state = load_run_state().get("worker", {})
        worker_running = (
            worker.is_running()
            or (
                worker_state.get("state") in {"running", "stopping"}
                and bool(worker_state.get("running", False))
            )
        )
        filtered_queue = list(st.session_state.get("filtered_file_paths", []))
        all_discovered = list(st.session_state.get("discovered_files", []))
        selected_for_start = st.session_state.get("selected_file_path")

        c1, c2, c3 = st.columns(3)
        with c1:
            if not worker_running:
                if st.button("Iniciar Lote", key="worker_start_filtered"):
                    queue = filtered_queue or all_discovered
                    config = {
                        "source_en_root": st.session_state.get("source_en_root", ""),
                        "source_es_root": st.session_state.get("source_es_root", ""),
                        "output_root": st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR")),
                        "queue": queue,
                        "batch_size": BATCH_SIZE,
                        "generate_zip": True,
                    }
                    started, msg = worker.start(config)
                    if started:
                        st.session_state.run_mode = "standby"
                        st.session_state.stop_requested = False
                        st.success("Worker iniciado em background.")
                    else:
                        st.warning(msg)
                    st.rerun()
        with c2:
            if not worker_running:
                if st.button("Iniciar Lote do arquivo selecionado", key="worker_start_from_selected"):
                    base_queue = filtered_queue or all_discovered
                    if selected_for_start in base_queue:
                        start_idx = base_queue.index(selected_for_start)
                        queue = base_queue[start_idx:]
                    else:
                        queue = base_queue
                    config = {
                        "source_en_root": st.session_state.get("source_en_root", ""),
                        "source_es_root": st.session_state.get("source_es_root", ""),
                        "output_root": st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR")),
                        "queue": queue,
                        "batch_size": BATCH_SIZE,
                        "generate_zip": True,
                    }
                    started, msg = worker.start(config)
                    if started:
                        st.session_state.run_mode = "standby"
                        st.session_state.stop_requested = False
                        st.success("Worker iniciado em background.")
                    else:
                        st.warning(msg)
                    st.rerun()
        with c3:
            if worker_running and st.button("Parar", key="worker_stop_inline"):
                worker.request_stop()
                st.rerun()

        if worker_running:
            render_worker_monitor(worker)
            persist_run_state()
            st.stop()
        else:
            st.info("Aplicacao em standby. Use 'Iniciar Lote' para executar em background.")
            persist_run_state()
            st.stop()

    if st.session_state.get("last_target") != target_id or "tree" not in st.session_state:
        resume_idx = 0
        if "tree" not in st.session_state and st.session_state.get("last_target") == target_id:
            try:
                resume_idx = int(st.session_state.get("idx", 0) or 0)
            except Exception:
                resume_idx = 0

        tree, root, display_name, progress_key = load_tree_from_target(
            source_mode,
            selected_path,
            uploaded_file,
            st.session_state.get("source_en_root") or st.session_state.get("source_root", ""),
        )
        st.session_state.tree = tree
        st.session_state.root = root
        st.session_state.entries = root.findall(".//Entry")
        if source_mode == "Diretorio":
            es_entries, es_file_path = carregar_entradas_es_para_arquivo(selected_path)
            st.session_state.es_entries = es_entries
            st.session_state.es_file_path = es_file_path
        else:
            st.session_state.es_entries = None
            st.session_state.es_file_path = None
        st.session_state.progress_key = progress_key
        reset_translation_state(target_id)
        if resume_idx > 0:
            st.session_state.idx = min(resume_idx, max(0, len(st.session_state.entries) - 1))
    else:
        if source_mode == "Diretorio":
            display_name = Path(selected_path).name
        else:
            display_name = uploaded_file.name

    if source_mode == "Diretorio" and st.session_state.get("source_es_root"):
        if st.session_state.get("es_file_path"):
            es_rel = relpath_display(Path(st.session_state.get("es_file_path")), st.session_state.get("source_es_root"))
            st.caption(f"Referencia ES mapeada: `{es_rel}`")
        else:
            st.warning("Referencia ES nao encontrada para o arquivo selecionado.")

    run_mode = st.session_state.get("run_mode", "standby")
    if run_mode != "running":
        if run_mode == "paused":
            st.info("Traducao pausada. Use 'Retomar de onde parou' ou clique em um comando de inicio.")
        else:
            st.info("Aplicacao em standby. Escolha um arquivo/filtro e clique em um comando de inicio.")
        persist_run_state()
        st.stop()

    if st.session_state.get("stop_requested"):
        st.session_state.run_mode = "paused"
        persist_run_state()
        st.info("Parada solicitada. Processo pausado.")
        st.stop()

    while st.session_state.idx < len(st.session_state.entries):
        if st.session_state.get("stop_requested"):
            st.session_state.run_mode = "paused"
            persist_run_state()
            st.info("Parada solicitada. Processo pausado.")
            st.stop()

        idx = st.session_state.idx
        total_entries = len(st.session_state.entries)
        chunk_start = idx
        chunk_end = min(total_entries, chunk_start + BATCH_SIZE)
        st.progress(st.session_state.idx / max(total_entries, 1))
        st.caption(f"Processando lote: entradas {chunk_start + 1} a {chunk_end} de {total_entries}")

        missing_cache_indexes = [i for i in range(chunk_start, chunk_end) if i not in st.session_state.cache]
        if missing_cache_indexes:
            if st.session_state.get("stop_requested"):
                st.session_state.run_mode = "paused"
                persist_run_state()
                st.info("Parada solicitada antes da chamada de API. Processo pausado.")
                st.stop()

            es_entries = st.session_state.get("es_entries")
            textos_en_lote = []
            textos_es_lote = []
            for i in range(chunk_start, chunk_end):
                entry_i = st.session_state.entries[i]
                txt_node_i = entry_i.find("DefaultText")
                textos_en_lote.append(txt_node_i.text if txt_node_i is not None and txt_node_i.text else "")
                txt_es_i = ""
                if es_entries and i < len(es_entries):
                    es_txt_node = es_entries[i].find("DefaultText")
                    txt_es_i = es_txt_node.text if es_txt_node is not None and es_txt_node.text else ""
                textos_es_lote.append(txt_es_i)

            instrucoes_dinamicas = obter_contexto_voz(display_name)
            with st.spinner(f"Processando lote {chunk_start + 1}-{chunk_end}..."):
                try:
                    lote_res = processar_lote_entrada(
                        client=client,
                        model_name=model_name,
                        textos_en=textos_en_lote,
                        textos_es=textos_es_lote,
                        instrucoes_voz=instrucoes_dinamicas,
                        glossario=obter_glossario_completo(),
                    )
                    for offset, item in enumerate(lote_res):
                        st.session_state.cache[chunk_start + offset] = item
                except TranslationAPIError as exc:
                    logger.exception("Erro de API no lote %s-%s", chunk_start, chunk_end - 1)
                    st.error("Falha ao chamar o Gemini para traduzir este lote.")
                    st.caption(f"Detalhe tecnico: {exc}")
                    if st.button("Tentar novamente", key=f"retry_api_lote_{chunk_start}"):
                        st.rerun()
                    persist_run_state()
                    st.stop()
                except TranslationResponseError as exc:
                    logger.warning("Resposta invalida no lote %s-%s: %s", chunk_start, chunk_end - 1, exc)
                    st.error("A resposta do Gemini veio em formato inesperado para este lote.")
                    st.caption(f"Detalhe tecnico: {exc}")
                    if st.button("Tentar novamente", key=f"retry_json_lote_{chunk_start}"):
                        st.rerun()
                    persist_run_state()
                    st.stop()

        audit_flagged_in_chunk = 0
        while st.session_state.idx < chunk_end:
            if st.session_state.get("stop_requested"):
                st.session_state.run_mode = "paused"
                persist_run_state()
                st.info("Parada solicitada durante o processamento do lote. Processo pausado.")
                st.stop()

            idx = st.session_state.idx
            entry = st.session_state.entries[idx]
            txt_node = entry.find("DefaultText")
            txt_en = txt_node.text if txt_node is not None and txt_node.text else ""
            txt_es = ""
            es_entries = st.session_state.get("es_entries")
            if es_entries and idx < len(es_entries):
                es_txt_node = es_entries[idx].find("DefaultText")
                txt_es = es_txt_node.text if es_txt_node is not None and es_txt_node.text else ""

            faltantes = verificar_termos_faltantes(txt_en)
            res = st.session_state.cache[idx]
            validacao = validar_traducao_com_es(
                texto_en=txt_en,
                texto_es=txt_es,
                texto_pt=res.get("traducao_padrao", ""),
            )
            needs_audit = precisa_auditoria(
                int(res.get("confianca", 0) or 0),
                validacao["status"],
            ) or bool(faltantes)

            if txt_node is not None:
                txt_node.text = res.get("traducao_padrao", "")
            f_node = entry.find("FemaleText")
            if f_node is not None:
                fem_final = normalizar_traducao_feminina(
                    res.get("traducao_padrao", ""),
                    res.get("traducao_feminina", ""),
                )
                f_node.text = fem_final if fem_final else None

            if needs_audit:
                audit_flagged_in_chunk += 1
                st.session_state.progresso.add_or_update_audit_item(
                    {
                        "file": st.session_state.get("progress_key"),
                        "entry_idx": idx,
                        "needs_audit": True,
                        "confidence": int(res.get("confianca", 0) or 0),
                        "validation_status": validacao.get("status"),
                        "issues": validacao.get("issues", []),
                        "missing_terms": faltantes,
                        "original_en": txt_en,
                        "reference_es": txt_es,
                        "translated_pt": res.get("traducao_padrao", ""),
                        "translated_feminine": res.get("traducao_feminina", ""),
                    }
                )
            else:
                st.session_state.progresso.clear_audit_item(
                    st.session_state.get("progress_key"),
                    idx,
                )

            st.session_state.idx += 1

        if audit_flagged_in_chunk > 0:
            st.warning(f"Lote concluido com {audit_flagged_in_chunk} entrada(s) marcadas para auditoria.")
    else:
        st.success("Arquivo finalizado!")
        st.session_state.progresso.update_status(st.session_state.progress_key, True)
        if source_mode == "Diretorio" and selected_path:
            try:
                destino = salvar_xml_traduzido(
                    tree=st.session_state.tree,
                    source_file=selected_path,
                    source_en_root=st.session_state.get("source_en_root", ""),
                    output_root=st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR")),
                )
                st.caption(f"Salvo em: `{destino}`")
            except Exception as exc:
                st.error("Falha ao salvar arquivo traduzido na estrutura espelho.")
                st.caption(f"Detalhe tecnico: {exc}")

        if source_mode == "Diretorio" and st.session_state.get("batch_active"):
            queue = st.session_state.get("batch_queue", [])
            cursor = st.session_state.get("batch_cursor", 0)
            if cursor < len(queue):
                current_from_queue = queue[cursor]
                if current_from_queue == selected_path:
                    cursor += 1
                elif selected_path in queue:
                    cursor = queue.index(selected_path) + 1

            st.session_state.batch_cursor = cursor
            if cursor < len(queue):
                st.session_state.selected_file_path = queue[cursor]
                st.rerun()
            else:
                st.session_state.batch_active = False
                st.session_state.batch_queue = []
                st.session_state.batch_cursor = 0
                st.session_state.run_mode = "standby"
                try:
                    zip_path = gerar_zip_da_saida(
                        output_root=st.session_state.get("output_root", str(Path.cwd() / "build" / "pt-BR")),
                        zip_name="traducao_ptbr.zip",
                    )
                    st.session_state.batch_zip_path = str(zip_path)
                except Exception as exc:
                    st.error("Nao foi possivel gerar o zip da traducao.")
                    st.caption(f"Detalhe tecnico: {exc}")
                st.success("Lote finalizado com sucesso.")
        else:
            st.session_state.run_mode = "standby"

    xml_output = ET.tostring(st.session_state.root, encoding="utf-8", xml_declaration=True)
    st.download_button("Baixar Traducao", xml_output, file_name=f"localizado_{display_name}")

    zip_path = st.session_state.get("batch_zip_path")
    if zip_path:
        zip_file = Path(zip_path)
        if zip_file.exists():
            st.caption(f"Pacote final: `{zip_file}`")
            with zip_file.open("rb") as f:
                st.download_button(
                    "Baixar pacote ZIP do lote",
                    data=f.read(),
                    file_name=zip_file.name,
                    mime="application/zip",
                )

    persist_run_state()


with tab_translate:
    if st.session_state.get("_resume_applied"):
        st.success("Retomando processo salvo anteriormente.")
        st.session_state._resume_applied = False
    render_translation_workspace(client, model_name, worker)
