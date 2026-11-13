from contextlib import contextmanager
from dataclasses import asdict
import hashlib
from pathlib import Path
import sqlite3
import time
import uuid
from .policy import AGENTS, PRICES, canonical, estimate, cost_microusd


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def stable(value):
    return uuid.uuid5(uuid.NAMESPACE_URL, value).hex


class Queue:
    """Uma polÃ­tica fixa por banco. ConteÃºdos em arquivo privado separado via ATTACH."""
    def __init__(self, directory, settings):
        self.settings = settings
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / 'operations.sqlite3').exists() != (folder / 'private.sqlite3').exists():
            raise ValueError('Par de bancos incompleto; restaurar ambos antes de retomar')
        self.db = sqlite3.connect(folder / 'operations.sqlite3', isolation_level=None, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('ATTACH DATABASE ? AS private', (str(folder / 'private.sqlite3'),))
        self.db.execute('PRAGMA private.journal_mode=DELETE')
        self.db.execute('PRAGMA private.synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT, paused INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, run TEXT, position INTEGER, agent TEXT, state TEXT,
          attempts INTEGER DEFAULT 0, available REAL DEFAULT 0, lease REAL,
          owner TEXT, cancel INTEGER DEFAULT 0, UNIQUE(run,position));
        CREATE TABLE IF NOT EXISTS events(
          sequence INTEGER PRIMARY KEY, at REAL, job TEXT, code TEXT);
        CREATE TABLE IF NOT EXISTS consumption(
          job TEXT, attempt INTEGER, reserved INTEGER, actual INTEGER,
          input_tokens INTEGER, output_tokens INTEGER, outcome TEXT,
          PRIMARY KEY(job,attempt));
        CREATE TABLE IF NOT EXISTS actions(
          id TEXT PRIMARY KEY, job TEXT, kind TEXT, fingerprint TEXT, state TEXT,
          expires REAL, reviewer TEXT);
        CREATE TABLE IF NOT EXISTS private.content(id TEXT PRIMARY KEY, input TEXT, output TEXT);
        CREATE TABLE IF NOT EXISTS private.requests(id TEXT PRIMARY KEY, text TEXT);
        CREATE TABLE IF NOT EXISTS private.action_content(id TEXT PRIMARY KEY, payload TEXT);
        ''')
        value = canonical(self.policy_document(settings))
        try:
            with self.transaction():
                old = self.db.execute('SELECT value FROM policy WHERE id=1').fetchone()
                if old and old['value'] != value:
                    raise ValueError('PolÃ­tica diferente: use pasta prÃ³pria; nÃ£o misture mock e live')
                self.db.execute('INSERT OR IGNORE INTO policy(id,value) VALUES(1,?)', (value,))
        except BaseException:
            self.db.close()
            raise

    def policy_document(self, settings):
        return {'schema': 1, 'settings': asdict(settings), 'prices': PRICES}

    def submission_metadata(self):
        return None

    def store_job_metadata(self, job, position):
        pass

    def estimate_task(self, job, text):
        return estimate(self.settings, job['agent'], text)

    def task_ready(self, job, estimated):
        return True

    def on_claim(self, job, attempt):
        pass

    def task_cost(self, job, input_tokens, output_tokens):
        return cost_microusd(self.settings.model, input_tokens, output_tokens)

    def task_output_limit(self, job):
        return self.settings.max_output_tokens

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

    def event(self, job, code):
        # Somente cÃ³digos definidos pelo programa; nunca exceÃ§Ãµes, prompts ou cabeÃ§alhos.
        self.db.execute('INSERT INTO events(at,job,code) VALUES(?,?,?)', (time.time(), job, code))

    def submit(self, key, text, agents=('coordenador_bi', 'analista_bi', 'analytics_finops')):
        agents = tuple(agents)
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.strip():
            raise ValueError('Chave invÃ¡lida')
        if not 1 <= len(agents) <= 6 or len(set(agents)) != len(agents) or any(a not in AGENTS for a in agents):
            raise ValueError('Cadeia invÃ¡lida: selecione atÃ© seis especialistas, sem ciclos')
        estimate(self.settings, agents[0], text)
        run = stable('phase3:' + digest(key))
        request_identity = {'text': text, 'agents': agents}
        if self.submission_metadata() is not None:
            request_identity['routing'] = self.submission_metadata()
        fingerprint = digest(canonical(request_identity))
        with self.transaction():
            old = self.db.execute('SELECT fingerprint FROM runs WHERE id=?', (run,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError('Chave reutilizada com pedido diferente')
                return run
            self.db.execute('INSERT INTO runs VALUES(?,?)', (run, fingerprint))
            self.db.execute('INSERT INTO private.requests VALUES(?,?)', (run, text))
            for pos, agent in enumerate(agents):
                job = stable(run + ':' + str(pos))
                self.db.execute('INSERT INTO jobs(id,run,position,agent,state) VALUES(?,?,?,?,?)',
                                (job, run, pos, agent, 'queued' if pos == 0 else 'waiting'))
                self.db.execute('INSERT INTO private.content VALUES(?,?,NULL)', (job, text if pos == 0 else None))
                self.store_job_metadata(job, pos)
                self.event(job, 'queued' if pos == 0 else 'waiting')
        return run

    def budget(self):
        used = self.db.execute('SELECT COALESCE(SUM(MAX(reserved,COALESCE(actual,0))),0) FROM consumption').fetchone()[0]
        paused = bool(self.db.execute('SELECT paused FROM policy WHERE id=1').fetchone()[0])
        return {'limit_microusd': self.settings.budget_microusd, 'held_microusd': used,
                'remaining_microusd': max(0, self.settings.budget_microusd - used), 'paused': paused}

    def recover(self):
        with self.transaction():
            rows = self.db.execute("SELECT id FROM jobs WHERE state='running' AND lease<=?", (time.time(),)).fetchall()
            for row in rows:
                self.db.execute("UPDATE jobs SET state='uncertain',owner=NULL WHERE id=?", (row['id'],))
                self.db.execute("UPDATE consumption SET outcome='uncertain' WHERE job=? AND outcome='reserved'", (row['id'],))
                self.event(row['id'], 'recovery_uncertain_no_resend')
        return len(rows)

    def claim(self):
        self.recover()
        with self.transaction():
            job = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','retryable') AND cancel=0 AND available<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not job:
                return None
            job = dict(job)
            text = self.db.execute('SELECT input FROM private.content WHERE id=?', (job['id'],)).fetchone()[0]
            try:
                estimated = self.estimate_task(job, text)
            except ValueError:
                self.db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job['id'],))
                self.event(job['id'], 'input_limit')
                return None
            if not self.task_ready(job, estimated):
                return None
            budget = self.budget()
            reserve = estimated['reserve_microusd']
            if budget['paused'] or reserve > budget['remaining_microusd'] or reserve > self.settings.call_microusd:
                self.db.execute("UPDATE jobs SET state='budget_blocked' WHERE id=?", (job['id'],))
                self.event(job['id'], 'budget_blocked')
                return None
            if job['attempts'] >= self.settings.max_attempts:
                self.db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job['id'],))
                self.event(job['id'], 'attempts_exhausted')
                return None
            owner = uuid.uuid4().hex
            attempt = job['attempts'] + 1
            self.db.execute("UPDATE jobs SET state='running',attempts=?,owner=?,lease=? WHERE id=?", 
                (attempt, owner, time.time() + self.settings.timeout_seconds + 5, job['id']))
            self.db.execute('INSERT INTO consumption VALUES(?,?,?,NULL,NULL,NULL,?)', (job['id'], attempt, reserve, 'reserved'))
            self.on_claim(job, attempt)
            self.event(job['id'], 'running_reserved')
            return dict(job, text=text, owner=owner, attempts=attempt, estimate=estimated)

    def is_cancelled(self, job):
        row = self.db.execute('SELECT cancel,state,owner FROM jobs WHERE id=?', (job['id'],)).fetchone()
        return row['cancel'] or row['state'] != 'running' or row['owner'] != job['owner']

    def finish(self, job, outcome):
        with self.transaction():
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if row['state'] != 'running' or row['owner'] != job['owner']:
                return False  # Resultado atrasado nÃ£o altera checkpoint de outro supervisor.
            code = outcome['code']
            if row['cancel']:
                code = 'cancelled_unknown'
            state = {'ok': 'done', 'rate_limited': 'retryable', 'rejected': 'failed',
                     'disabled': 'failed', 'invalid': 'uncertain', 'timeout': 'uncertain',
                     'unknown': 'uncertain', 'cancelled_unknown': 'cancelled_unknown'}[code]
            if state == 'retryable' and row['attempts'] >= self.settings.max_attempts:
                state = 'failed'
            actual = input_tokens = output_tokens = None
            if code == 'ok':
                input_tokens, output_tokens = outcome['input_tokens'], outcome['output_tokens']
                actual = self.task_cost(dict(row), input_tokens, output_tokens)
                reserved = self.db.execute('SELECT reserved FROM consumption WHERE job=? AND attempt=?',
                                          (job['id'], row['attempts'])).fetchone()[0]
                if actual > reserved or output_tokens > self.task_output_limit(dict(row)):
                    self.db.execute('UPDATE policy SET paused=1 WHERE id=1')
                    state = 'uncertain'
                    self.event(job['id'], 'usage_overrun_circuit_open')
                else:
                    self.db.execute('UPDATE private.content SET output=? WHERE id=?', (outcome['text'], job['id']))
            self.db.execute('UPDATE consumption SET actual=?,input_tokens=?,output_tokens=?,outcome=? WHERE job=? AND attempt=?',
                (actual, input_tokens, output_tokens, code, job['id'], row['attempts']))
            self.db.execute('UPDATE jobs SET state=?,owner=NULL,available=? WHERE id=?',
                (state, time.time() + min(30, 2 ** row['attempts']) if state == 'retryable' else 0, job['id']))
            self.event(job['id'], state)
            if state == 'done':
                next_job = self.db.execute('SELECT id FROM jobs WHERE run=? AND position=? AND state=?',
                    (row['run'], row['position'] + 1, 'waiting')).fetchone()
                if next_job:
                    original = self.db.execute('SELECT text FROM private.requests WHERE id=?', (row['run'],)).fetchone()[0]
                    message = canonical({'pedido_original': original, 'agente_anterior': row['agent'], 'resultado_anterior': outcome['text']})
                    self.db.execute('UPDATE private.content SET input=? WHERE id=?', (message, next_job['id']))
                    self.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (next_job['id'],))
                    self.event(next_job['id'], 'handoff_queued')
            return True

    def cancel(self, run):
        with self.transaction():
            for row in self.db.execute('SELECT id,state FROM jobs WHERE run=?', (run,)).fetchall():
                if row['state'] in ('done', 'failed', 'uncertain', 'cancelled', 'cancelled_unknown'):
                    self.db.execute('UPDATE jobs SET cancel=1 WHERE id=?', (row['id'],))
                    continue
                state = 'running' if row['state'] == 'running' else 'cancelled'
                self.db.execute('UPDATE jobs SET cancel=1,state=? WHERE id=?', (state, row['id']))
                self.event(row['id'], 'cancel_requested')
            self.db.execute("UPDATE actions SET state='revoked' WHERE job IN (SELECT id FROM jobs WHERE run=?) AND state IN ('awaiting_approval','approved')", (run,))

    def status(self, run):
        rows = self.db.execute('SELECT id,agent,state,attempts FROM jobs WHERE run=? ORDER BY position', (run,)).fetchall()
        if not rows:
            raise KeyError('SolicitaÃ§Ã£o inexistente')
        return [dict(row) for row in rows]

    def private_messages(self, run):
        return [dict(row) for row in self.db.execute('SELECT j.agent,c.input,c.output FROM jobs j JOIN private.content c ON c.id=j.id WHERE j.run=? ORDER BY j.position', (run,))]

    def propose_action(self, key, job, kind, payload, *, ttl_seconds=900):
        if kind not in ('send_email', 'write_file', 'commitment') or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError('AÃ§Ã£o ou validade invÃ¡lida')
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or not isinstance(payload, dict):
            raise ValueError('AÃ§Ã£o invÃ¡lida')
        raw = canonical(payload)
        if len(raw.encode()) > 12000:
            raise ValueError('AÃ§Ã£o muito grande')
        fingerprint = digest(canonical({'job': job, 'kind': kind, 'payload': payload}))
        action = stable('action:' + digest(key))
        with self.transaction():
            row = self.db.execute('SELECT state,cancel FROM jobs WHERE id=?', (job,)).fetchone()
            if not row or row['state'] != 'done' or row['cancel']:
                raise PermissionError('Somente anÃ¡lise concluÃ­da pode propor aÃ§Ã£o')
            old = self.db.execute('SELECT fingerprint FROM actions WHERE id=?', (action,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise ValueError('AÃ§Ã£o modificada exige outra identificaÃ§Ã£o e nova aprovaÃ§Ã£o')
                return action
            self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,NULL)',
                (action, job, kind, fingerprint, 'awaiting_approval', time.time() + ttl_seconds))
            self.db.execute('INSERT INTO private.action_content VALUES(?,?)', (action, raw))
            self.event(job, 'action_awaiting_approval')
        return action

    def review_action(self, action):
        row = self.db.execute('SELECT a.*,c.payload FROM actions a JOIN private.action_content c ON a.id=c.id WHERE a.id=?', (action,)).fetchone()
        if row is None:
            raise KeyError('AÃ§Ã£o inexistente')
        return dict(row)

    def decide(self, action, fingerprint, *, approve, reviewer):
        if type(approve) is not bool or not isinstance(reviewer, str) or not 1 <= len(reviewer) <= 128:
            raise ValueError('RevisÃ£o invÃ¡lida')
        with self.transaction():
            row = self.db.execute('SELECT * FROM actions WHERE id=?', (action,)).fetchone()
            if not row or row['state'] != 'awaiting_approval' or row['expires'] <= time.time() or row['fingerprint'] != fingerprint:
                raise PermissionError('AprovaÃ§Ã£o invÃ¡lida, expirada ou conteÃºdo divergente')
            import json
            payload = self.db.execute('SELECT payload FROM private.action_content WHERE id=?', (action,)).fetchone()
            if not payload or digest(canonical({'job': row['job'], 'kind': row['kind'],
                                                'payload': json.loads(payload[0])})) != fingerprint:
                raise PermissionError('ConteÃºdo privado diverge da aprovaÃ§Ã£o')
            self.db.execute('UPDATE actions SET state=?,reviewer=? WHERE id=?',
                ('approved' if approve else 'rejected', digest(reviewer), action))
            self.event(row['job'], 'action_approved' if approve else 'action_rejected')

    def revoke(self, action):
        with self.transaction():
            row = self.db.execute('SELECT job,state FROM actions WHERE id=?', (action,)).fetchone()
            if not row or row['state'] not in ('awaiting_approval', 'approved'):
                raise PermissionError('NÃ£o Ã© possÃ­vel revogar esta aÃ§Ã£o')
            self.db.execute("UPDATE actions SET state='revoked' WHERE id=?", (action,))
            self.event(row['job'], 'action_revoked')

    def execute_action(self, action):
        # Nenhum conector nesta fase, inclusive depois da aprovaÃ§Ã£o.
        raise PermissionError('Executor externo desativado; aprovaÃ§Ã£o nÃ£o executa a aÃ§Ã£o')
