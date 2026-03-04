from wiki_personagens import REGRAS_COMPANHEIROS
from wiki_faccoes import REGRAS_FACCOES


def obter_contexto_voz(nome_arquivo: str) -> str:
    """
    Busca diretrizes de tom baseadas no nome do arquivo
    cruzando com as wikis de personagens e faccoes.
    """
    contexto = ""
    nome_low = (nome_arquivo or "").lower()

    for chave, dados in REGRAS_COMPANHEIROS.items():
        if chave in nome_low:
            if isinstance(dados, dict):
                contexto += (
                    f"\nPERSONAGEM ({chave.upper()}): "
                    f"{dados.get('perfil')} {dados.get('diretriz')}"
                )
            break

    for chave, dados in REGRAS_FACCOES.items():
        if chave in nome_low:
            contexto += (
                f"\nFACCAO ({dados.get('nome')}): "
                f"{dados.get('perfil')} Tom: {dados.get('tom')}"
            )
            break

    return contexto
