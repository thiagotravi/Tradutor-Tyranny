import os
from dotenv import load_dotenv
import streamlit as st
import streamlit.components.v1 as components
import google.generativeai as genai
import xml.etree.ElementTree as ET
import json
import re # Necessário para detectar as tags de glossário

# Importações dos seus arquivos de configuração
from config_traducao import GLOSSARIO, GUIA_ESTILO
from wiki_personagens import REGRAS_COMPANHEIROS
from wiki_faccoes import REGRAS_FACCOES
from translation_progress import ProgressManager

load_dotenv()

# --- CONFIGURAÇÃO DA API ---
API_KEY = os.getenv("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY")
if not API_KEY:
    st.error("Erro: API_KEY não encontrada. Verifique seu arquivo .env")
    st.stop()
genai.configure(api_key=API_KEY.strip())
MODEL_NAME = 'models/gemini-flash-latest'
model = genai.GenerativeModel(MODEL_NAME)

# --- FUNÇÕES DE DETECÇÃO ---

def verificar_termos_faltantes(texto_en):
    """
    Varre o texto em busca de tags [url=glossary:termo] e verifica 
    se o termo original (chave) existe no nosso GLOSSARIO.
    """
    tags_encontradas = re.findall(r'\[url=glossary:([^\]]+)\]', texto_en.lower())
    faltantes = [t for t in tags_encontradas if t not in GLOSSARIO]
    return faltantes
    
def obter_contexto_voz(nome_arquivo):
    """
    Busca diretrizes de tom baseadas no nome do arquivo 
    cruzando com as wikis de personagens e facções.
    """
    contexto = ""
    nome_low = nome_arquivo.lower()
    
    # Busca em Personagens/Companheiros
    for chave, dados in REGRAS_COMPANHEIROS.items():
        if chave in nome_low:
            if isinstance(dados, dict):
                contexto += f"\nPERSONAGEM ({chave.upper()}): {dados.get('perfil')} {dados.get('diretriz')}"
            break
            
    # Busca em Facções
    for chave, dados in REGRAS_FACCOES.items():
        if chave in nome_low:
            contexto += f"\nFACÇÃO ({dados.get('nome')}): {dados.get('perfil')} Tom: {dados.get('tom')}"
            break
            
    return contexto

# --- LÓGICA DE TRADUÇÃO ---

def verificar_termos_faltantes(texto_en):
    """
    Varre o texto em busca de tags [url=glossary:termo] e verifica 
    se o termo original existe no GLOSSARIO (ignorando maiúsculas).
    """
    # 1. Encontra os termos nas tags e coloca em minúsculo
    tags_encontradas = re.findall(r'\[url=glossary:([^\]]+)\]', texto_en.lower())
    
    # 2. Cria uma lista das chaves do glossário também em minúsculo para comparar
    glossario_keys_low = [k.lower() for k in GLOSSARIO.keys()]
    
    # 3. Filtra apenas o que realmente não está no glossário
    faltantes = [t for t in tags_encontradas if t not in glossario_keys_low]
    return faltantes
    
def processar_entrada(texto_en, instrucoes_voz):
    if not texto_en or texto_en.strip() == "":
        return {"traducao_padrao": "", "traducao_feminina": "", "confianca": 10}

    prompt_completo = f"""
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

RETORNE APENAS JSON:
{{
  "traducao_padrao": "...",
  "traducao_feminina": "...",
  "confianca": 10
}}

Texto original:
{texto_en}
"""
    try:
        response = model.generate_content(prompt_completo)
        txt = response.text.strip()
        if "```json" in txt: txt = txt.split("```json")[1].split("```")[0]
        elif "```" in txt: txt = txt.split("```")[1].split("```")[0]
        return json.loads(txt)
    except Exception:
        return {"traducao_padrao": "Erro no processamento", "traducao_feminina": "", "confianca": 0}

# --- INTERFACE ---

st.set_page_config(page_title="Tyranny Localizer v0.6 Stable", layout="wide")

if "progresso" not in st.session_state:
    st.session_state.progresso = ProgressManager()

with st.sidebar:
    st.header("📊 Progresso")
    total, concluidos, percent = st.session_state.progresso.get_stats()
    st.progress(percent / 100)
    st.write(f"**{concluidos}** de **{total}** arquivos")
    
    st.divider()
    with st.expander("📂 Checklist de Arquivos"):
        busca = st.text_input("🔍 Buscar...")
        if hasattr(st.session_state.progresso, 'progress'):
            lista = list(st.session_state.progresso.progress.items())
            if busca: lista = [(n, s) for n, s in lista if busca.lower() in n.lower()]
            for arq_nome, status in lista:
                col_t, col_b = st.columns([0.8, 0.2])
                col_t.write(f"{'✅' if status else '⌛'} {arq_nome}")
                if col_b.button("🔄", key=f"btn_{arq_nome}"):
                    st.session_state.progresso.update_status(arq_nome, not status)
                    st.rerun()

st.title("⚖️ Estação Seladestinos v0.6")

arquivo = st.file_uploader("Suba o arquivo .stringtable", type=["stringtable", "xml"])

if arquivo:
    if "tree" not in st.session_state or st.session_state.get('last_file') != arquivo.name:
        st.session_state.tree = ET.parse(arquivo)
        st.session_state.root = st.session_state.tree.getroot()
        st.session_state.entries = st.session_state.root.findall(".//Entry")
        st.session_state.idx = 0
        st.session_state.cache = {}
        st.session_state.last_file = arquivo.name

    while st.session_state.idx < len(st.session_state.entries):
        idx = st.session_state.idx
        entry = st.session_state.entries[idx]
        txt_en = entry.find("DefaultText").text or ""
        
        # Cria uma lista de chaves do glossário em minúsculas para comparação
        faltantes = verificar_termos_faltantes(txt_en)
        # BUSCA CONTEXTO DINÂMICO
        instrucoes_dinamicas = obter_contexto_voz(arquivo.name) # <--- CORREÇÃO AQUI

        if idx not in st.session_state.cache:
            with st.spinner(f"Processando entrada {idx}..."):
                st.session_state.cache[idx] = processar_entrada(txt_en, instrucoes_dinamicas)
        
        res = st.session_state.cache[idx]
        
        # Pausa se houver termos faltantes OU confiança baixa
        if res.get('confianca', 0) >= 10 and not faltantes:
            entry.find("DefaultText").text = res.get('traducao_padrao', "")
            f_node = entry.find("FemaleText")
            if f_node is not None and res.get('traducao_feminina'):
                f_node.text = res.get('traducao_feminina')
            st.session_state.idx += 1
        else:
            st.warning(f"⚠️ INTERVENÇÃO NECESSÁRIA (Entrada {idx})")
            if faltantes:
                st.error(f"🔍 **Termos não mapeados no glossário:** {', '.join(faltantes)}")
                st.info("💡 Sugestão: Adicione estes termos ao seu arquivo `config_traducao.py` para evitar futuras pausas.")
            
            col1, col2, col3 = st.columns(3)
            with col1: st.text_area("Original (EN):", value=txt_en, height=350, disabled=True)
            with col2: edit_p = st.text_area("Padrão:", value=res.get('traducao_padrao', ""), height=350, key=f"p_{idx}")
            with col3: edit_f = st.text_area("Feminino:", value=res.get('traducao_feminina', ""), height=350, key=f"f_{idx}")

            if st.button("Aprovar Entrada ✅"):
                entry.find("DefaultText").text = edit_p
                f_node = entry.find("FemaleText")
                if f_node is not None: f_node.text = edit_f
                st.session_state.idx += 1
                st.rerun()
            break
    else:
        st.success("🎉 Arquivo finalizado!")
        st.session_state.progresso.update_status(arquivo.name, True)

    xml_output = ET.tostring(st.session_state.root, encoding='utf-8', xml_declaration=True)
    st.download_button("💾 Baixar Tradução", xml_output, file_name=f"localizado_{arquivo.name}")