import json
from config_traducao import GLOSSARIO, GUIA_ESTILO


class TranslationError(Exception):
    """Erro base de traducao."""


class TranslationAPIError(TranslationError):
    """Falha na chamada do modelo."""


class TranslationResponseError(TranslationError):
    """Resposta invalida do modelo."""


def normalizar_traducao_feminina(traducao_padrao: str, traducao_feminina: str) -> str:
    padrao = (traducao_padrao or "").strip()
    feminina = (traducao_feminina or "").strip()
    if not feminina:
        return ""
    if feminina == padrao or feminina.casefold() == padrao.casefold():
        return ""
    return traducao_feminina


def extrair_json_resposta(resposta_texto: str) -> str:
    txt = (resposta_texto or "").strip()
    if not txt:
        raise TranslationResponseError("Resposta vazia do modelo.")

    if "```json" in txt:
        return txt.split("```json", maxsplit=1)[1].split("```", maxsplit=1)[0].strip()
    if "```" in txt:
        return txt.split("```", maxsplit=1)[1].split("```", maxsplit=1)[0].strip()
    return txt


def normalizar_resposta(data):
    if not isinstance(data, dict):
        raise TranslationResponseError("Resposta JSON nao e um objeto.")

    traducao_padrao = str(data.get("traducao_padrao", ""))
    traducao_feminina = str(data.get("traducao_feminina", ""))
    traducao_feminina = normalizar_traducao_feminina(traducao_padrao, traducao_feminina)
    try:
        confianca = int(data.get("confianca", 0))
    except (TypeError, ValueError):
        confianca = 0

    return {
        "traducao_padrao": traducao_padrao,
        "traducao_feminina": traducao_feminina,
        "confianca": max(0, min(10, confianca)),
    }


def montar_prompt(texto_en: str, instrucoes_voz: str) -> str:
    return f"""
Você é um Localizador sênior para o RPG "Tyranny".

DIRETRIZES DE ESTILO:
{GUIA_ESTILO}

CONTEXTO ESPECÍFICO DE PERSONAGEM/FACÇÃO:
{instrucoes_voz}

REGRA DE DECISÃO:
- Se o texto original contiver explicações de atributos, danos, armas ou regras (mecânicas), use o ESTILO MECÂNICO.
- Se o texto for uma fala ou descrição de história, use o ESTILO NARRATIVO e o CONTEXTO ESPECÍFICO fornecido.

GLOSSÁRIO OBRIGATÓRIO:
{json.dumps(GLOSSARIO, ensure_ascii=False, indent=2)}

REGRAS DE OURO:
1. Mantenha EXATAMENTE as quebras de linha (\\n) originais.
2. Preserve todas as tags [url=glossary:...].
3. Use o glossário para termos dentro das tags.
4. Preencha "traducao_feminina" APENAS quando houver variacao real de genero (artigos, pronomes, adjetivos ou flexao). Se nao houver necessidade, retorne string vazia.

RETORNE APENAS JSON:
{{
  "traducao_padrao": "...",
  "traducao_feminina": "...",
  "confianca": 10
}}

Texto original:
{texto_en}
"""


def processar_entrada(client, model_name: str, texto_en: str, instrucoes_voz: str):
    if not texto_en or texto_en.strip() == "":
        return {"traducao_padrao": "", "traducao_feminina": "", "confianca": 10}

    prompt = montar_prompt(texto_en, instrucoes_voz)
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
        )
    except Exception as exc:
        raise TranslationAPIError("Falha ao comunicar com o Gemini.") from exc

    try:
        txt_json = extrair_json_resposta(getattr(response, "text", ""))
        data = json.loads(txt_json)
        return normalizar_resposta(data)
    except json.JSONDecodeError as exc:
        raise TranslationResponseError("Gemini retornou JSON invalido.") from exc
