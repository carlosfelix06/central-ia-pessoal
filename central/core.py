import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone

DEFAULT_TEAM = 'equipe_bi'

AGENTS = {
    'coordenador_bi': ('Coordenador de BI', ('kpi', 'indicador', 'meta', 'governança', 'agenda', 'reunião', 'email', 'organizar')),
    'analista_bi': ('Analista de BI', ('análise', 'cubo', 'dax', 'tendência', 'vaga', 'currículo', 'carreira', 'emprego')),
    'analytics_finops': ('Analytics & FinOps', ('roi', 'custo', 'margem', 'receita', 'gasto', 'orçamento', 'despesa', 'finança')),
    'engenheiro_dados': ('Engenheiro de Dados', ('etl', 'pipeline', 'sql', 'warehouse', 'python', 'mdm', 'golden record', 'deduplicação', 'computador', 'erro', 'software')),
    'especialista_dataviz': ('Especialista DataViz', ('dashboard', 'painel', 'power bi', 'gráfico', 'estudar', 'aprender', 'curso', 'aula')),
    'auditor_qualidade': ('Auditor de Qualidade & LGPD', ('qualidade', 'lgpd', 'anomalia', 'compliance', 'stewardship', 'linhagem', 'família', 'filho', 'casa', 'auditor_qualidade')),
}


class Store:
    def __init__(self, path='central.sqlite3'):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, agent TEXT, state TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS audit (
          at TEXT, task TEXT, event TEXT, fingerprint TEXT);
        CREATE TABLE IF NOT EXISTS charges (
          id TEXT PRIMARY KEY, day TEXT, amount INTEGER);
        ''')

    def close(self):
        self.db.close()

    def audit(self, task, event, text=''):
        self.db.execute('INSERT INTO audit VALUES (?,?,?,?)',
            (datetime.now(timezone.utc).isoformat(), task, event,
             hashlib.sha256(text.encode()).hexdigest()))

    def reserve(self, task, amount, daily_limit, call_limit):
        # Unidades inteiras: micros de dólar. Reserva persistente e atômica.
        if any(type(v) is not int or v < 0 for v in (amount, daily_limit, call_limit)):
            raise ValueError('Limites devem ser inteiros não negativos')
        if amount <= 0 or amount > call_limit:
            raise PermissionError('Limite por chamada excedido ou não configurado')
        day = datetime.now(timezone.utc).date().isoformat()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            spent = self.db.execute('SELECT COALESCE(SUM(amount),0) FROM charges WHERE day=?', (day,)).fetchone()[0]
            if spent + amount > daily_limit:
                raise PermissionError('Orçamento diário esgotado')
            self.db.execute('INSERT INTO charges VALUES (?,?,?)', (task, day, amount))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise


class Orchestrator:
    def __init__(self, store, provider, *, allow_paid=False, grants=(),
                 reserve_microusd=0, daily_microusd=0, call_microusd=0):
        self.store, self.provider = store, provider
        self.allow_paid, self.grants = allow_paid, frozenset(grants)
        self.limits = (reserve_microusd, daily_microusd, call_microusd)

    def run(self, text, agents=None, action='draft'):
        if not text.strip() or len(text) > 12000:
            raise ValueError('Informe entre 1 e 12000 caracteres')
        if agents is None:
            agents = [a for a, (_, words) in AGENTS.items() if any(w in text.lower() for w in words)] or ['coordenador_bi']
        agents = list(dict.fromkeys(agents))
        if not agents or len(agents) > 6 or any(a not in AGENTS for a in agents):
            raise ValueError('Seleção de agentes inválida')
        results = {}
        for agent in agents:
            task = uuid.uuid4().hex
            self.store.db.execute('INSERT INTO tasks VALUES (?,?,?,NULL)', (task, agent, 'pending'))
            self.store.audit(task, 'created', text)
            try:
                # Não existe executor de ações externas nesta fase.
                if action != 'draft' or (agent, action) not in self.grants:
                    raise PermissionError('Ação não permitida')
                if self.provider.name != 'mock':
                    if not self.allow_paid:
                        raise PermissionError('Modo pago bloqueado')
                    self.store.reserve(task, *self.limits)
                    self.store.audit(task, 'budget_reserved')
                result = self.provider.generate(AGENTS[agent][0], text)
                self.store.db.execute('UPDATE tasks SET state=?, result=? WHERE id=?', ('done', result, task))
                self.store.audit(task, 'done')
                results[agent] = result
            except Exception as exc:
                self.store.db.execute('UPDATE tasks SET state=? WHERE id=?', ('failed', task))
                self.store.audit(task, 'failed:' + type(exc).__name__)
                raise
        # Encaminhamento interno automático, sem copiar respostas entre agentes.
        return results
