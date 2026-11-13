from dataclasses import dataclass
from central.phase3.policy import PRICES as OPENAI_PRICES, instructions

GEMINI_MODEL = 'gemini-3.5-flash-lite'
PRICES = {'openai': dict(OPENAI_PRICES), 'gemini': {GEMINI_MODEL: (300000, 2500000)}}
PRICE_DATE = '2026-10-31'


@dataclass(frozen=True)
class Route:
    provider: str
    model: str = ''
    max_output_tokens: int = 256
    allow_cross_provider: bool = False

    def __post_init__(self):
        if self.provider not in PRICES:
            raise ValueError('Provedor inválido')
        if not self.model:
            object.__setattr__(self, 'model', GEMINI_MODEL if self.provider == 'gemini' else 'gpt-5.4-mini-2026-03-17')
        if self.model not in PRICES[self.provider]:
            raise ValueError('Modelo fora do catálogo revisado')
        if type(self.max_output_tokens) is not int or not 16 <= self.max_output_tokens <= 1024:
            raise ValueError('Limite de saída inválido')
        if type(self.allow_cross_provider) is not bool:
            raise ValueError('Consentimento inválido')


def cost(route, input_tokens, output_tokens):
    if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
        raise ValueError('Uso inválido')
    i, o = PRICES[route.provider][route.model]
    return (input_tokens*i + output_tokens*o + 999999)//1000000


def estimate_route(settings, route, agent, text):
    if not isinstance(text,str) or not text.strip() or len(text.encode()) > settings.max_input_bytes:
        raise ValueError('Entrada acima do limite ou vazia')
    size = len((instructions(agent)+text).encode())
    typical, reserved = (size+3)//4+64, size+256
    return {'estimated_input_tokens':typical,'reserved_input_tokens':reserved,
            'max_output_tokens':route.max_output_tokens,
            'estimated_microusd':cost(route,typical,route.max_output_tokens),
            'reserve_microusd':cost(route,reserved,route.max_output_tokens)}
