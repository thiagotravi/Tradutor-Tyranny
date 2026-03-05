import re


MECHANIC_HINTS = {
    "damage",
    "armor",
    "health",
    "accuracy",
    "defense",
    "attack",
    "dodge",
    "parry",
    "critical",
    "weapon",
    "ability",
    "skill",
    "spell",
}

ORNATE_MECHANIC_WORDS_PT = {
    "flagelo",
    "moroso",
    "celeridade",
}

COMMON_CAPITALIZED_EXCLUSIONS = {
    "I",
    "The",
    "A",
    "An",
    "This",
    "That",
    "These",
    "Those",
}


def _extract_placeholders(text: str):
    return re.findall(r"\{[0-9]+\}", text or "")


def _extract_glossary_tags(text: str):
    return re.findall(r"\[url=glossary:([^\]]+)\]", text or "", flags=re.IGNORECASE)


def _extract_capitalized_tokens(text: str):
    return set(re.findall(r"\b[A-Z][A-Za-z0-9'\-]+\b", text or ""))


def _has_mechanic_context(text_en: str, text_es: str):
    joined = f"{text_en or ''} {text_es or ''}".lower()
    return any(hint in joined for hint in MECHANIC_HINTS)


def validar_traducao_com_es(texto_en: str, texto_es: str, texto_pt: str):
    """
    Valida traducao PT-BR usando EN como base e ES como referencia auxiliar.
    Retorna dicionario com status e lista de issues.
    """
    issues = []
    en = texto_en or ""
    es = texto_es or ""
    pt = texto_pt or ""

    placeholders_en = _extract_placeholders(en)
    placeholders_pt = _extract_placeholders(pt)
    if placeholders_en != placeholders_pt:
        issues.append(
            {
                "code": "placeholder_mismatch",
                "severity": "block",
                "message": "Placeholders divergentes entre EN e PT.",
            }
        )

    tags_en = [t.lower() for t in _extract_glossary_tags(en)]
    tags_pt = [t.lower() for t in _extract_glossary_tags(pt)]
    if tags_en != tags_pt:
        issues.append(
            {
                "code": "glossary_tag_mismatch",
                "severity": "block",
                "message": "Tags de glossario divergentes entre EN e PT.",
            }
        )

    if en.count("\\n") != pt.count("\\n"):
        issues.append(
            {
                "code": "linebreak_mismatch",
                "severity": "block",
                "message": "Quantidade de quebras de linha difere entre EN e PT.",
            }
        )

    if en.count('"') != pt.count('"'):
        issues.append(
            {
                "code": "quote_mismatch",
                "severity": "block",
                "message": 'Quantidade de aspas duplas (") difere entre EN e PT.',
            }
        )

    if re.search(r"[“”„‟«»]", pt):
        issues.append(
            {
                "code": "non_ascii_quotes",
                "severity": "block",
                "message": 'Aspas tipograficas detectadas no PT. Use apenas aspas duplas ASCII (").',
            }
        )

    if es.strip():
        # Sinaliza possivel traducao indevida de nomes proprios:
        # token capitalizado presente em EN e ES, ausente em PT.
        proper_candidates = _extract_capitalized_tokens(en).intersection(_extract_capitalized_tokens(es))
        proper_candidates = {t for t in proper_candidates if t not in COMMON_CAPITALIZED_EXCLUSIONS}
        missing_in_pt = [t for t in sorted(proper_candidates) if t.lower() not in pt.lower()]
        if missing_in_pt:
            issues.append(
                {
                    "code": "proper_name_changed",
                    "severity": "review",
                    "message": f"Possivel traducao indevida de nome proprio: {', '.join(missing_in_pt[:5])}",
                }
            )

    if _has_mechanic_context(en, es):
        ornate_hits = [w for w in ORNATE_MECHANIC_WORDS_PT if re.search(rf"\b{re.escape(w)}\b", pt, re.IGNORECASE)]
        if ornate_hits:
            issues.append(
                {
                    "code": "ornate_mechanic_term",
                    "severity": "review",
                    "message": f"Termos rebuscados em contexto mecanico: {', '.join(sorted(ornate_hits))}",
                }
            )

    has_block = any(i["severity"] == "block" for i in issues)
    status = "block" if has_block else ("review" if issues else "ok")
    return {"status": status, "issues": issues}
