import json
import re
from pathlib import Path

GLOSSARY_TAG_PATTERN = re.compile(r"\[url=glossary:([^\]]+)\]", flags=re.IGNORECASE)
USER_GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "data" / "glossary_user.json"


def carregar_glossario_usuario():
    if not USER_GLOSSARY_PATH.exists():
        return {}
    try:
        with USER_GLOSSARY_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}
    return {}


def salvar_glossario_usuario(glossario_usuario: dict):
    USER_GLOSSARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USER_GLOSSARY_PATH.open("w", encoding="utf-8") as f:
        json.dump(glossario_usuario, f, ensure_ascii=False, indent=2, sort_keys=True)


def resetar_glossario_usuario():
    USER_GLOSSARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USER_GLOSSARY_PATH.open("w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2, sort_keys=True)


def obter_glossario_completo():
    return carregar_glossario_usuario()


def extrair_tags_glossario(texto: str):
    return [m for m in GLOSSARY_TAG_PATTERN.findall(texto or "")]


def coletar_termos_glossario_em_arquivos(file_paths: list[str]):
    termos = set()
    for file_path in file_paths:
        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for termo in extrair_tags_glossario(text):
            if termo:
                termos.add(termo.strip())
    return sorted(termos, key=lambda t: t.lower())


def coletar_contexto_termos_glossario_em_arquivos(file_paths: list[str], max_por_termo: int = 3):
    """
    Retorna contexto de uso por termo:
    {
      "Termo": [
         {"file": "...", "line": 10, "excerpt": "..."},
      ]
    }
    """
    contexto: dict[str, list[dict]] = {}
    for file_path in file_paths:
        path = Path(file_path)
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, start=1):
                    matches = GLOSSARY_TAG_PATTERN.findall(line)
                    if not matches:
                        continue
                    excerpt = " ".join((line or "").strip().split())
                    if len(excerpt) > 220:
                        excerpt = excerpt[:220] + "..."
                    for termo in matches:
                        key = (termo or "").strip()
                        if not key:
                            continue
                        bucket = contexto.setdefault(key, [])
                        if len(bucket) < max_por_termo:
                            bucket.append(
                                {
                                    "file": str(path),
                                    "line": line_no,
                                    "excerpt": excerpt,
                                }
                            )
        except Exception:
            continue
    return contexto


def verificar_termos_faltantes(texto_en: str):
    """
    Varre o texto em busca de tags [url=glossary:termo] e verifica
    se o termo original existe no glossario (ignorando maiusculas).
    """
    tags_encontradas = [t.lower() for t in extrair_tags_glossario(texto_en)]
    glossario_keys_low = {k.lower() for k in obter_glossario_completo().keys()}
    return sorted({t for t in tags_encontradas if t not in glossario_keys_low})
