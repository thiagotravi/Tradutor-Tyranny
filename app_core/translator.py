import json
import re
import time
from config_traducao import GUIA_ESTILO
from app_core.glossary import obter_glossario_completo


class TranslationError(Exception):
    """Erro base de traducao."""


class TranslationAPIError(TranslationError):
    """Falha na chamada do modelo."""


class TranslationResponseError(TranslationError):
    """Resposta invalida do modelo."""


GLOSSARY_WRAPPED_TAG_PATTERN = re.compile(
    r"\[url=glossary:([^\]]+)\](.*?)\[/url\]",
    flags=re.IGNORECASE | re.DOTALL,
)
DEFAULT_API_RETRY_ATTEMPTS = 4
DEFAULT_API_RETRY_BASE_DELAY_S = 1.2


def normalizar_traducao_feminina(traducao_padrao: str, traducao_feminina: str) -> str:
    padrao = (traducao_padrao or "").strip()
    feminina = (traducao_feminina or "").strip()
    if not feminina:
        return ""
    if feminina == padrao or feminina.casefold() == padrao.casefold():
        return ""
    return traducao_feminina


def sanitizar_tags_glossario(texto_en: str, texto_pt: str) -> str:
    """
    Forca a estrutura de tags de glossario do PT a seguir o EN:
    - mesma ordem de aparicao
    - sem tags extras
    - IDs alinhados ao EN
    """
    en_ids = [m.strip() for m in re.findall(r"\[url=glossary:([^\]]+)\]", texto_en or "", flags=re.IGNORECASE)]
    if not texto_pt:
        return ""
    if not en_ids:
        # Se EN nao tem tags, remove qualquer tag de glossario inserida no PT.
        return GLOSSARY_WRAPPED_TAG_PATTERN.sub(lambda m: m.group(2) or "", texto_pt)

    idx = 0

    def _replace(match):
        nonlocal idx
        inner = match.group(2) or ""
        if idx < len(en_ids):
            term_id = en_ids[idx]
            idx += 1
            return f"[url=glossary:{term_id}]{inner}[/url]"
        # Remove tags excedentes que nao existem no EN.
        return inner

    return GLOSSARY_WRAPPED_TAG_PATTERN.sub(_replace, texto_pt)


def texto_parece_truncado(texto_en: str, texto_pt: str) -> bool:
    en = texto_en or ""
    pt = texto_pt or ""
    en_compact = re.sub(r"\s+", " ", en).strip()
    pt_compact = re.sub(r"\s+", " ", pt).strip()

    if not en_compact:
        return False

    ratio = len(pt_compact) / max(1, len(en_compact))
    en_line_count = en.count("\n")
    pt_line_count = pt.count("\n")

    if en_line_count >= 2 and pt_line_count < en_line_count:
        return True
    if len(en_compact) >= 120 and ratio < 0.55:
        return True
    return False


def _gerar_e_parsear_json(client, model_name: str, prompt: str):
    last_exc = None
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
                raise TranslationAPIError(
                    f"Falha ao comunicar com o Gemini apos {DEFAULT_API_RETRY_ATTEMPTS} tentativas."
                ) from exc
            delay_s = DEFAULT_API_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            time.sleep(delay_s)

    txt_json = extrair_json_resposta(getattr(response, "text", ""))
    return json.loads(txt_json)


def extrair_json_resposta(resposta_texto: str) -> str:
    txt = (resposta_texto or "").strip()
    if not txt:
        raise TranslationResponseError("Resposta vazia do modelo.")

    if "```json" in txt:
        return txt.split("```json", maxsplit=1)[1].split("```", maxsplit=1)[0].strip()
    if "```" in txt:
        return txt.split("```", maxsplit=1)[1].split("```", maxsplit=1)[0].strip()
    return txt


def _coerce_to_response_object(data):
    if isinstance(data, dict):
        # Caso comum
        return data

    if isinstance(data, str):
        # Algumas respostas vêm como JSON serializado dentro de string.
        try:
            parsed = json.loads(data)
            return _coerce_to_response_object(parsed)
        except Exception:
            raise TranslationResponseError("Resposta JSON veio como texto nao-estruturado.")

    if isinstance(data, list):
        # Alguns modelos retornam lista; tenta encontrar primeiro objeto util.
        for item in data:
            if isinstance(item, dict):
                return item
            if isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
        raise TranslationResponseError("Resposta JSON em lista sem objeto valido.")

    raise TranslationResponseError("Resposta JSON nao e um objeto.")


def normalizar_resposta(data):
    data = _coerce_to_response_object(data)

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


def montar_prompt(texto_en: str, instrucoes_voz: str, glossario: dict) -> str:
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
{json.dumps(glossario, ensure_ascii=False, indent=2)}

REGRAS DE OURO:
1. Mantenha EXATAMENTE as quebras de linha (\\n) originais.
2. Preserve todas as tags [url=glossary:...].
3. Use o glossário para termos dentro das tags.
4. Preencha "traducao_feminina" APENAS quando houver variacao real de genero (artigos, pronomes, adjetivos ou flexao). Se nao houver necessidade, retorne string vazia.
5. NUNCA adicione tags [url=glossary:...] extras. O total e a ordem de tags devem ser identicos ao EN.

RETORNE APENAS JSON:
{{
  "traducao_padrao": "...",
  "traducao_feminina": "...",
  "confianca": 10
}}

Texto original:
{texto_en}
"""


def processar_entrada(client, model_name: str, texto_en: str, instrucoes_voz: str, glossario: dict | None = None):
    if not texto_en or texto_en.strip() == "":
        return {"traducao_padrao": "", "traducao_feminina": "", "confianca": 10}

    glossario_final = glossario or obter_glossario_completo()
    prompt = montar_prompt(texto_en, instrucoes_voz, glossario_final)
    try:
        data = _gerar_e_parsear_json(client, model_name, prompt)
    except TranslationAPIError:
        raise
    except Exception as exc:
        raise TranslationAPIError("Falha ao comunicar com o Gemini.") from exc

    def _normalizar_final(data_obj):
        res = normalizar_resposta(data_obj)
        res["traducao_padrao"] = sanitizar_tags_glossario(texto_en, res.get("traducao_padrao", ""))
        res["traducao_feminina"] = sanitizar_tags_glossario(texto_en, res.get("traducao_feminina", ""))
        res["traducao_feminina"] = normalizar_traducao_feminina(
            res.get("traducao_padrao", ""),
            res.get("traducao_feminina", ""),
        )
        return res
    try:
        res = _normalizar_final(data)
    except TranslationResponseError:
        # Quando o modelo devolve formato inesperado, tenta uma correcao guiada.
        repair_prompt = f"""
Reformate a resposta abaixo para JSON OBJETO valido com as chaves:
traducao_padrao, traducao_feminina, confianca.
Nao omita conteudo.

Texto EN:
{texto_en}

Resposta recebida:
{json.dumps(data, ensure_ascii=False)}

Retorne APENAS JSON:
{{
  "traducao_padrao": "...",
  "traducao_feminina": "...",
  "confianca": 10
}}
"""
        try:
            repaired_data = _gerar_e_parsear_json(client, model_name, repair_prompt)
            res = _normalizar_final(repaired_data)
        except Exception as exc:
            raise TranslationResponseError("Gemini retornou formato de resposta inesperado.") from exc

    # Segunda tentativa automatica quando houver sinais fortes de truncamento.
    if texto_parece_truncado(texto_en, res.get("traducao_padrao", "")):
        retry_prompt = f"""
Corrija a traducao PT-BR abaixo sem omitir nenhum trecho do EN.
Mantenha EXATAMENTE a mesma quantidade de quebras de linha do EN e preserve tags [url=glossary:...].

GLOSSARIO OBRIGATORIO:
{json.dumps(glossario_final, ensure_ascii=False, indent=2)}

Texto EN:
{texto_en}

Traducao PT-BR atual (provavelmente truncada):
{res.get("traducao_padrao", "")}

Retorne APENAS JSON:
{{
  "traducao_padrao": "...",
  "traducao_feminina": "...",
  "confianca": 10
}}
"""
        try:
            retry_data = _gerar_e_parsear_json(client, model_name, retry_prompt)
            retry_res = _normalizar_final(retry_data)
            if not texto_parece_truncado(texto_en, retry_res.get("traducao_padrao", "")):
                return retry_res
        except Exception:
            pass

    return res


def sugerir_traducao_glossario(client, model_name: str, termo_en: str, termo_es: str = "") -> str:
    prompt = f"""
Voce e um localizador de RPG. Sugira a melhor traducao PT-BR para o termo de glossario abaixo.
Considere manter nomes proprios quando apropriado.
Se houver referencia oficial em espanhol, use apenas para evitar inconsistencias.

Termo EN: {termo_en}
Termo ES (opcional): {termo_es}

Retorne APENAS JSON:
{{
  "traducao_sugerida": "..."
}}
"""
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
        txt_json = extrair_json_resposta(getattr(response, "text", ""))
        data = json.loads(txt_json)
        suggested = str(data.get("traducao_sugerida", "")).strip()
        return suggested if suggested else termo_en
    except Exception:
        return termo_en
