# Script PowerShell para rodar Streamlit com caminho relativo

# Pega o diretório atual (onde o script .ps1 está salvo)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Monta o caminho relativo para o arquivo Python
$appPath = Join-Path $scriptDir "app_tradutor.py"

# Executa o Streamlit
streamlit run $appPath