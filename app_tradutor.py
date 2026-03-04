import logging
from pathlib import Path
import xml.etree.ElementTree as ET

import streamlit as st

from app_core.context_rules import obter_contexto_voz
from app_core.filesystem import (
    descobrir_arquivos_stringtable,
    relpath_display,
)
from app_core.glossary import verificar_termos_faltantes
from app_core.settings import criar_client, obter_api_key, obter_model_name
from app_core.translator import (
    TranslationAPIError,
    TranslationResponseError,
    normalizar_traducao_feminina,
    processar_entrada,
)
from translation_progress import ProgressManager

logger = logging.getLogger(__name__)


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
    selected_label = st.selectbox("Arquivo para traduzir", labels, key="selected_relpath")
    selected_map = dict(opcoes)
    selected_file_path = selected_map[selected_label]

    col_start, col_stop = st.columns(2)
    with col_start:
        if st.button("Iniciar lote (arquivos filtrados)"):
            st.session_state.batch_queue = [full for _, full in opcoes]
            st.session_state.batch_cursor = 0
            st.session_state.batch_active = len(st.session_state.batch_queue) > 0
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

render_sidebar_progress()
client, model_name = init_translation_client()

source_mode = st.radio(
    "Modo de entrada",
    options=["Diretorio", "Arquivo unico (legado)"],
    horizontal=True,
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
    st.stop()

if st.session_state.get("last_target") != target_id or "tree" not in st.session_state:
    tree, root, display_name, progress_key = load_tree_from_target(
        source_mode,
        selected_path,
        uploaded_file,
        st.session_state.get("source_en_root") or st.session_state.get("source_root", ""),
    )
    st.session_state.tree = tree
    st.session_state.root = root
    st.session_state.entries = root.findall(".//Entry")
    st.session_state.progress_key = progress_key
    reset_translation_state(target_id)
else:
    if source_mode == "Diretorio":
        display_name = Path(selected_path).name
    else:
        display_name = uploaded_file.name

while st.session_state.idx < len(st.session_state.entries):
    idx = st.session_state.idx
    entry = st.session_state.entries[idx]
    txt_node = entry.find("DefaultText")
    txt_en = txt_node.text if txt_node is not None and txt_node.text else ""
    faltantes = verificar_termos_faltantes(txt_en)
    instrucoes_dinamicas = obter_contexto_voz(display_name)

    if idx not in st.session_state.cache:
        with st.spinner(f"Processando entrada {idx}..."):
            try:
                st.session_state.cache[idx] = processar_entrada(
                    client=client,
                    model_name=model_name,
                    texto_en=txt_en,
                    instrucoes_voz=instrucoes_dinamicas,
                )
            except TranslationAPIError as exc:
                logger.exception("Erro de API na entrada %s", idx)
                st.error("Falha ao chamar o Gemini para traduzir esta entrada.")
                st.caption(f"Detalhe tecnico: {exc}")
                if st.button("Tentar novamente", key=f"retry_api_{idx}"):
                    st.rerun()
                st.stop()
            except TranslationResponseError as exc:
                logger.exception("Resposta invalida na entrada %s", idx)
                st.error("A resposta do Gemini veio em formato inesperado.")
                st.caption(f"Detalhe tecnico: {exc}")
                if st.button("Tentar novamente", key=f"retry_json_{idx}"):
                    st.rerun()
                st.stop()

    res = st.session_state.cache[idx]
    if res.get("confianca", 0) >= 10 and not faltantes:
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

        col1, col2, col3 = st.columns(3)
        with col1:
            st.text_area("Original (EN):", value=txt_en, height=350, disabled=True)
        with col2:
            edit_p = st.text_area(
                "Padrao:",
                value=res.get("traducao_padrao", ""),
                height=350,
                key=f"p_{idx}",
            )
        with col3:
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
        break
else:
    st.success("Arquivo finalizado!")
    st.session_state.progresso.update_status(st.session_state.progress_key, True)
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
            st.success("Lote finalizado com sucesso.")

xml_output = ET.tostring(st.session_state.root, encoding="utf-8", xml_declaration=True)
st.download_button("Baixar Traducao", xml_output, file_name=f"localizado_{display_name}")
