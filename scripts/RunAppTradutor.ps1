# Script PowerShell para rodar Streamlit com caminho relativo

# Pasta do script (scripts/)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Raiz do projeto
$projectRoot = Split-Path -Parent $scriptDir

# Caminho para o app principal
$appPath = Join-Path $projectRoot "app_tradutor.py"

# Executa o Streamlit
streamlit run $appPath
