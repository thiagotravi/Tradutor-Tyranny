import json
import time
from datetime import datetime, timezone

from app_core.glossary import obter_glossario_completo
from app_core.settings import criar_client, obter_api_key, obter_model_name
from app_core.translator import extrair_json_resposta
from audit_core.text_sanitize import strip_bidi_controls


CHECK_IDS = ("tags", "quotes", "glossary", "female", "brackets")
DEFAULT_API_RETRY_ATTEMPTS = 3
DEFAULT_API_RETRY_BASE_DELAY_S = 1.0


def _build_prompt(
    text_en: str,
    text_ref: str,
    text_pt: str,
    text_female: str,
    ref_language: str,
    glossary: dict,
) -> str:
    return f"""
Voce e um auditor de localizacao PT-BR para o jogo Tyranny.

Voce deve avaliar a entrada traduzida com 5 criterios:
1) tags: uso correto de tags comparando EN, referencia ({ref_language}) e PT
2) quotes: quantidade e posicao de aspas duplas ASCII (")
3) glossary: aderencia aos termos do glossario fornecido
4) female: uso correto de FemaleText (preencher apenas quando houver variacao real de genero)
5) brackets: uso correto de colchetes [] para a engine do jogo

Instrucoes:
- Considere EN como fonte principal.
- Use referencia ({ref_language}) apenas como apoio de contexto.
- Nao invente regras novas fora desses 5 criterios.
- Seja objetivo e tecnico.

GLOSSARIO:
{json.dumps(glossary, ensure_ascii=False, indent=2)}

Retorne APENAS JSON no formato:
{{
  "overall_status": "pass|review|fail",
  "checks": [
    {{"id":"tags","status":"pass|fail","details":"..."}},
    {{"id":"quotes","status":"pass|fail","details":"..."}},
    {{"id":"glossary","status":"pass|fail","details":"..."}},
    {{"id":"female","status":"pass|fail","details":"..."}},
    {{"id":"brackets","status":"pass|fail","details":"..."}}
  ],
  "summary": "resumo curto",
  "suggested_fix_pt": "texto sugerido para DefaultText (string vazia se nao precisar)",
  "suggested_fix_female": "texto sugerido para FemaleText (string vazia se nao precisar)"
}}

<english>
{text_en or ""}
</english>
<reference language="{ref_language}">
{text_ref or ""}
</reference>
<pt_default>
{text_pt or ""}
</pt_default>
<pt_female>
{text_female or ""}
</pt_female>
"""


def _normalize_check(item) -> dict:
    if not isinstance(item, dict):
        return {"id": "", "status": "fail", "details": "Item de check invalido."}
    check_id = str(item.get("id", "")).strip().lower()
    status = str(item.get("status", "fail")).strip().lower()
    details = str(item.get("details", "")).strip()
    if check_id not in CHECK_IDS:
        check_id = ""
    if status not in {"pass", "fail"}:
        status = "fail"
    return {"id": check_id, "status": status, "details": details}


def _normalize_response(data) -> dict:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    checks_raw = data.get("checks", [])
    checks = []
    if isinstance(checks_raw, list):
        checks = [_normalize_check(c) for c in checks_raw]

    checks_by_id = {c["id"]: c for c in checks if c.get("id")}
    fixed_checks = []
    for check_id in CHECK_IDS:
        item = checks_by_id.get(check_id)
        if not item:
            item = {
                "id": check_id,
                "status": "fail",
                "details": "Modelo nao retornou avaliacao para este criterio.",
            }
        fixed_checks.append(item)

    overall = str(data.get("overall_status", "review")).strip().lower()
    if overall not in {"pass", "review", "fail"}:
        overall = "review"
    if any(c["status"] == "fail" for c in fixed_checks) and overall == "pass":
        overall = "review"

    return {
        "overall_status": overall,
        "checks": fixed_checks,
        "summary": str(data.get("summary", "")).strip(),
        "suggested_fix_pt": str(data.get("suggested_fix_pt", "")).strip(),
        "suggested_fix_female": str(data.get("suggested_fix_female", "")).strip(),
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }


def _call_model_json(client, model_name: str, prompt: str):
    last_exc = None
    response = None
    for attempt in range(1, DEFAULT_API_RETRY_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= DEFAULT_API_RETRY_ATTEMPTS:
                raise RuntimeError(
                    f"Falha ao comunicar com Gemini apos {DEFAULT_API_RETRY_ATTEMPTS} tentativas."
                ) from exc
            delay_s = DEFAULT_API_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            time.sleep(delay_s)
    if response is None:
        raise RuntimeError(f"Falha ao comunicar com Gemini: {last_exc}")
    txt_json = extrair_json_resposta(getattr(response, "text", ""))
    return json.loads(txt_json)


def _call_model_text(client, model_name: str, prompt: str) -> str:
    last_exc = None
    response = None
    for attempt in range(1, DEFAULT_API_RETRY_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= DEFAULT_API_RETRY_ATTEMPTS:
                raise RuntimeError(
                    f"Falha ao comunicar com Gemini apos {DEFAULT_API_RETRY_ATTEMPTS} tentativas."
                ) from exc
            delay_s = DEFAULT_API_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            time.sleep(delay_s)
    if response is None:
        raise RuntimeError(f"Falha ao comunicar com Gemini: {last_exc}")
    return str(getattr(response, "text", "") or "").strip()


def build_audit_report_text(result: dict) -> str:
    result = result or {}
    lines = []
    lines.append(f"Status geral: {result.get('overall_status', 'review')}")
    checks = result.get("checks", [])
    for c in checks:
        check_id = c.get("id", "")
        status = c.get("status", "fail")
        details = c.get("details", "")
        lines.append(f"- {check_id}: {status}")
        if details:
            lines.append(f"  {details}")
    summary = (result.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append("Resumo:")
        lines.append(summary)
    suggestion = (result.get("suggested_fix_pt") or "").strip()
    if suggestion:
        lines.append("")
        lines.append("Sugestao de ajuste (DefaultText):")
        lines.append(suggestion)
    suggestion_female = (result.get("suggested_fix_female") or "").strip()
    if suggestion_female:
        lines.append("")
        lines.append("Sugestao de ajuste (FemaleText):")
        lines.append(suggestion_female)
    return "\n".join(lines).strip()


def validate_entry_with_gemini(
    text_en: str,
    text_ref: str,
    text_pt: str,
    text_female: str,
    ref_language: str = "es",
) -> dict:
    api_key = obter_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY nao configurada.")
    client = criar_client(api_key)
    model_name = obter_model_name()
    clean_en = strip_bidi_controls(text_en or "")
    clean_ref = strip_bidi_controls(text_ref or "")
    clean_pt = strip_bidi_controls(text_pt or "")
    clean_female = strip_bidi_controls(text_female or "")
    prompt = _build_prompt(
        text_en=clean_en,
        text_ref=clean_ref,
        text_pt=clean_pt,
        text_female=clean_female,
        ref_language=(ref_language or "es").lower(),
        glossary=obter_glossario_completo(),
    )
    data = _call_model_json(client, model_name, prompt)
    return _normalize_response(data)


def ask_gemini_audit_chat(
    question: str,
    text_en: str,
    text_ref: str,
    text_pt: str,
    text_female: str,
    ref_language: str = "es",
    history: list | None = None,
) -> str:
    api_key = obter_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY nao configurada.")
    client = criar_client(api_key)
    model_name = obter_model_name()

    history = history or []
    history_lines = []
    for msg in history[-8:]:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user")).strip().lower()
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        role_label = "Usuario" if role == "user" else "Assistente"
        history_lines.append(f"{role_label}: {content}")
    history_block = "\n".join(history_lines).strip()

    clean_en = strip_bidi_controls(text_en or "")
    clean_ref = strip_bidi_controls(text_ref or "")
    clean_pt = strip_bidi_controls(text_pt or "")
    clean_female = strip_bidi_controls(text_female or "")
    clean_question = strip_bidi_controls(question or "")

    prompt = f"""
Voce e um assistente de auditoria de traducao PT-BR para Tyranny.
Ajude o usuario a decidir a melhor traducao para a entrada atual.

Regras:
- Use EN como fonte principal.
- Use referencia ({(ref_language or "es").lower()}) apenas para comparacao de contexto.
- Considere o texto PT atual e, quando existir, FemaleText.
- Aponte riscos de tags, aspas, glossario e colchetes quando relevante.
- Responda em PT-BR de forma objetiva e pratica.

Contexto da entrada:
<english>
{clean_en}
</english>
<reference language="{(ref_language or "es").lower()}">
{clean_ref}
</reference>
<pt_default>
{clean_pt}
</pt_default>
<pt_female>
{clean_female}
</pt_female>

Historico recente:
{history_block or "(sem historico)"}

Pergunta do usuario:
{clean_question}
"""
    return _call_model_text(client, model_name, prompt)
