from pathlib import Path
import zipfile


def garantir_diretorio(path_str: str) -> Path:
    base = Path(path_str).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base


def caminho_saida_espelho(source_file: str, source_en_root: str, output_root: str) -> Path:
    source = Path(source_file)
    en_root = Path(source_en_root)
    out_root = garantir_diretorio(output_root)
    rel = source.relative_to(en_root)
    destino = out_root / rel
    destino.parent.mkdir(parents=True, exist_ok=True)
    return destino


def salvar_xml_traduzido(tree, source_file: str, source_en_root: str, output_root: str) -> Path:
    destino = caminho_saida_espelho(source_file, source_en_root, output_root)
    tree.write(destino, encoding="utf-8", xml_declaration=True)
    return destino


def gerar_zip_da_saida(output_root: str, zip_name: str = "traducao_ptbr.zip") -> Path:
    out_root = Path(output_root).expanduser()
    if not out_root.exists():
        raise ValueError("Diretorio de saida nao encontrado.")

    zip_path = out_root.parent / zip_name
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in out_root.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(out_root)
                zf.write(file_path, arcname=str(arcname))
    return zip_path
