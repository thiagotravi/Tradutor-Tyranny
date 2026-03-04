import re
from config_traducao import GLOSSARIO


def verificar_termos_faltantes(texto_en: str):
    """
    Varre o texto em busca de tags [url=glossary:termo] e verifica
    se o termo original existe no glossario (ignorando maiusculas).
    """
    tags_encontradas = re.findall(r"\[url=glossary:([^\]]+)\]", (texto_en or "").lower())
    glossario_keys_low = {k.lower() for k in GLOSSARIO.keys()}
    return sorted({t for t in tags_encontradas if t not in glossario_keys_low})
