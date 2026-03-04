from pathlib import Path


VALID_EXTENSIONS = {".stringtable", ".xml"}


def resolver_source_en_root(root_path: str) -> Path:
    """
    Resolve o diretorio fonte EN a partir de:
    - raiz localizada (ex.: .../localized)
    - pasta EN (ex.: .../localized/en)
    - pasta EN/text (ex.: .../localized/en/text)
    """
    base = Path(root_path).expanduser()
    if not base.exists() or not base.is_dir():
        raise ValueError("Diretorio invalido.")

    parts_low = [p.lower() for p in base.parts]
    if "en" in parts_low:
        # Ja estamos dentro de en (ou subpasta). Usa o caminho informado.
        return base

    en_dir = base / "en"
    if en_dir.exists() and en_dir.is_dir():
        en_text = en_dir / "text"
        return en_text if en_text.exists() and en_text.is_dir() else en_dir

    raise ValueError("Nao foi encontrada a pasta 'en' dentro do diretorio informado.")


def descobrir_arquivos_stringtable(root_path: str):
    source_en = resolver_source_en_root(root_path)

    arquivos = []
    for path in source_en.rglob("*"):
        if path.is_file() and path.suffix.lower() in VALID_EXTENSIONS:
            arquivos.append(path)

    arquivos.sort(key=lambda p: str(p).lower())
    return source_en, arquivos


def relpath_display(path_obj: Path, root_path: str) -> str:
    root = Path(root_path).expanduser()
    try:
        return str(path_obj.relative_to(root))
    except Exception:
        return str(path_obj)
