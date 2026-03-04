# TradutorTyranny

Aplicacao Streamlit para localizar arquivos `.stringtable/.xml` do jogo **Tyranny** com suporte de IA (Gemini), glossario obrigatorio e revisao manual quando necessario.

## O que o projeto faz

- Recebe um arquivo de texto do jogo (`.stringtable` ou `.xml`).
- Processa cada `<Entry>` e sugere traducao em PT-BR usando contexto e regras.
- Aplica automaticamente entradas com alta confianca e sem termos faltantes.
- Pausa para revisao manual quando a resposta exige intervencao.
- Permite baixar o XML traduzido.
- Mantem checklist de progresso por arquivo com `data/Tyranny_Structure.xml` e `data/progress_data.json`.

## Requisitos

- Python 3.10+ (recomendado)
- Chave de API do Gemini

## Instalacao

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuracao

1. Copie `.env.example` para `.env`.
2. Preencha sua chave:

```env
GEMINI_API_KEY=sua_chave_aqui
GEMINI_MODEL=gemini-2.5-flash
```

Tambem e possivel usar `.streamlit/secrets.toml`:

```toml
GEMINI_API_KEY = "sua_chave_aqui"
```

## Execucao

```powershell
streamlit run app_tradutor.py
```

Ou execute:

```powershell
.\scripts\RunAppTradutor.ps1
```

## Fluxo de uso

1. Abra a interface Streamlit.
2. Escolha o modo de entrada:
   - **Diretorio** (novo): informe `localized`, `localized/en` ou `localized/en/text`. A ferramenta detecta e usa apenas a arvore de `en`.
   - **Arquivo unico (legado)**: envie um `.stringtable`/`.xml` manualmente.
3. (Opcional) Informe tambem a raiz ES (`localized`, `localized/es` ou `localized/es/text`) para validacao cruzada.
4. Selecione o arquivo a traduzir (no modo diretorio).
5. Opcional: use **Iniciar lote (arquivos filtrados)** para processar varios arquivos em sequencia.
6. Revise entradas pausadas (termos faltantes, baixa confianca ou inconsistencias EN/ES/PT).
7. Clique em aprovar para continuar.
8. Cada arquivo finalizado e salvo automaticamente no **diretorio de saida espelho**.
9. Ao fim do lote, a ferramenta gera um `traducao_ptbr.zip` para distribuicao/aplicacao.
10. Baixe o arquivo traduzido individual ou o ZIP do lote.

## Scripts auxiliares

- `scripts/RunAppTradutor.ps1`: inicia o Streamlit no script principal.
- `scripts/ExportarEstruturaXML.ps1`: gera `data/Tyranny_Structure.xml` para o checklist de progresso (exige ajuste de caminhos locais).

## Observacoes

- O projeto depende da API do Gemini.
- Caso a resposta da IA venha em formato invalido, o app exibe erro e permite tentar novamente.
- `data/progress_data.json` e `.env` estao ignorados no Git por conterem dados locais/sensiveis.
