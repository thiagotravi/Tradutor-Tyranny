# Caminho base do Tyranny (ajuste se necessario)
$path = "C:\Program Files (x86)\Steam\steamapps\common\Tyranny\Data\data\exported\localized\en\text"

# Pega caminhos do projeto
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectRoot = Split-Path -Parent $scriptDir
$dataDir = Join-Path $projectRoot "data"
$outputFile = Join-Path $dataDir "Tyranny_Structure.xml"

# Garante pasta data/
if (!(Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
}

# Cria documento XML
$xmlDoc = New-Object System.Xml.XmlDocument

function Get-XmlStructure($folder, $xmlDoc) {
    # Cria elemento <Directory> vinculado ao documento
    $xmlElement = $xmlDoc.CreateElement("Directory")
    $xmlElement.SetAttribute("name", $folder.FullName)

    # Adiciona arquivos
    foreach ($file in Get-ChildItem -Path $folder.FullName -File) {
        $fileElement = $xmlDoc.CreateElement("File")
        $fileElement.SetAttribute("name", $file.Name)
        $xmlElement.AppendChild($fileElement) | Out-Null
    }

    # Adiciona subpastas recursivamente
    foreach ($subfolder in Get-ChildItem -Path $folder.FullName -Directory) {
        $subDirElement = Get-XmlStructure $subfolder $xmlDoc
        $xmlElement.AppendChild($subDirElement) | Out-Null
    }

    return $xmlElement
}

# Cria raiz a partir da pasta inicial
$root = Get-XmlStructure (Get-Item $path) $xmlDoc
$xmlDoc.AppendChild($root) | Out-Null

# Salva em arquivo dentro de data/
$xmlDoc.Save($outputFile)
Write-Host "Estrutura exportada para: $outputFile"
