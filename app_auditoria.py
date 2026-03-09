from pathlib import Path
import hashlib

import streamlit as st

from audit_core.filemap import (
    discover_target_files,
    get_entry_text,
    load_entries_from_file,
    rel_key,
    save_entry_in_target_file,
)
from audit_core.gemini_audit import build_audit_report_text, validate_entry_with_gemini
from audit_core.gemini_audit import ask_gemini_audit_chat
from audit_core.state import load_post_audit_state, save_post_audit_state


def _status_key(file_rel: str, entry_idx: int) -> str:
    return f"{file_rel}::{entry_idx}"


def _init_state_defaults():
    defaults = {
        "audit_source_en_root": "",
        "audit_source_ref_root": "",
        "audit_target_root": "",
        "audit_ref_language": "es",
        "audit_discovered_rel_files": [],
        "audit_file_idx": 0,
        "audit_entry_idx": 0,
        "audit_entry_status": {},
        "audit_validation_results": {},
        "audit_chat_history": {},
        "audit_editor_rev": {},
        "audit_loaded_once": False,
        "audit_scan_done": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _apply_saved_state_once():
    if st.session_state.get("audit_loaded_once"):
        return
    saved = load_post_audit_state()
    if saved:
        st.session_state.audit_source_en_root = saved.get("source_en_root", "")
        st.session_state.audit_source_ref_root = saved.get("source_ref_root", "")
        st.session_state.audit_target_root = saved.get("target_root", "")
        st.session_state.audit_ref_language = saved.get("ref_language", "es")
        st.session_state.audit_discovered_rel_files = saved.get("discovered_rel_files", [])
        st.session_state.audit_file_idx = int(saved.get("file_idx", 0) or 0)
        st.session_state.audit_entry_idx = int(saved.get("entry_idx", 0) or 0)
        st.session_state.audit_entry_status = saved.get("entry_status", {}) or {}
        st.session_state.audit_validation_results = saved.get("validation_results", {}) or {}
        st.session_state.audit_chat_history = saved.get("chat_history", {}) or {}
        st.session_state.audit_scan_done = bool(st.session_state.audit_discovered_rel_files)
    st.session_state.audit_loaded_once = True


def _persist_state():
    payload = {
        "schema": "v1",
        "source_en_root": st.session_state.get("audit_source_en_root", ""),
        "source_ref_root": st.session_state.get("audit_source_ref_root", ""),
        "target_root": st.session_state.get("audit_target_root", ""),
        "ref_language": st.session_state.get("audit_ref_language", "es"),
        "discovered_rel_files": st.session_state.get("audit_discovered_rel_files", []),
        "file_idx": int(st.session_state.get("audit_file_idx", 0) or 0),
        "entry_idx": int(st.session_state.get("audit_entry_idx", 0) or 0),
        "entry_status": st.session_state.get("audit_entry_status", {}),
        "validation_results": st.session_state.get("audit_validation_results", {}),
        "chat_history": st.session_state.get("audit_chat_history", {}),
    }
    save_post_audit_state(payload)


def _entry_hash(text_en: str, text_ref: str, text_pt: str, text_fem: str) -> str:
    payload = "\n||\n".join([text_en or "", text_ref or "", text_pt or "", text_fem or ""])
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()


def _editor_key(base: str, entry_key: str) -> str:
    rev_map = st.session_state.get("audit_editor_rev", {}) or {}
    rev = int(rev_map.get(entry_key, 0) or 0)
    return f"{base}_{entry_key}_{rev}"


def _resolve_source_path(root_str: str, rel: str, language: str | None = None) -> Path:
    root = Path(root_str or "").expanduser()
    if not root_str:
        return root / rel

    lang = (language or "").strip().lower()
    candidates = [root / rel, root / "text" / rel]

    if root.name.lower() == "localized":
        if lang:
            candidates.append(root / lang / "text" / rel)
            candidates.append(root / lang / rel)
    elif root.parent.name.lower() == "localized":
        candidates.append(root / "text" / rel)

    seen = set()
    for cand in candidates:
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        if cand.exists():
            return cand

    return candidates[0]


def _go_next_entry(total_entries: int, total_files: int):
    file_idx = int(st.session_state.get("audit_file_idx", 0) or 0)
    entry_idx = int(st.session_state.get("audit_entry_idx", 0) or 0)
    if entry_idx + 1 < total_entries:
        st.session_state.audit_entry_idx = entry_idx + 1
    elif file_idx + 1 < total_files:
        st.session_state.audit_file_idx = file_idx + 1
        st.session_state.audit_entry_idx = 0
    _persist_state()


def _go_prev_entry():
    file_idx = int(st.session_state.get("audit_file_idx", 0) or 0)
    entry_idx = int(st.session_state.get("audit_entry_idx", 0) or 0)
    if entry_idx > 0:
        st.session_state.audit_entry_idx = entry_idx - 1
    elif file_idx > 0:
        st.session_state.audit_file_idx = file_idx - 1
        st.session_state.audit_entry_idx = 0
    _persist_state()


st.set_page_config(page_title="Auditoria Pós-Tradução", layout="wide")
st.title("Auditoria Pós-Tradução")

_init_state_defaults()
_apply_saved_state_once()

en_root = st.text_input(
    "Diretorio EN (source)",
    value=st.session_state.get("audit_source_en_root", ""),
)
ref_language = st.text_input(
    "Idioma de referencia (ex.: es)",
    value=st.session_state.get("audit_ref_language", "es"),
)
ref_root = st.text_input(
    "Diretorio de referencia (opcional)",
    value=st.session_state.get("audit_source_ref_root", ""),
)
target_root = st.text_input(
    "Diretorio de traducao (build/pt-BR)",
    value=st.session_state.get("audit_target_root", ""),
)

if st.button("Carregar Auditoria"):
    st.session_state.audit_source_en_root = en_root.strip()
    st.session_state.audit_ref_language = (ref_language.strip() or "es").lower()
    st.session_state.audit_source_ref_root = ref_root.strip()
    st.session_state.audit_target_root = target_root.strip()
    try:
        target_base, target_files = discover_target_files(st.session_state.audit_target_root)
        rels = [rel_key(p, target_base) for p in target_files]
        st.session_state.audit_discovered_rel_files = rels
        st.session_state.audit_scan_done = True
        st.session_state.audit_file_idx = min(
            int(st.session_state.get("audit_file_idx", 0) or 0),
            max(0, len(rels) - 1),
        )
        st.session_state.audit_entry_idx = max(0, int(st.session_state.get("audit_entry_idx", 0) or 0))
        _persist_state()
        st.success(f"Arquivos de tradução carregados: {len(rels)}")
    except Exception as exc:
        st.error(f"Falha ao carregar auditoria: {exc}")

rels = st.session_state.get("audit_discovered_rel_files", [])
if not rels:
    st.info("Configure os diretórios e clique em 'Carregar Auditoria'.")
    st.stop()

entry_status = st.session_state.get("audit_entry_status", {})
approved_count = sum(1 for v in entry_status.values() if v == "approved")
skipped_count = sum(1 for v in entry_status.values() if v == "skipped")
st.caption(f"Entradas aprovadas: {approved_count} | puladas: {skipped_count}")

file_idx = int(st.session_state.get("audit_file_idx", 0) or 0)
file_idx = max(0, min(file_idx, len(rels) - 1))
st.session_state.audit_file_idx = file_idx
rel = rels[file_idx]

target_path = Path(st.session_state.audit_target_root) / rel
en_path = _resolve_source_path(
    st.session_state.audit_source_en_root,
    rel,
    language="en",
)
ref_path = (
    _resolve_source_path(
        st.session_state.audit_source_ref_root,
        rel,
        language=st.session_state.audit_ref_language,
    )
    if st.session_state.audit_source_ref_root
    else None
)

if not target_path.exists():
    st.error(f"Arquivo alvo nao encontrado em build para auditoria: `{rel}`")
    st.stop()

_, _, target_entries = load_entries_from_file(str(target_path))
en_entries = []
if en_path.exists():
    _, _, en_entries = load_entries_from_file(str(en_path))
else:
    st.warning(
        f"Arquivo EN ausente para `{rel}`. Auditoria segue em modo parcial (sem contexto EN)."
    )
ref_entries = None
if ref_path and ref_path.exists():
    _, _, ref_entries = load_entries_from_file(str(ref_path))

entry_idx = int(st.session_state.get("audit_entry_idx", 0) or 0)
entry_idx = max(0, min(entry_idx, max(0, len(target_entries) - 1)))
st.session_state.audit_entry_idx = entry_idx

st.subheader(f"Arquivo {file_idx + 1}/{len(rels)}")
st.caption(f"`{rel}`")
st.caption(f"Entrada {entry_idx + 1}/{len(target_entries)}")

en_entry = en_entries[entry_idx] if entry_idx < len(en_entries) else None
target_entry = target_entries[entry_idx]
ref_entry = ref_entries[entry_idx] if ref_entries and entry_idx < len(ref_entries) else None

txt_en = get_entry_text(en_entry, "DefaultText") if en_entry is not None else ""
txt_ref = get_entry_text(ref_entry, "DefaultText") if ref_entry is not None else ""
txt_pt = get_entry_text(target_entry, "DefaultText")
txt_fem = get_entry_text(target_entry, "FemaleText")

col_en, col_ref, col_pt = st.columns(3)
with col_en:
    st.text_area("Original (EN)", value=txt_en, height=320, disabled=True)
with col_ref:
    st.text_area(f"Referencia ({st.session_state.audit_ref_language.upper()})", value=txt_ref, height=320, disabled=True)
with col_pt:
    edit_pt = st.text_area(
        "Tradução (PT)",
        value=txt_pt,
        height=220,
        key=_editor_key("audit_pt", _status_key(rel, entry_idx)),
    )
    edit_fem = st.text_area(
        "Tradução feminina (PT) - opcional",
        value=txt_fem,
        height=90,
        key=_editor_key("audit_fem", _status_key(rel, entry_idx)),
    )

status_now = entry_status.get(_status_key(rel, entry_idx), "pending")
st.caption(f"Status atual da entrada: `{status_now}`")

validation_results = st.session_state.get("audit_validation_results", {})
entry_key = _status_key(rel, entry_idx)
entry_validation = validation_results.get(entry_key, {})
current_hash = _entry_hash(txt_en, txt_ref, edit_pt, edit_fem)
validated_hash = str(entry_validation.get("input_hash", ""))
is_validation_stale = bool(entry_validation) and validated_hash != current_hash

st.divider()
st.subheader("Validação Gemini (5 critérios)")
col_validate, col_apply, col_hint = st.columns([1, 1, 2])
with col_validate:
    if st.button("Revalidar com Gemini"):
        try:
            with st.spinner("Executando validação com Gemini..."):
                result = validate_entry_with_gemini(
                    text_en=txt_en,
                    text_ref=txt_ref,
                    text_pt=edit_pt,
                    text_female=edit_fem,
                    ref_language=st.session_state.audit_ref_language,
                )
            result["input_hash"] = current_hash
            validation_results[entry_key] = result
            st.session_state.audit_validation_results = validation_results
            _persist_state()
            st.success("Validação concluída e salva.")
            st.rerun()
        except Exception as exc:
            st.error(f"Falha na validação Gemini: {exc}")
with col_apply:
    can_apply_suggestion = bool(
        entry_validation
        and (
            str(entry_validation.get("suggested_fix_pt", "")).strip()
            or str(entry_validation.get("suggested_fix_female", "")).strip()
        )
    )
    if st.button("Aplicar sugestão", disabled=not can_apply_suggestion):
        suggested_pt = str(entry_validation.get("suggested_fix_pt", "")).strip()
        suggested_fem = str(entry_validation.get("suggested_fix_female", "")).strip()
        final_pt = suggested_pt if suggested_pt else edit_pt
        final_fem = suggested_fem if suggested_fem else edit_fem
        try:
            save_entry_in_target_file(str(target_path), entry_idx, final_pt, final_fem)
            # Evita mutacao de key de widget instanciado no mesmo ciclo.
            rev_map = st.session_state.get("audit_editor_rev", {}) or {}
            rev_map[entry_key] = int(rev_map.get(entry_key, 0) or 0) + 1
            st.session_state.audit_editor_rev = rev_map
            st.success("Sugestão aplicada e salva no arquivo de tradução.")
            st.rerun()
        except Exception as exc:
            st.error(f"Falha ao aplicar sugestão: {exc}")
with col_hint:
    if not entry_validation:
        st.caption("Sem validação salva para esta entrada.")
    elif is_validation_stale:
        st.warning("A validação salva está desatualizada para o texto atual. Revalide.")
    else:
        st.caption("Validação salva está alinhada com o texto atual.")

if entry_validation:
    report_text = build_audit_report_text(entry_validation)
    st.text_area(
        "Relatório de checagem (pass/fail por critério)",
        value=report_text,
        height=230,
        disabled=True,
        key=f"audit_report_{rel}_{entry_idx}",
    )

st.divider()
st.subheader("Chat contextual com Gemini")
chat_all = st.session_state.get("audit_chat_history", {})
entry_chat = list(chat_all.get(entry_key, []))
if not isinstance(entry_chat, list):
    entry_chat = []
for msg in entry_chat[-12:]:
    if not isinstance(msg, dict):
        continue
    role = str(msg.get("role", "assistant")).strip().lower()
    content = str(msg.get("content", "")).strip()
    if not content:
        continue
    with st.chat_message("user" if role == "user" else "assistant"):
        st.write(content)

if st.button("Limpar conversa desta entrada"):
    chat_all[entry_key] = []
    st.session_state.audit_chat_history = chat_all
    _persist_state()
    st.rerun()

chat_question = st.chat_input("Pergunte ao Gemini sobre esta entrada")
if chat_question:
    entry_chat.append({"role": "user", "content": chat_question})
    try:
        with st.spinner("Consultando Gemini..."):
            answer = ask_gemini_audit_chat(
                question=chat_question,
                text_en=txt_en,
                text_ref=txt_ref,
                text_pt=edit_pt,
                text_female=edit_fem,
                ref_language=st.session_state.audit_ref_language,
                history=entry_chat,
            )
        entry_chat.append({"role": "assistant", "content": answer or "Sem resposta textual do modelo."})
    except Exception as exc:
        entry_chat.append({"role": "assistant", "content": f"Falha na consulta ao Gemini: {exc}"})
    chat_all[entry_key] = entry_chat[-20:]
    st.session_state.audit_chat_history = chat_all
    _persist_state()
    st.rerun()

col_prev, col_save, col_approve, col_skip, col_next = st.columns(5)
with col_prev:
    if st.button("Entrada anterior"):
        _go_prev_entry()
        st.rerun()
with col_save:
    if st.button("Salvar edição"):
        save_entry_in_target_file(str(target_path), entry_idx, edit_pt, edit_fem)
        st.success("Edição salva no arquivo de tradução.")
with col_approve:
    if st.button("Aprovar e próxima"):
        save_entry_in_target_file(str(target_path), entry_idx, edit_pt, edit_fem)
        entry_status[_status_key(rel, entry_idx)] = "approved"
        st.session_state.audit_entry_status = entry_status
        _go_next_entry(len(target_entries), len(rels))
        st.rerun()
with col_skip:
    if st.button("Pular"):
        entry_status[_status_key(rel, entry_idx)] = "skipped"
        st.session_state.audit_entry_status = entry_status
        _go_next_entry(len(target_entries), len(rels))
        st.rerun()
with col_next:
    if st.button("Próxima entrada"):
        _go_next_entry(len(target_entries), len(rels))
        st.rerun()

_persist_state()
