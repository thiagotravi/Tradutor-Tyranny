from pathlib import Path


VALID_EXTENSIONS = {".stringtable", ".xml"}
EXCLUDED_FILENAMES = {"language.xml"}


def resolver_source_language_root(root_path: str, language: str) -> Path:
    """
    Resolve o diretorio fonte por idioma a partir de:
    - raiz localizada (ex.: .../localized)
    - pasta de idioma (ex.: .../localized/en)
    - pasta de idioma/text (ex.: .../localized/en/text)
    """
    base = Path(root_path).expanduser()
    if not base.exists() or not base.is_dir():
        raise ValueError("Diretorio invalido.")

    lang = (language or "").strip().lower()
    if not lang:
        raise ValueError("Idioma invalido para resolucao do diretorio.")

    parts_low = [p.lower() for p in base.parts]
    if lang in parts_low:
        # Ja estamos dentro do idioma (ou subpasta). Usa o caminho informado.
        return base

    lang_dir = base / lang
    if lang_dir.exists() and lang_dir.is_dir():
        lang_text = lang_dir / "text"
        return lang_text if lang_text.exists() and lang_text.is_dir() else lang_dir

    raise ValueError(f"Nao foi encontrada a pasta '{lang}' dentro do diretorio informado.")


def resolver_source_en_root(root_path: str) -> Path:
    return resolver_source_language_root(root_path, "en")


def descobrir_arquivos_stringtable(root_path: str):
    source_en = resolver_source_en_root(root_path)

    arquivos = []
    for path in source_en.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in VALID_EXTENSIONS
            and path.name.lower() not in EXCLUDED_FILENAMES
        ):
            arquivos.append(path)

    arquivos.sort(key=lambda p: str(p).lower())
    return source_en, arquivos


def localizar_arquivo_equivalente_por_idioma(
    source_file: str,
    source_root_origem: str,
    source_root_destino: str,
) -> Path | None:
    origem = Path(source_file)
    root_origem = Path(source_root_origem)
    root_destino = Path(source_root_destino)
    try:
        rel = origem.relative_to(root_origem)
    except Exception:
        return None

    candidates = [root_destino / rel]

    # Fallback 1: se origem usa ".../text" e destino nao, tenta remover prefixo "text".
    rel_parts_low = [p.lower() for p in rel.parts]
    if rel_parts_low and rel_parts_low[0] == "text":
        candidates.append(root_destino / Path(*rel.parts[1:]))
    else:
        # Fallback 2: se destino usar ".../text", tenta adicionar "text" no inicio.
        candidates.append(root_destino / "text" / rel)

    for candidato in candidates:
        if candidato.exists() and candidato.is_file():
            return candidato
    return None


def relpath_display(path_obj: Path, root_path: str) -> str:
    root = Path(root_path).expanduser()
    try:
        return str(path_obj.relative_to(root))
    except Exception:
        return str(path_obj)
