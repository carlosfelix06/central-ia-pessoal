"""Explicit credential source; no values in logs, no implicit authorization from .env."""
import os
from pathlib import Path

NAMES = ('GEMINI_API_KEY','GOOGLE_API_KEY','OPENAI_API_KEY')


def env_values(path):
    if not Path(path).is_file():
        return {}
    from dotenv import dotenv_values
    try:
        return {k:v for k,v in dotenv_values(path,interpolate=False,encoding='utf-8-sig').items() if k in NAMES}
    except Exception:
        raise ValueError('Falha ao ler configuração privada; valores omitidos') from None


def inspect_credentials(path='.env'):
    values = env_values(path)
    return {'env_file_present':Path(path).is_file(),
            'keys':{k:{'in_file':bool(values.get(k)),'in_process':bool(os.getenv(k))} for k in NAMES},
            'gemini_alias_conflict_in_file':bool(values.get('GEMINI_API_KEY') and values.get('GOOGLE_API_KEY')
                                                and values['GEMINI_API_KEY']!=values['GOOGLE_API_KEY']),
            'api_validity_checked':False}


def load_credentials(path):
    # Only credentials; flags/models in .env can never authorize calls.
    values = env_values(path)
    for name,value in values.items():
        if value and os.getenv(name) and value != os.environ[name]:
            raise PermissionError('Conflito de fontes de credencial; valores omitidos')
    for name,value in values.items():
        if value:
            os.environ[name]=value


def gemini_key():
    gemini, google = os.getenv('GEMINI_API_KEY'),os.getenv('GOOGLE_API_KEY')
    if gemini and google and gemini != google:
        raise PermissionError('Aliases Gemini conflitantes; valores omitidos')
    key = gemini or google
    if not key or len(key)>1024 or any(c.isspace() for c in key):
        raise PermissionError('Credencial Gemini ausente ou inválida; valor omitido')
    return key
