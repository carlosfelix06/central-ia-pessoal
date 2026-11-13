import argparse
from dataclasses import asdict
import json
from pathlib import Path
from .policy import AGENTS, Settings, estimate, PRICE_DATE, canonical
from .queue import Queue
from .worker import Worker


def demo(directory):
    """Não lê configuração de API. Mesmo com chave/flags no ambiente, permanece mock."""
    settings = Settings(mode='mock', budget_microusd=100000, timeout_seconds=3)
    queue = Queue(directory, settings)
    try:
        run = queue.submit('fase3-cadeia-6-v1', 'Estruturar pipeline ETL e dashboard executivo de BI na Matriz-SP.', tuple(AGENTS))
        worker = Worker(queue)
        worker.once()  # checkpoint persistente antes de simular reinício do supervisor
        queue.close()
        queue = Queue(directory, settings)
        Worker(queue).drain()
        statuses = queue.status(run)
        action = queue.propose_action('fase3-email-v1', statuses[-1]['id'], 'send_email',
            {'destinatario': 'recrutador@example.invalid', 'corpo': 'Rascunho fictício: esclarecer jornada e rescisão.'})
        reviewed = queue.review_action(action)
        before = reviewed['state']
        # Aprovação de demonstração; não representa autorização do usuário para envio real.
        if before == 'awaiting_approval':
            queue.decide(action, reviewed['fingerprint'], approve=True, reviewer='humano-ficticio-da-demonstracao')
        try:
            queue.execute_action(action)
            action_blocked = False
        except PermissionError:
            action_blocked = True
        report = {'mode': 'mock', 'api_calls': 0, 'actual_api_cost_usd': '0.00',
            'run': run, 'jobs': statuses, 'private_messages': queue.private_messages(run),
            'resumed_after_reopen': True, 'budget_simulated': queue.budget(),
            'approval': {'before': 'awaiting_approval', 'after': queue.review_action(action)['state'],
                         'human_is_fictitious': True, 'external_execution_blocked': action_blocked},
            'pricing_reference_date': PRICE_DATE,
            'policy': asdict(settings)}
    finally:
        queue.close()
    # Banco separado para demonstrar bloqueio no limite exato, sem mexer na política da cadeia.
    one = estimate(settings, 'coordenador_bi', 'Teste de orçamento')['reserve_microusd']
    cap = Settings(mode='mock', budget_microusd=one, timeout_seconds=3)
    budget_queue = Queue(Path(directory) / 'budget-demo', cap)
    try:
        first = budget_queue.submit('primeira', 'Teste de orçamento', ('coordenador_bi',))
        second = budget_queue.submit('segunda', 'Teste de orçamento', ('coordenador_bi',))
        Worker(budget_queue).drain()
        report['budget_limit_demonstration'] = {'first': budget_queue.status(first),
            'second': budget_queue.status(second), 'budget': budget_queue.budget()}
    finally:
        budget_queue.close()
    target = Path(directory) / 'relatorio-fase3.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(canonical({'report': str(target), 'completed_agents': len([j for j in statuses if j['state']=='done']),
                     'budget_blocked': report['budget_limit_demonstration']['second'][0]['state'],
                     'api_calls': 0, 'external_actions': 0}))
    return report


def main():
    parser = argparse.ArgumentParser(description='Central Fase 3 — OpenAI com simulação por padrão')
    parser.add_argument('--directory', default='fase3-local')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('demo')
    request = commands.add_parser('submit')
    request.add_argument('--key', required=True)
    request.add_argument('--input-file', required=True, help='Arquivo UTF-8 com pedido; não aparece nos logs')
    request.add_argument('--agents', nargs='+', choices=AGENTS, default=['coordenador_bi', 'analista_bi', 'analytics_finops'])
    commands.add_parser('estimate').add_argument('--input-file', required=True)
    work = commands.add_parser('work')
    work.add_argument('--allow-paid', action='store_true')
    work.add_argument('--max-jobs', type=int, default=1)
    for name in ('status', 'cancel'):
        commands.add_parser(name).add_argument('--run', required=True)
    commands.add_parser('recover')
    action_request = commands.add_parser('propose-action')
    action_request.add_argument('--key', required=True)
    action_request.add_argument('--job', required=True)
    action_request.add_argument('--kind', required=True, choices=('send_email', 'write_file', 'commitment'))
    action_request.add_argument('--payload-file', required=True)
    for name in ('review-action', 'approve', 'reject', 'revoke'):
        action = commands.add_parser(name)
        action.add_argument('--action', required=True)
        if name in ('approve', 'reject'):
            action.add_argument('--fingerprint', required=True)
            action.add_argument('--reviewer', required=True)
            action.add_argument('--human-confirm', action='store_true', required=True)
    args = parser.parse_args()
    if args.command == 'demo':
        demo(args.directory)
        return
    settings = Settings.from_env()
    if args.command == 'estimate':
        print(canonical(estimate(settings, 'coordenador_bi', Path(args.input_file).read_text(encoding='utf-8'))))
        return
    queue = Queue(args.directory, settings)
    try:
        if args.command == 'submit':
            print(queue.submit(args.key, Path(args.input_file).read_text(encoding='utf-8'), args.agents))
        elif args.command == 'work':
            print(canonical(Worker(queue, allow_paid=args.allow_paid).drain(max_jobs=args.max_jobs)))
        elif args.command == 'status':
            print(canonical(queue.status(args.run)))
        elif args.command == 'cancel':
            queue.cancel(args.run)
        elif args.command == 'recover':
            print(canonical({'uncertain': queue.recover()}))
        elif args.command == 'propose-action':
            payload = json.loads(Path(args.payload_file).read_text(encoding='utf-8'))
            print(queue.propose_action(args.key, args.job, args.kind, payload))
        elif args.command == 'review-action':
            # Comando explícito de revisão privada: mostra o conteúdo, não é log operacional.
            print(canonical(queue.review_action(args.action)))
        elif args.command in ('approve', 'reject'):
            queue.decide(args.action, args.fingerprint, approve=args.command == 'approve', reviewer=args.reviewer)
        elif args.command == 'revoke':
            queue.revoke(args.action)
    finally:
        queue.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Nem caminhos privados nem valores de ambiente/exceções arbitrárias no terminal.
        raise SystemExit('Operação não concluída. Verifique configuração, estados e permissões; detalhes privados omitidos.') from None
