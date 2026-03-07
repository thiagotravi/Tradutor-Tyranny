import logging
from pathlib import Path
import xml.etree.ElementTree as ET

import streamlit as st

from app_core.context_rules import obter_contexto_voz
from app_core.filesystem import (
    descobrir_arquivos_stringtable,
    localizar_arquivo_equivalente_por_idioma,
    relpath_display,
    resolver_source_language_root,
)
from app_core.glossary import verificar_termos_faltantes
from app_core.output_packaging import gerar_zip_da_saida, salvar_xml_traduzido
from app_core.settings import criar_client, obter_api_key, obter_model_name
from app_core.translator import (
    TranslationAPIError,
    TranslationResponseError,
    normalizar_traducao_feminina,
    processar_lote_entrada,
    sugerir_traducao_glossario,
)
from app_core.validation import validar_traducao_com_es
from app_core.run_state import load_run_state, save_run_state
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
    save_run_state(state)


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
    st.session_state._run_state_loaded = True


def render_resume_controls():
    saved = st.session_state.get("_saved_run_state", {})
    if not saved:
        return

    source_root = saved.get("source_root", "")
    selected_file = saved.get("selected_file_path")
    idx = int(saved.get("idx", 0) or 0)
    glossary_done = bool(saved.get("glossary_ready", False))
    batch_active = bool(saved.get("batch_active", False))
    st.info("Processo anterior detectado. Voce pode retomar ou reiniciar.")
    st.caption(f"EN root: `{source_root or '-'}`")
    st.caption(f"ES root: `{saved.get('source_es_root') or '-'}`")
    st.caption(f"Arquivo alvo: `{selected_file or '-'}` | Entrada: `{idx}`")
    st.caption(f"Glossario concluido: `{glossary_done}` | Lote ativo: `{batch_active}`")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Retomar de onde parou", key="resume_previous_run"):
            _aplicar_estado(saved, force=True)
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
    selected_file_saved = st.session_state.get("selected_file_path")
    if selected_file_saved:
        for rel_label, full_path in opcoes:
            if full_path == selected_file_saved:
                st.session_state["selected_relpath"] = rel_label
                break
    selected_label = st.selectbox("Arquivo para traduzir", labels, key="selected_relpath")
    selected_map = dict(opcoes)
    selected_file_path = selected_map[selected_label]

    col_start, col_stop = st.columns(2)
    with col_start:
        if st.button("Iniciar lote (arquivos filtrados)"):
            st.session_state.batch_queue = [full for _, full in opcoes]
            st.session_state.batch_cursor = 0
            st.session_state.batch_active = len(st.session_state.batch_queue) > 0
            st.session_state.batch_zip_path = ""
            st.rerun()
    with col_stop:
        if st.button("Parar lote"):
            st.session_state.batch_active = False
            st.session_state.batch_queue = []
            st.session_state.batch_cursor = 0
            st.rerun()

    if st.session_state.get("batch_active"):
        queue = st.session_state.get("batch_queue", [])
        cursor = st.session_state.get("batch_cursor", 0)
        if not queue or cursor >= len(queue):
            st.session_state.batch_active = False
            st.session_state.batch_queue = []
            st.session_state.batch_cursor = 0
            st.success("Lote concluido.")
            st.session_state.selected_file_path = selected_file_path
            return selected_file_path

        current_file = queue[cursor]
        current_rel = relpath_display(Path(current_file), source_en_root)
        st.info(f"Lote ativo: {cursor + 1}/{len(queue)} - {current_rel}")
        st.session_state.selected_file_path = current_file
        return current_file

    st.session_state.selected_file_path = selected_file_path
    return selected_file_path


def render_sidebar_progress():
    with st.sidebar:
        st.header("Progresso")
        total, concluidos, percent = st.session_state.progresso.get_stats()
        st.progress(percent / 100)
        st.write(f"**{concluidos}** de **{total}** arquivos")

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


st.set_page_config(page_title="Tyranny Localizer v0.7", layout="wide")
st.title("Estacao Seladestinos v0.7")

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

aplicar_estado_salvo_no_session()

render_sidebar_progress()
render_resume_controls()
if st.session_state.get("_resume_applied"):
    st.success("Retomando processo salvo anteriormente.")
    st.session_state._resume_applied = False
client, model_name = init_translation_client()

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

while st.session_state.idx < len(st.session_state.entries):
    idx = st.session_state.idx
    total_entries = len(st.session_state.entries)
    chunk_start = idx
    chunk_end = min(total_entries, chunk_start + BATCH_SIZE)
    st.progress(st.session_state.idx / max(total_entries, 1))
    st.caption(f"Processando lote: entradas {chunk_start + 1} a {chunk_end} de {total_entries}")

    missing_cache_indexes = [i for i in range(chunk_start, chunk_end) if i not in st.session_state.cache]
    if missing_cache_indexes:
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

    interrompido_para_revisao = False
    while st.session_state.idx < chunk_end:
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
        tem_inconsistencia = validacao["status"] in {"review", "block"}

        if res.get("confianca", 0) >= 10 and not faltantes and not tem_inconsistencia:
            if txt_node is not None:
                txt_node.text = res.get("traducao_padrao", "")
            f_node = entry.find("FemaleText")
            if f_node is not None:
                fem_final = normalizar_traducao_feminina(
                    res.get("traducao_padrao", ""),
                    res.get("traducao_feminina", ""),
                )
                f_node.text = fem_final if fem_final else None
            st.session_state.idx += 1
        else:
            st.warning(f"INTERVENCAO NECESSARIA (Entrada {idx})")
            if faltantes:
                st.error(f"Termos nao mapeados no glossario: {', '.join(faltantes)}")
            if validacao["issues"]:
                st.warning("Inconsistencias detectadas na validacao EN/ES/PT:")
                for issue in validacao["issues"]:
                    st.write(f"- [{issue['severity']}] {issue['message']}")
            if st.session_state.get("source_es_root") and not st.session_state.get("es_file_path"):
                st.info("Arquivo de referencia ES correspondente nao foi encontrado para este arquivo EN.")

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.text_area("Original (EN):", value=txt_en, height=350, disabled=True)
            with col2:
                st.text_area("Referencia (ES):", value=txt_es, height=350, disabled=True)
            with col3:
                edit_p = st.text_area(
                    "Padrao:",
                    value=res.get("traducao_padrao", ""),
                    height=350,
                    key=f"p_{idx}",
                )
            with col4:
                sugestao_fem = normalizar_traducao_feminina(
                    res.get("traducao_padrao", ""),
                    res.get("traducao_feminina", ""),
                )
                edit_f = st.text_area(
                    "Feminino:",
                    value=sugestao_fem,
                    height=350,
                    key=f"f_{idx}",
                )

            if st.button("Aprovar Entrada", key=f"approve_{idx}"):
                if txt_node is not None:
                    txt_node.text = edit_p
                f_node = entry.find("FemaleText")
                if f_node is not None:
                    fem_final = normalizar_traducao_feminina(edit_p, edit_f)
                    f_node.text = fem_final if fem_final else None
                st.session_state.idx += 1
                st.rerun()

            interrompido_para_revisao = True
            break

    if interrompido_para_revisao:
        break
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
