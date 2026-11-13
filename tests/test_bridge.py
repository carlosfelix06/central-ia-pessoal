"""Testes de conformidade, resiliência, autenticação e privacidade da Ponte Central."""
import os
import sqlite3
import tempfile
import pytest

from central.bridge.contract import BridgeTask, BridgeEvent, canonical_json
from central.bridge.auth import ApprovalAuthenticator
from central.bridge.connectors import (
    SimulatedConnector,
    OpenAIConnector,
    GeminiConnector,
    GitHubConnector,
    CopilotConnector,
    AntigravityConnector,
    CodexConnector,
)
from central.bridge.queue import BridgeQueue
from central.bridge.orchestrator import BridgeOrchestrator


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix='.sqlite3')
    os.close(fd)
    private_path = path.replace('.sqlite3', '-private.sqlite3')
    yield path
    for p in (path, private_path):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def test_contract_validation():
    task = BridgeTask(
        task_id='t-1',
        correlation_id='c-1',
        source_agent='user',
        target_agent='coordenador_bi',
        authorized_input={'msg': 'olá'},
    )
    task.validate()
    assert task.fingerprint() is not None
    assert task.to_dict()['state'] == 'created'

    # Estado inválido
    task.state = 'invalid_state'
    with pytest.raises(ValueError, match='Estado inválido'):
        task.validate()

    # authorized_input não-dicionário
    task.state = 'created'
    task.authorized_input = 'string_invalida'  # type: ignore
    with pytest.raises(ValueError, match='deve ser um dicionário'):
        task.validate()


def test_queue_idempotency(temp_db):
    queue = BridgeQueue(temp_db)
    task1 = BridgeTask(
        task_id='t-100',
        correlation_id='corr-idemp-1',
        source_agent='user',
        target_agent='coordenador_bi',
        authorized_input={'param': 1},
    )
    queue.submit(task1)

    # Submeter novamente mesma correlação e payload -> retorna mesmo task_id
    task2 = BridgeTask(
        task_id='t-101',
        correlation_id='corr-idemp-1',
        source_agent='user',
        target_agent='coordenador_bi',
        authorized_input={'param': 1},
    )
    res_id = queue.submit(task2)
    assert res_id == 't-100'

    # Submeter mesma correlação com payload diferente -> erro de idempotência
    task3 = BridgeTask(
        task_id='t-102',
        correlation_id='corr-idemp-1',
        source_agent='user',
        target_agent='coordenador_bi',
        authorized_input={'param': 2},
    )
    with pytest.raises(ValueError, match='Idempotência violada'):
        queue.submit(task3)

    queue.close()


def test_end_to_end_local_multi_agent_chain(temp_db):
    """Cenário 1: Usuário registra uma vez -> Coordenador de BI -> Engenheiro de Dados -> Resultado consolidado -> Auditoria."""
    orchestrator = BridgeOrchestrator(db_path=temp_db)

    def sec_handler(task):
        return {
            'agent': 'coordenador_bi',
            'agenda_status': 'agendado',
            'pontos': ['Alinhar arquitetura', 'Revisar requisitos'],
        }

    def tec_handler(task):
        prev = task.authorized_input.get('resultado_anterior', {})
        return {
            'agent': 'engenheiro_dados',
            'especificacao_tecnica': 'Definida com base nos pontos do secretário',
            'pontos_recebidos': prev.get('pontos', []),
        }

    orchestrator.register_connector('coordenador_bi', SimulatedConnector('coordenador_bi', handler=sec_handler))
    orchestrator.register_connector('engenheiro_dados', SimulatedConnector('engenheiro_dados', handler=tec_handler))

    reg = orchestrator.register_task(
        'Planejar reunião técnica e desenhar arquitetura',
        chain=['coordenador_bi', 'engenheiro_dados'],
        correlation_id='corr-e2e-chain',
    )
    assert reg['state'] == 'queued'

    steps = orchestrator.run_until_idle()
    assert len(steps) == 2
    assert steps[0]['agent'] == 'coordenador_bi'
    assert steps[0]['status'] == 'done'
    assert steps[1]['agent'] == 'engenheiro_dados'
    assert steps[1]['status'] == 'done'

    cons = orchestrator.consolidate('corr-e2e-chain')
    assert cons['is_complete'] is True
    assert cons['total_tasks'] == 2
    assert cons['completed_tasks'] == 2

    tec_step = cons['steps'][1]
    assert tec_step['result']['pontos_recebidos'] == ['Alinhar arquitetura', 'Revisar requisitos']

    events = cons['audit_events']
    assert len(events) >= 4

    orchestrator.close()


def test_failure_and_recovery(temp_db):
    """Cenário 2: Falha transitória simulada -> fila marca retryable -> worker recupera e conclui."""
    orchestrator = BridgeOrchestrator(db_path=temp_db)

    fail_connector = SimulatedConnector('analytics_finops', transient_failures=1)
    orchestrator.register_connector('analytics_finops', fail_connector)

    reg = orchestrator.register_task(
        'Apurar despesas do mês',
        target='analytics_finops',
        correlation_id='corr-resilience',
    )

    step1 = orchestrator.step()
    assert step1['status'] == 'retryable'

    task_mid = orchestrator.queue.get_task(reg['task_id'])
    assert task_mid.state == 'retryable'
    assert task_mid.attempt_count == 1
    assert len(task_mid.errors) == 1

    orchestrator.queue.db.execute("UPDATE bridge_tasks SET available=0 WHERE task_id=?", (reg['task_id'],))

    step2 = orchestrator.step()
    assert step2['status'] == 'done'

    task_end = orchestrator.queue.get_task(reg['task_id'])
    assert task_end.state == 'done'
    assert task_end.attempt_count == 2

    orchestrator.close()


def test_verifiable_human_approval_gate(temp_db):
    """Cenário 3: Autenticação criptográfica verificável para portão de aprovação humana."""
    authenticator = ApprovalAuthenticator()
    orchestrator = BridgeOrchestrator(db_path=temp_db, authenticator=authenticator)

    reg = orchestrator.register_task(
        'Transferir recursos e liquidar fatura',
        target='analytics_finops',
        require_approval=True,
        correlation_id='corr-approval',
    )
    assert reg['state'] == 'awaiting_approval'
    challenge = reg['challenge']
    assert challenge is not None

    # 1. Sem aprovação -> nada é executado
    step_blocked = orchestrator.step()
    assert step_blocked is None

    # 2. Tentativa com token inválido ou falsificado -> PermissionError
    with pytest.raises(PermissionError, match='autenticação verificável ausente ou token inválido'):
        orchestrator.approve(reg['task_id'], reviewer_id='bi_lead', token='token_falso')

    # 3. Emissão de token legítimo via autenticador
    valid_token = authenticator.generate_token('bi_lead', challenge)

    # 4. Aprovação verificada -> sucesso
    approved = orchestrator.approve(reg['task_id'], reviewer_id='bi_lead', token=valid_token)
    assert approved is True

    # 5. Execução prossegue e conclui
    step_ok = orchestrator.step()
    assert step_ok['status'] == 'done'

    task_done = orchestrator.queue.get_task(reg['task_id'])
    assert task_done.state == 'done'

    orchestrator.close()


def test_privacy_and_data_isolation(temp_db):
    """Prioridade 3: Verifica que textos sensíveis NÃO ficam em texto claro no banco principal."""
    queue = BridgeQueue(temp_db)
    secret_text = 'CPF 123.456.789-00 - Salário confidencial R$ 25.000,00'
    task = BridgeTask(
        task_id='t-priv-1',
        correlation_id='c-priv-1',
        source_agent='user',
        target_agent='analytics_finops',
        authorized_input={'dados_sensiveis': secret_text},
    )
    queue.submit(task)

    # Inspecionar o banco operacional principal diretamente sem attach
    conn_pub = sqlite3.connect(temp_db)
    conn_pub.row_factory = sqlite3.Row
    row_pub = conn_pub.execute('SELECT * FROM bridge_tasks WHERE task_id=?', ('t-priv-1',)).fetchone()

    # O texto sensível NÃO deve existir em nenhuma coluna do banco principal
    for key in row_pub.keys():
        val = str(row_pub[key])
        assert secret_text not in val

    # As fingerprints devem estar preenchidas
    assert row_pub['input_fingerprint'] is not None

    # O conteúdo deve estar no banco privado
    private_db_path = temp_db.replace('.sqlite3', '-private.sqlite3')
    conn_priv = sqlite3.connect(private_db_path)
    conn_priv.row_factory = sqlite3.Row
    row_priv = conn_priv.execute('SELECT input FROM bridge_content WHERE task_id=?', ('t-priv-1',)).fetchone()
    assert secret_text in row_priv['input']

    conn_pub.close()
    conn_priv.close()
    queue.close()


def test_unified_audit_synchronization(temp_db):
    """Prioridade 1: Verifica sincronização de eventos com as tabelas events (Fase 3) e audit (Fase 1)."""
    queue = BridgeQueue(temp_db)
    task = BridgeTask(
        task_id='t-audit-1',
        correlation_id='c-audit-1',
        source_agent='user',
        target_agent='coordenador_bi',
        authorized_input={'action': 'test_audit'},
    )
    queue.submit(task)

    # 1. Conferir na tabela bridge_events
    b_events = queue.db.execute('SELECT * FROM bridge_events WHERE task_id=?', ('t-audit-1',)).fetchall()
    assert len(b_events) >= 1

    # 2. Conferir sincronização na tabela events (Fase 3)
    f3_events = queue.db.execute('SELECT * FROM events WHERE job=?', ('t-audit-1',)).fetchall()
    assert len(f3_events) >= 1
    assert f3_events[0]['code'] == 'task_submitted_queued'

    # 3. Conferir sincronização na tabela audit (Fase 1)
    f1_audit = queue.db.execute('SELECT * FROM audit WHERE task=?', ('t-audit-1',)).fetchall()
    assert len(f1_audit) >= 1
    assert f1_audit[0]['event'] == 'task_submitted_queued'

    queue.close()


def test_persistence_after_restart(temp_db):
    """Prioridade 5: Testa persistência e recuperação de estado e conteúdo após reinício do processo."""
    q1 = BridgeQueue(temp_db)
    task = BridgeTask(
        task_id='t-persist-1',
        correlation_id='c-persist-1',
        source_agent='user',
        target_agent='engenheiro_dados',
        authorized_input={'code_version': '1.0.0'},
    )
    q1.submit(task)
    q1.close()

    # Reiniciar abrindo uma nova conexão
    q2 = BridgeQueue(temp_db)
    restored = q2.get_task('t-persist-1')
    assert restored is not None
    assert restored.state == 'queued'
    assert restored.authorized_input == {'code_version': '1.0.0'}
    q2.close()


def test_all_connectors_safety():
    """Valida que todos os conectores fornecem interfaces seguras e respeitam proteções."""
    gh = GitHubConnector()
    assert gh.is_available() is True
    res_gh = gh.dispatch(BridgeTask('t-gh', 'c-gh', 'user', 'github', authorized_input={'action': 'test'}))
    assert res_gh['platform'] == 'github'

    copilot = CopilotConnector()
    assert copilot.is_available() is True
    res_cp = copilot.dispatch(BridgeTask('t-cp', 'c-cp', 'user', 'copilot', authorized_input={}))
    assert res_cp['agent'] == 'copilot'

    ag = AntigravityConnector()
    assert ag.is_available() is True
    res_ag = ag.dispatch(BridgeTask('t-ag', 'c-ag', 'user', 'antigravity', authorized_input={'code': 'test'}))
    assert res_ag['agent'] == 'antigravity'

    cdx = CodexConnector()
    assert cdx.is_available() is True
    res_cdx = cdx.dispatch(BridgeTask('t-cdx', 'c-cdx', 'user', 'codex', authorized_input={'spec': 'test'}))
    assert res_cdx['agent'] == 'codex'

    oa = OpenAIConnector(mode='simulated')
    assert oa.is_available() is True
    res_oa = oa.dispatch(BridgeTask('t-oa', 'c-oa', 'user', 'openai', authorized_input={}))
    assert res_oa['provider'] == 'openai'

    gem = GeminiConnector(mode='simulated')
    assert gem.is_available() is True
    res_gem = gem.dispatch(BridgeTask('t-gem', 'c-gem', 'user', 'gemini', authorized_input={}))
    assert res_gem['provider'] == 'gemini'

    oa_live = OpenAIConnector(mode='live', api_key=None)
    with pytest.raises(PermissionError):
        oa_live.dispatch(BridgeTask('t-oa-live', 'c-oa-live', 'user', 'openai', authorized_input={}))


def test_all_six_specialists_chain_and_handoff(temp_db):
    """Check 2: Confirma que os seis especialistas podem ser selecionados e o handoff transmite resultados."""
    orchestrator = BridgeOrchestrator(db_path=temp_db)
    all_six = ['coordenador_bi', 'analista_bi', 'analytics_finops', 'engenheiro_dados', 'especialista_dataviz', 'auditor_qualidade']

    reg = orchestrator.register_task(
        'Planejamento integral da carreira e rotina técnica e familiar',
        chain=all_six,
        correlation_id='corr-all-six-chain',
    )
    assert reg['state'] == 'queued'

    steps = orchestrator.run_until_idle(max_steps=10)
    assert len(steps) == 6

    executed_agents = [s['agent'] for s in steps]
    assert executed_agents == all_six
    assert all(s['status'] == 'done' for s in steps)

    cons = orchestrator.consolidate('corr-all-six-chain')
    assert cons['is_complete'] is True
    assert cons['total_tasks'] == 6
    assert cons['completed_tasks'] == 6

    orchestrator.close()


def test_github_issue_bridge_lifecycle_mocked(temp_db):
    """Check 7: Valida ciclo completo GitHub Issue -> Bridge -> Especialista -> Resultado -> Atualização da Issue (Mock)."""
    orchestrator = BridgeOrchestrator(db_path=temp_db)

    issue_number = 42
    issue_title = 'Investigar divergência no cálculo orçamentário'
    issue_body = 'O total de despesas projetadas diverge das regras contábeis locais.'

    reg = orchestrator.register_task(
        payload={'text': issue_body, 'issue_number': issue_number, 'issue_title': issue_title},
        source=f'github:issue#{issue_number}',
        target='analytics_finops',
        correlation_id=f'gh-issue-{issue_number}',
        metadata={'issue_number': issue_number, 'issue_title': issue_title},
    )
    assert reg['state'] == 'queued'

    step1 = orchestrator.step()
    assert step1['status'] == 'done'
    assert step1['agent'] == 'analytics_finops'
    diag_summary = step1['result']['summary']

    update_task = BridgeTask(
        task_id=f'task-gh-comment-{issue_number}',
        correlation_id=f'gh-issue-{issue_number}',
        source_agent='analytics_finops',
        target_agent='github',
        authorized_input={
            'action': 'issue_comment',
            'issue_number': issue_number,
            'comment_body': f'Diagnóstico técnico concluído: {diag_summary}',
        },
    )
    orchestrator.queue.submit(update_task)

    step2 = orchestrator.step()
    assert step2['status'] == 'done'
    assert step2['agent'] == 'github'
    assert step2['result']['platform'] == 'github'
    assert step2['result']['action'] == 'issue_comment'
    assert 'gh-recibo-' in step2['result']['receipt_id']

    cons = orchestrator.consolidate(f'gh-issue-{issue_number}')
    assert cons['is_complete'] is True
    assert cons['completed_tasks'] == 2
    assert cons['steps'][0]['target'] == 'analytics_finops'
    assert cons['steps'][1]['target'] == 'github'

    orchestrator.close()
