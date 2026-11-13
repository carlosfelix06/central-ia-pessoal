"""Fluxo offline fechado: sem provedores, rede, ferramentas ou código de modelo."""
import argparse
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import time
from datetime import datetime, timezone

CHAIN = ('coordenador_bi_executivo', 'analista_bi', 'analytics_finops', 'orquestrador')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class TransientFailure(Exception):
    pass


def process(agent, received):
    """Regras fictícias locais. Valores financeiros são hipóteses, não alíquotas."""
    if agent == CHAIN[0]:
        return {'proposta': received, 'decisao': 'encaminhar para análise profissional',
                'pendencias': ['Confirmar escopo, jornada e modalidade de trabalho']}
    if agent == CHAIN[1]:
        return {'triagem': received,
                'decisao': 'avaliar custos antes de negociar; não aceitar automaticamente',
                'perguntas': ['Prazo e rescisão?', 'Exclusividade?', 'Férias e reajuste?',
                              'Deslocamento ou mudança para Matriz-SP?'],
                'avaliacao': 'Sem currículo ou descrição da vaga, aderência e mercado não avaliados'}
    if agent == CHAIN[2]:
        proposal = received['triagem']['proposta']
        gross = Decimal(proposal['mensal_brl'])
        # Cenário didático deliberadamente explícito, sem regras tributárias reais.
        provision = (gross * Decimal('0.20')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        balance = gross - provision - Decimal('500.00') - Decimal('1000.00')
        return {'profissional': received, 'bruto_mensal_brl': str(gross),
                'hipoteses': {'provisao_generica_percentual': '20', 'provisao_brl': str(provision),
                             'contabilidade_brl': '500.00', 'deslocamento_brl': '1000.00'},
                'saldo_ilustrativo_brl': str(balance),
                'decisao': 'solicitar custos reais e validação contábil antes da decisão',
                'limite': 'Saldo não é salário líquido nem estimativa tributária; faltam benefícios, férias, previdência e custos reais'}
    if agent == CHAIN[3]:
        return {'analise_financeira': received, 'decisao': 'preparar perguntas e negociação para revisão humana',
                'acoes_externas_executadas': [], 'custo_api_usd': '0.00',
                'conclusao': 'Projeto analítico fictício de BI de R$ 19.000 na Matriz-SP requer esclarecimentos; nenhuma aceitação enviada'}
    raise ValueError('Agente fora do fluxo autorizado')


class Workflow:
    def __init__(self, store, *, failures=None):
        self.db = store.db
        self.failures = dict(failures or {})  # Injeção local de falhas para validação.
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS workflows (
          id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL,
          state TEXT NOT NULL, max_attempts INTEGER, max_executions INTEGER, executions INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS workflow_steps (
          run TEXT, position INTEGER, agent TEXT, state TEXT, attempts INTEGER DEFAULT 0,
          received TEXT, result TEXT, PRIMARY KEY(run, position));
        CREATE TABLE IF NOT EXISTS workflow_events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, at TEXT, agent TEXT,
          state TEXT, detail TEXT);
        ''')

    def event(self, run, agent, state, detail):
        self.db.execute('INSERT INTO workflow_events(run,at,agent,state,detail) VALUES(?,?,?,?,?)',
                        (run, datetime.now(timezone.utc).isoformat(), agent, state, encode(detail)))

    def create(self, key, proposal, *, max_attempts=3, max_executions=12):
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ValueError('Chave de idempotência inválida')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError('Tentativas: 1 a 5')
        if type(max_executions) is not int or not 1 <= max_executions <= 20:
            raise ValueError('Execuções: 1 a 20')
        if set(proposal) != {'mensal_brl', 'cidade', 'contrato'}:
            raise ValueError('Campos da proposta inválidos')
        value = Decimal(proposal['mensal_brl'])
        if not value.is_finite() or not 0 < value <= 1000000 or proposal['contrato'] != 'PJ':
            raise ValueError('Proposta inválida')
        if not isinstance(proposal['cidade'], str) or not 1 <= len(proposal['cidade']) <= 120:
            raise ValueError('Cidade inválida')
        proposal = dict(proposal, mensal_brl=str(value.quantize(Decimal('0.01'))))
        request = encode(proposal)
        fingerprint = hashlib.sha256(request.encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT fingerprint,max_attempts,max_executions FROM workflows WHERE id=?', (key,)).fetchone()
            if old:
                if old != (fingerprint, max_attempts, max_executions):
                    raise ValueError('Chave já utilizada com conteúdo ou limites diferentes')
            else:
                self.db.execute('INSERT INTO workflows VALUES(?,?,?,?,?,?,0)',
                                (key, fingerprint, request, 'pending', max_attempts, max_executions))
                for pos, agent in enumerate(CHAIN):
                    self.db.execute('INSERT INTO workflow_steps VALUES(?,?,?,?,0,NULL,NULL)', (key, pos, agent, 'pending'))
                self.event(key, 'orquestrador', 'pending', {'decisao': 'iniciar cadeia offline fixa'})
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return key

    def execute(self, key, *, max_ticks=20, max_seconds=10):
        if type(max_ticks) is not int or not 1 <= max_ticks <= 20 or not 0 < max_seconds <= 60:
            raise ValueError('Limites de execução inválidos')
        started = time.monotonic()
        for _ in range(max_ticks):
            if time.monotonic() - started >= max_seconds:
                break
            # Transação curta cobre cálculo puro e checkpoint: outro trabalhador não duplica a etapa.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                run = self.db.execute('SELECT state,max_attempts,max_executions,executions,request FROM workflows WHERE id=?', (key,)).fetchone()
                if not run:
                    raise KeyError(key)
                state, attempts_limit, executions_limit, executions, request = run
                if state in ('done', 'failed', 'limited'):
                    self.db.execute('COMMIT')
                    break
                row = self.db.execute("SELECT position,agent,attempts FROM workflow_steps WHERE run=? AND state!='done' ORDER BY position LIMIT 1", (key,)).fetchone()
                pos, agent, attempts = row
                if executions >= executions_limit:
                    self.db.execute("UPDATE workflows SET state='limited' WHERE id=?", (key,))
                    self.event(key, agent, 'limited', {'decisao': 'limite total atingido'})
                    self.db.execute('COMMIT')
                    break
                received = json.loads(request) if pos == 0 else json.loads(self.db.execute(
                    'SELECT result FROM workflow_steps WHERE run=? AND position=?', (key, pos - 1)).fetchone()[0])
                self.db.execute("UPDATE workflows SET state='running',executions=executions+1 WHERE id=?", (key,))
                self.db.execute("UPDATE workflow_steps SET state='running',attempts=attempts+1,received=? WHERE run=? AND position=?", (encode(received), key, pos))
                self.event(key, agent, 'running', {'tentativa': attempts + 1, 'recebido': received})
                try:
                    if self.failures.get(agent, 0) > 0:
                        self.failures[agent] -= 1
                        raise TransientFailure('Falha simulada')
                    result = process(agent, deepcopy(received))
                    self.db.execute("UPDATE workflow_steps SET state='done',result=? WHERE run=? AND position=?", (encode(result), key, pos))
                    self.event(key, agent, 'done', {'resultado': result, 'decisao': result['decisao']})
                    if pos == len(CHAIN) - 1:
                        self.db.execute("UPDATE workflows SET state='done' WHERE id=?", (key,))
                except Exception as exc:
                    retry = isinstance(exc, TransientFailure) and attempts + 1 < attempts_limit
                    new_state = 'retryable' if retry else 'failed'
                    self.db.execute('UPDATE workflow_steps SET state=? WHERE run=? AND position=?', (new_state, key, pos))
                    self.db.execute('UPDATE workflows SET state=? WHERE id=?', (new_state, key))
                    self.event(key, agent, new_state, {'erro': type(exc).__name__, 'decisao': 'tentar novamente' if retry else 'interromper fluxo'})
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return self.snapshot(key)

    def snapshot(self, key):
        row = self.db.execute('SELECT state,executions FROM workflows WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        steps = self.db.execute('SELECT agent,state,attempts,received,result FROM workflow_steps WHERE run=? ORDER BY position', (key,)).fetchall()
        events = self.db.execute('SELECT sequence,at,agent,state,detail FROM workflow_events WHERE run=? ORDER BY sequence', (key,)).fetchall()
        return {'id': key, 'state': row[0], 'executions': row[1], 'api_calls': 0,
                'steps': [dict(agent=a, state=s, attempts=n, received=json.loads(i) if i else None,
                               result=json.loads(o) if o else None) for a,s,n,i,o in steps],
                'events': [dict(sequence=n, at=t, agent=a, state=s, detail=json.loads(d)) for n,t,a,s,d in events]}


def markdown_report(snapshot):
    lines = ['# Fase 2 — relatório de execução fictícia', '',
             f"Fluxo: `{snapshot['id']}`. Estado: **{snapshot['state']}**. "
             f"Execuções de etapas: **{snapshot['executions']}**. Chamadas API: **0**. Custo API: **US$ 0,00**.", '',
             'Cadeia: Coordenador Executivo de BI → Analista de BI → Analytics & FinOps → Orquestrador.', '',
             'Este relatório foi montado a partir das mensagens, resultados e eventos persistidos no SQLite.', '',
             '## O que cada agente recebeu e devolveu', '']
    for step in snapshot['steps']:
        lines += [f"### {step['agent']}", '',
                  f"Estado: {step['state']}. Tentativas: {step['attempts']}.", '',
                  'Recebeu automaticamente:', '', '```json',
                  json.dumps(step['received'], ensure_ascii=False, indent=2), '```', '',
                  'Processou por regras locais determinísticas e devolveu:', '', '```json',
                  json.dumps(step['result'], ensure_ascii=False, indent=2), '```', '']
    lines += ['## Trilha de estados e decisões', '', '| Evento | Horário UTC | Agente | Estado | Decisão/erro |',
              '|---|---|---|---|---|']
    for event in snapshot['events']:
        detail = event['detail']
        decision = detail.get('decisao', '')
        error = detail.get('erro', '')
        lines.append(f"| {event['sequence']} | {event['at']} | {event['agent']} | {event['state']} | {decision} {error} |")
    lines += ['', '## Interpretação do cenário', '',
              'Hipóteses exclusivamente fictícias: R$ 19.000,00 brutos; provisão genérica de 20% '
              '(R$ 3.800,00), contabilidade de R$ 500,00 e deslocamento de R$ 1.000,00. '
              'Saldo aritmético ilustrativo: R$ 13.700,00. Não é salário líquido nem cálculo tributário.', '',
              'Decisão: preparar perguntas e negociação para revisão humana. Nenhuma proposta aceita, '
              'mensagem enviada ou transação realizada. A falha transitória no Analista de BI foi inserida '
              'deliberadamente para demonstrar recuperação.', '',
              '## Limitações e produção', '',
              'Agentes são funções locais com regras fixas; não há inferência por IA, pesquisa de mercado ou '
              'avaliação de currículo. A cadeia é fixa e as mensagens anteriores são encaminhadas integralmente.', '',
              'SQLite e auditoria não são criptografados nem invioláveis. Idempotência depende de reutilizar '
              'a mesma chave para o mesmo pedido. Chaves diferentes representam pedidos novos. '
              'Cálculos são puros e transacionais; interrupção antes do commit permite recalcular sem efeito externo.', '',
              'O limite de tempo é cooperativo entre etapas e por invocação; não interrompe uma função travada. '
              'Tentativas e limite total de execuções confirmadas são persistentes; crashes com rollback não consomem '
              'essa cota. Fluxos failed/limited são terminais. Não há fila de serviço, agendador ou backoff.', '',
              'Antes de produção: autenticação e permissões por usuário/ação; segregação e minimização de dados; '
              'retenção, backup e restauração; migrações versionadas; supervisão com timeout real e limites de '
              'reinício; fila durável, backoff e tratamento de contenção do banco; monitoramento; aprovação humana '
              'vinculada ao conteúdo; idempotência dos efeitos externos e reconciliação de falhas ambíguas. '
              'Para APIs: validar modelos, contratos de resposta, custos reais, cotas dos provedores e proteção de '
              'credenciais. Nenhuma dessas integrações está ativada nesta demonstração.', '']
    return '\n'.join(lines)


def main():
    from .core import Store
    parser = argparse.ArgumentParser(description='Demonstração fictícia offline da fase 2')
    parser.add_argument('--db', default='fase2.sqlite3')
    parser.add_argument('--report', default='relatorio-fase2.json')
    args = parser.parse_args()
    store = Store(args.db)
    try:
        workflow = Workflow(store, failures={'analista_bi': 1})
        key = workflow.create('demo-bi-pipeline-19000-v1', {'mensal_brl': '19000.00', 'cidade': 'Matriz-SP', 'contrato': 'PJ'})
        result = workflow.execute(key)
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        Path(args.report).with_suffix('.md').write_text(markdown_report(result), encoding='utf-8')
        print(encode({'state': result['state'], 'executions': result['executions'], 'api_calls': 0, 'report': args.report}))
    finally:
        store.close()


if __name__ == '__main__':
    main()
