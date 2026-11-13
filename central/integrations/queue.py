"""Routing extension; reuses phase3 persistence, reservations, recovery and approvals."""
from dataclasses import asdict
import json
import time
from central.phase3.queue import Queue
from .policy import PRICES, Route, cost, estimate_route


class RoutedQueue(Queue):
    def __init__(self, directory, settings, *, provider_budgets=None, rpm=None):
        self.provider_budgets = dict(provider_budgets if provider_budgets is not None else {p:settings.budget_microusd for p in PRICES})
        self.rpm = dict(rpm if rpm is not None else {p:2 for p in PRICES})
        if set(self.provider_budgets)!=set(PRICES) or set(self.rpm)!=set(PRICES):
            raise ValueError('Defina limites dos dois provedores')
        if any(type(n) is not int or not 0<=n<=settings.budget_microusd for n in self.provider_budgets.values()):
            raise ValueError('Cota de provedor inválida')
        if any(type(n) is not int or not 1<=n<=60 for n in self.rpm.values()):
            raise ValueError('RPM inválido')
        self.authorizer = None
        self._pending_routes = None
        super().__init__(directory,settings)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS task_routes(job TEXT PRIMARY KEY, provider TEXT, route TEXT);
        CREATE TABLE IF NOT EXISTS route_attempts(job TEXT, attempt INTEGER, provider TEXT, at REAL,
          PRIMARY KEY(job,attempt));
        ''')

    def policy_document(self, settings):
        result = super().policy_document(settings)
        result.update(schema=2,kind='explicit-provider-routing',provider_prices=PRICES,
                      provider_budgets=self.provider_budgets,rpm=self.rpm)
        return result

    def submit(self, key, text, agents=('coordenador_bi','analista_bi','analytics_finops'), *, routes=None):
        agents = tuple(agents)
        if routes is None or len(routes)!=len(agents) or any(not isinstance(r,Route) for r in routes):
            raise ValueError('Uma rota explícita por tarefa é obrigatória')
        for prev,current in zip(routes,routes[1:]):
            if prev.provider!=current.provider and not current.allow_cross_provider:
                raise PermissionError('Passagem para outro provedor exige consentimento explícito')
        self._pending_routes=tuple(routes)
        try:
            return super().submit(key,text,agents)
        finally:
            self._pending_routes=None

    def submission_metadata(self):
        return [asdict(r) for r in self._pending_routes] if self._pending_routes else None

    def store_job_metadata(self, job, position):
        if self._pending_routes is None:
            raise PermissionError('Rota ausente; não existe fallback')
        route = self._pending_routes[position]
        self.db.execute('INSERT INTO task_routes VALUES(?,?,?)',(job,route.provider,json.dumps(asdict(route))))
        self.event(job,'route_'+route.provider)

    def route(self, job):
        row=self.db.execute('SELECT route FROM task_routes WHERE job=?',(job['id'],)).fetchone()
        if row is None:
            raise PermissionError('Rota persistida ausente; sem fallback')
        return Route(**json.loads(row[0]))

    def estimate_task(self, job, text):
        return estimate_route(self.settings,self.route(job),job['agent'],text)

    def task_ready(self, job, estimated):
        route=self.route(job)
        if self.settings.mode=='live':
            if self.authorizer is None:
                raise PermissionError('Despacho real exige trabalhador autorizado')
            self.authorizer(route)
        used=self.db.execute('SELECT COALESCE(SUM(MAX(c.reserved,COALESCE(c.actual,0))),0) FROM consumption c '
                             'JOIN task_routes r ON r.job=c.job WHERE r.provider=?',(route.provider,)).fetchone()[0]
        if used+estimated['reserve_microusd']>self.provider_budgets[route.provider]:
            self.db.execute("UPDATE jobs SET state='budget_blocked' WHERE id=?",(job['id'],))
            self.event(job['id'],'provider_budget_blocked')
            return False
        now=time.time()
        times=[r[0] for r in self.db.execute('SELECT at FROM route_attempts WHERE provider=? AND at>? ORDER BY at',
                                            (route.provider,now-60))]
        if len(times)>=self.rpm[route.provider]:
            self.db.execute('UPDATE jobs SET available=? WHERE id=?',(times[0]+60.01,job['id']))
            self.event(job['id'],'provider_rate_wait')
            return False
        return True

    def on_claim(self, job, attempt):
        route=self.route(job)
        self.db.execute('INSERT INTO route_attempts VALUES(?,?,?,?)',(job['id'],attempt,route.provider,time.time()))

    def task_cost(self, job, input_tokens, output_tokens):
        return cost(self.route(job),input_tokens,output_tokens)

    def task_output_limit(self, job):
        return self.route(job).max_output_tokens

    def route_status(self, run):
        return [dict(s,provider=self.route(s).provider,model=self.route(s).model) for s in self.status(run)]
