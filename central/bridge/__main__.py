"""Interface de linha de comando para a demonstração e operação da Ponte Central."""
import argparse
import json
from .orchestrator import BridgeOrchestrator


def run_demo() -> None:
    print('=' * 70)
    print('CENTRAL-IA-PESSOAL — DEMONSTRAÇÃO DA PONTE CENTRAL (100% OFFLINE)')
    print('=' * 70)

    orchestrator = BridgeOrchestrator(db_path='fase-ponte-demo.sqlite3')

    # Cenário 1: Fluxo encadeado entre especialistas
    print('\n[CENÁRIO 1] Registro de Tarefa Única com Handoff Automático')
    print('Entrada do usuário: "Organizar agenda semanal e preparar especificações técnicas de software"')

    reg1 = orchestrator.register_task(
        'Organizar agenda semanal e preparar especificações técnicas de software',
        chain=['coordenador_bi', 'engenheiro_dados'],
        correlation_id='demo-corr-cenario1',
    )
    print(f"Tarefa registrada com sucesso: id={reg1['task_id']}, correlação={reg1['correlation_id']}")
    print(f"Cadeia configurada: {' -> '.join(reg1['chain'])}")

    print('\nProcessando passos do orquestrador...')
    steps = orchestrator.run_until_idle()
    for s in steps:
        print(f" -> Passo concluído: Agente={s.get('agent')}, Status={s.get('status')}")

    consolidated = orchestrator.consolidate('demo-corr-cenario1')
    print(f"\nResultado da Consolidação: Concluído={consolidated['is_complete']}, Total Tarefas={consolidated['total_tasks']}")
    print('Estrutura final transmitida:')
    print(json.dumps(consolidated['final_result'], indent=2, ensure_ascii=False))

    # Cenário 2: Falha transitória e recuperação
    print('\n' + '-' * 70)
    print('[CENÁRIO 2] Resiliência: Falha Transitória e Retentativa com Sucesso')

    from .connectors import SimulatedConnector
    # Configurar conector do financeiro com 1 falha simulada inicial
    fail_connector = SimulatedConnector('analytics_finops', transient_failures=1)
    orchestrator.register_connector('analytics_finops', fail_connector)

    reg2 = orchestrator.register_task(
        'Calcular estimativa orçamentária do projeto',
        target='analytics_finops',
        correlation_id='demo-corr-cenario2-resiliencia',
    )
    print(f"Tarefa registrada: {reg2['task_id']}")

    print('Tentativa 1 (espera-se falha transitória registrada):')
    res1 = orchestrator.step()
    print(f" -> Resposta da tentativa 1: {res1}")

    print('Tentativa 2 (recuperação automática):')
    # Liberar temporizador de backoff no banco para a demonstração
    orchestrator.queue.db.execute("UPDATE bridge_tasks SET available=0 WHERE correlation_id='demo-corr-cenario2-resiliencia'")
    res2 = orchestrator.step()
    print(f" -> Resposta da tentativa 2: {res2}")

    cons2 = orchestrator.consolidate('demo-corr-cenario2-resiliencia')
    print(f"Recuperação confirmada! Status da tarefa={cons2['steps'][0]['state']}, Tentativas={cons2['steps'][0]['attempts']}")

    # Cenário 3: Portão de aprovação humana com autenticação verificável
    print('\n' + '-' * 70)
    print('[CENÁRIO 3] Portão de Aprovação Humana com Autenticação Criptográfica')
    reg3 = orchestrator.register_task(
        'Enviar e-mail para parceiro externo e aprovar compromisso financeiro',
        target='analytics_finops',
        require_approval=True,
        correlation_id='demo-corr-cenario3-aprovacao',
    )
    print(f"Tarefa sensível registrada. Estado={reg3['state']}, Desafio={reg3['challenge'][:16]}...")

    step_blocked = orchestrator.step()
    print(f"Tentativa de execução sem aprovação: {step_blocked} (Nenhuma tarefa executada - aguardando humano)")

    reviewer_id = 'carlos'
    print(f'Tentativa de aprovação forjada ou com token inválido por {reviewer_id}...')
    invalid_sample_auth = 'simulated_unauthorized_value'
    try:
        orchestrator.approve(reg3['task_id'], reviewer_id=reviewer_id, token=invalid_sample_auth)  # nosec B106
    except PermissionError as pe:
        print(f' -> Bloqueado com sucesso por falta de autenticação verificável: {pe}')

    print('Emitindo token criptográfico válido para o revisor autorizado...')
    token = orchestrator.authenticator.generate_token(reviewer_id, reg3['challenge'])
    approved = orchestrator.approve(reg3['task_id'], reviewer_id=reviewer_id, token=token)
    print(f'Aprovação autenticada com sucesso: {approved}')

    step_approved = orchestrator.step()
    print(f"Execução após aprovação autenticada: {step_approved}")

    orchestrator.close()
    print('\n' + '=' * 70)
    print('DEMONSTRAÇÃO CONCLUÍDA COM 100% DE ÊXITO')
    print('=' * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description='Ponte Central — Central-IA-Pessoal')
    parser.add_argument('command', choices=['demo'], nargs='?', default='demo', help='Comando a executar')
    args = parser.parse_args()

    if args.command == 'demo':
        run_demo()


if __name__ == '__main__':
    main()
