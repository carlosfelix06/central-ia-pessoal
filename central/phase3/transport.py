"""HTTP OpenAI isolado em processo; nenhuma repetição implícita."""
import http.client
import json
import os
import time
from .policy import instructions

MAX_RESPONSE_BYTES = 131072


def live_ready(settings, allow_paid):
    if settings.mode == 'live':
        if not allow_paid or os.getenv('P3_ENABLE_OPENAI') != 'true' or os.getenv('P3_PRICES_ACK') != 'true':
            raise PermissionError('OpenAI real bloqueada: habilitação e revisão de preços necessárias')
        key = os.getenv('OPENAI_API_KEY', '')
        if not key or len(key) > 1024 or any(c.isspace() for c in key):
            raise PermissionError('Credencial ausente ou inválida; valor omitido')


def request_body(settings, agent, text):
    return {'model': settings.model, 'instructions': instructions(agent), 'input': text,
            'max_output_tokens': settings.max_output_tokens, 'reasoning': {'effort': 'none'},
            'tools': [], 'store': False, 'background': False}


def parse_response(data):
    if not isinstance(data, dict) or data.get('status') != 'completed':
        return {'code': 'invalid'}
    usage = data.get('usage', {})
    if not isinstance(usage, dict):
        return {'code': 'invalid'}
    values = [usage.get('input_tokens'), usage.get('output_tokens')]
    if any(type(n) is not int or n < 0 for n in values):
        return {'code': 'invalid'}
    texts = []
    for item in data.get('output', []):
        if not isinstance(item, dict) or item.get('type') != 'message':
            return {'code': 'invalid'}  # Nenhuma ferramenta/autorização é interpretada.
        for content in item.get('content', []):
            if content.get('type') != 'output_text' or not isinstance(content.get('text'), str):
                return {'code': 'invalid'}
            texts.append(content['text'])
    text = '\n'.join(texts)
    if not text.strip() or len(text.encode()) > 32768:
        return {'code': 'invalid'}
    return {'code': 'ok', 'text': text, 'input_tokens': values[0], 'output_tokens': values[1]}


def openai_request(settings, job, allow_paid):
    if settings.mode != 'live':
        raise PermissionError('Transporte real exige modo live')
    live_ready(settings, allow_paid)
    connection = http.client.HTTPSConnection('api.openai.com', timeout=settings.timeout_seconds)
    try:
        connection.request('POST', '/v1/responses', body=json.dumps(
            request_body(settings, job['agent'], job['text'])).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'],
                     'X-Client-Request-Id': job['id']})
        response = connection.getresponse()
        if response.status == 429:
            return {'code': 'rate_limited'}
        if response.status in (400, 401, 403, 404, 422):
            return {'code': 'rejected'}
        if response.status != 200:
            return {'code': 'unknown'}
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return {'code': 'invalid'}
        return parse_response(json.loads(raw))
    finally:
        connection.close()


def child_entry(connection, settings, job, allow_paid, simulation):
    """Só códigos fixos atravessam IPC em falhas. Nunca serializar exceções."""
    try:
        if settings.mode == 'live':
            result = openai_request(settings, job, allow_paid)
        elif simulation == 'hang':
            time.sleep(3600)
            result = {'code': 'unknown'}
        elif simulation == 'crash':
            os._exit(2)
        elif simulation == 'exception':
            raise RuntimeError('Valor privado de teste: ' + os.getenv('OPENAI_API_KEY', ''))
        elif simulation in ('rate_limited', 'rejected', 'invalid'):
            result = {'code': simulation}
        else:
            result = parse_response({'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text',
                    'text': '[SIMULAÇÃO] ' + job['agent'] + ': análise recebida; revisão humana pendente.'}]}],
                'usage': {'input_tokens': job['estimate']['estimated_input_tokens'], 'output_tokens': 16}})
        connection.send_bytes(json.dumps(result).encode('utf-8'))
    except BaseException:
        try:
            connection.send_bytes(b'{"code":"unknown"}')
        except BaseException:
            pass
    finally:
        connection.close()
