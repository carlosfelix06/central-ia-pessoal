import json
import os
import urllib.request


class MockProvider:
    name = 'mock'

    def generate(self, role, text):
        return f'[SIMULAÇÃO — {role}] Plano recebido: {text}'


class APIProvider:
    """Adaptadores de texto. Não executam ferramentas nem repetem chamadas."""
    ENDPOINTS = {
        'openai': 'https://api.openai.com/v1/responses',
        'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
        'xai': 'https://api.x.ai/v1/responses',
    }

    def __init__(self, name):
        if name not in self.ENDPOINTS:
            raise ValueError('Provedor desconhecido')
        self.name = name

    def generate(self, role, text):
        # Segunda barreira, inclusive se alguém chamar o adaptador diretamente.
        if os.getenv('CENTRAL_ALLOW_PAID') != 'true':
            raise PermissionError('Chamadas externas desativadas')
        key = os.environ.get(self.name.upper() + '_API_KEY')
        model = os.environ.get(self.name.upper() + '_MODEL')
        if not key or not model:
            raise ValueError('Configure chave e modelo fora do código')
        instruction = f'Você é o agente {role}. Produza orientação em português. Não afirme executar ações externas.'
        if self.name == 'gemini':
            body = dict(model=model, messages=[{'role': 'system', 'content': instruction},
                        {'role': 'user', 'content': text}], max_tokens=512)
        else:
            body = dict(model=model, instructions=instruction, input=text,
                        max_output_tokens=512, store=False)
        request = urllib.request.Request(self.ENDPOINTS[self.name],
            data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            if self.name == 'gemini':
                result = data['choices'][0]['message']['content']
            else:
                result = '\n'.join(c['text'] for item in data.get('output', [])
                    for c in item.get('content', []) if c.get('type') == 'output_text')
            if not result:
                raise ValueError('Resposta vazia')
            return result
        except Exception:
            # Não propagar corpo HTTP, cabeçalhos ou segredos nos registros.
            raise RuntimeError('Falha no provedor; sem repetição automática') from None
