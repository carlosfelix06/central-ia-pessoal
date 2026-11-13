import argparse
import json
import os
from .core import AGENTS, Orchestrator, Store
from .providers import MockProvider, APIProvider


def main():
    parser = argparse.ArgumentParser(description='Central-IA-Pessoal — simulação por padrão')
    parser.add_argument('pedido')
    parser.add_argument('--agents', nargs='+', choices=AGENTS)
    args = parser.parse_args()
    provider_name = os.getenv('CENTRAL_PROVIDER', 'mock')
    provider = MockProvider() if provider_name == 'mock' else APIProvider(provider_name)
    store = Store(os.getenv('CENTRAL_DB', 'central.sqlite3'))
    try:
        app = Orchestrator(store, provider,
            allow_paid=os.getenv('CENTRAL_ALLOW_PAID') == 'true',
            grants={(a, 'draft') for a in AGENTS},
            reserve_microusd=int(os.getenv('CENTRAL_RESERVE_MICROUSD', '0')),
            daily_microusd=int(os.getenv('CENTRAL_DAILY_MICROUSD', '0')),
            call_microusd=int(os.getenv('CENTRAL_CALL_MICROUSD', '0')))
        print(json.dumps(app.run(args.pedido, args.agents), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
