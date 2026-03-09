from pathlib import Path
import xml.etree.ElementTree as ET


VALID_EXTENSIONS = {".stringtable", ".xml"}
EXCLUDED_FILENAMES = {"language.xml"}


def discover_target_files(target_root: str):
    root = Path(target_root).expanduser()
    if not root.exists() or not root.is_dir():
        raise ValueError("Diretorio de traducao (build) invalido.")
    files = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS and p.name.lower() not in EXCLUDED_FILENAMES:
            files.append(p)
    files.sort(key=lambda x: str(x).lower())
    return root, files


def rel_key(path_obj: Path, root: Path) -> str:
    try:
        return str(path_obj.relative_to(root))
    except Exception:
        return str(path_obj)


def load_entries_from_file(file_path: str):
    tree = ET.parse(file_path)
    root = tree.getroot()
    entries = root.findall(".//Entry")
    return tree, root, entries


def get_entry_text(entry, tag: str):
    node = entry.find(tag)
    return node.text if node is not None and node.text else ""


def save_entry_in_target_file(target_file: str, entry_idx: int, default_text: str, female_text: str):
    tree, _, entries = load_entries_from_file(target_file)
    if entry_idx < 0 or entry_idx >= len(entries):
        raise ValueError("Indice de entrada invalido para salvar auditoria.")
    entry = entries[entry_idx]
    def_node = entry.find("DefaultText")
    if def_node is not None:
        def_node.text = default_text or ""
    fem_node = entry.find("FemaleText")
    if fem_node is not None:
        fem_node.text = (female_text or "").strip() or None
    tree.write(target_file, encoding="utf-8", xml_declaration=True)
