import os
import logging
import streamlit as st
from dotenv import load_dotenv
from google import genai

load_dotenv()

logger = logging.getLogger(__name__)


def obter_api_key() -> str:
    env_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if env_key:
        return env_key

    try:
        secret_key = (st.secrets.get("GEMINI_API_KEY") or "").strip()
    except Exception:
        secret_key = ""
    return secret_key


def obter_model_name() -> str:
    return (os.getenv("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash").strip()


def criar_client(api_key: str):
    try:
        return genai.Client(api_key=api_key)
    except Exception as exc:
        logger.exception("Falha ao inicializar cliente Gemini.")
        raise RuntimeError("Nao foi possivel inicializar o cliente Gemini.") from exc
