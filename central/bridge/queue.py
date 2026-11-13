"""Fila persistente e SQLite unificada para a Ponte Central, com isolamento privado e auditoria."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional
import uuid

from .auth import ApprovalAuthenticator
from .contract import BridgeTask, BridgeEvent, canonical_json, digest


class BridgeQueue:
    """Fila persistente unificada com isolamento de dados privados em banco anexo e auditoria sincronizada."""

    def __init__(
        self,
        db_path: str = 'central.sqlite3',
        *,
        authenticator: Optional[ApprovalAuthenticator] = None,
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.authenticator = authenticator or ApprovalAuthenticator()

        # Determinar caminho do banco privado isolado (padrão paired database da Fase 3)
        if self.path.name == 'operations.sqlite3':
            self.private_path = self.path.parent / 'private.sqlite3'
        else:
            self.private_path = self.path.parent / f'{self.path.stem}-private.sqlite3'

        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row

        # Configurar pragmas para integridade transacional
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')

        # Anexar banco privado para separação estrita de dados sensíveis
        self.db.execute('ATTACH DATABASE ? AS private', (str(self.private_path),))
        self.db.execute('PRAGMA private.journal_mode=DELETE')
        self.db.execute('PRAGMA private.synchronous=FULL')

        self._init_schema()

    def _init_schema(self) -> None:
        # 1. Esquema Operacional Público (sem dados pessoais em texto claro)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS bridge_tasks (
            task_id TEXT PRIMARY KEY,
            correlation_id TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            target_agent TEXT NOT NULL,
            state TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            result_fingerprint TEXT,
            errors TEXT,
            attempt_count INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3,
            timestamp REAL NOT NULL,
            human_approval_required INTEGER DEFAULT 0,
            metadata TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            owner TEXT,
            lease REAL,
            available REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bridge_events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            at REAL NOT NULL,
            code TEXT NOT NULL,
            details_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bridge_approvals (
            approval_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            payload_fingerprint TEXT NOT NULL,
            challenge TEXT NOT NULL,
            state TEXT NOT NULL,
            requested_at REAL NOT NULL,
            decided_at REAL,
            reviewer_id TEXT,
            reason TEXT
        );

        -- Compatibilidade e Sincronização com Auditorias da Fase 1 e Fase 3
        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL,
            job TEXT,
            code TEXT
        );

        CREATE TABLE IF NOT EXISTS audit (
            at TEXT,
            task TEXT,
            event TEXT,
            fingerprint TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_bridge_tasks_corr ON bridge_tasks(correlation_id);
        CREATE INDEX IF NOT EXISTS idx_bridge_tasks_state ON bridge_tasks(state, available);
        CREATE INDEX IF NOT EXISTS idx_bridge_events_corr ON bridge_events(correlation_id);
        ''')

        # 2. Esquema Privado Anexo (Conteúdos sensíveis isolados)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS private.bridge_content (
            task_id TEXT PRIMARY KEY,
            input TEXT,
            output TEXT
        );

        CREATE TABLE IF NOT EXISTS private.bridge_action_content (
            approval_id TEXT PRIMARY KEY,
            payload TEXT
        );
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def record_event(self, task_id: str, correlation_id: str, code: str, details: Optional[Dict[str, Any]] = None) -> None:
        event = BridgeEvent.create(task_id, correlation_id, code, details)
        det_hash = digest(canonical_json(event.details))

        # 1. Registrar na auditoria da Ponte
        self.db.execute(
            'INSERT INTO bridge_events VALUES(?,?,?,?,?,?)',
            (event.event_id, event.task_id, event.correlation_id, event.at, event.code, det_hash)
        )

        # 2. Sincronizar com tabela unificada da Fase 3
        self.db.execute(
            'INSERT INTO events(at, job, code) VALUES(?,?,?)',
            (event.at, event.task_id, event.code)
        )

        # 3. Sincronizar com tabela unificada da Fase 1
        from datetime import datetime, timezone
        iso_now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            'INSERT INTO audit VALUES(?,?,?,?)',
            (iso_now, event.task_id, event.code, det_hash)
        )

    def submit(self, task: BridgeTask) -> str:
        task.validate()
        fp = task.fingerprint()
        input_fp = digest(canonical_json(task.authorized_input))
        result_fp = digest(canonical_json(task.result)) if task.result else None

        with self.transaction():
            existing = self.db.execute(
                'SELECT task_id, fingerprint, state FROM bridge_tasks WHERE correlation_id=? AND target_agent=?',
                (task.correlation_id, task.target_agent)
            ).fetchone()

            if existing:
                if existing['fingerprint'] != fp:
                    raise ValueError(f'Idempotência violada: correlation_id {task.correlation_id} com payload divergente.')
                return existing['task_id']

            initial_state = 'awaiting_approval' if task.human_approval_required else 'queued'
            task.state = initial_state

            # Persistir no banco operacional apenas fingerprints e metadados
            self.db.execute(
                '''
                INSERT INTO bridge_tasks (
                    task_id, correlation_id, source_agent, target_agent, state,
                    input_fingerprint, result_fingerprint, errors, attempt_count, max_attempts,
                    timestamp, human_approval_required, metadata, fingerprint,
                    owner, lease, available
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,0)
                ''',
                (
                    task.task_id,
                    task.correlation_id,
                    task.source_agent,
                    task.target_agent,
                    task.state,
                    input_fp,
                    result_fp,
                    canonical_json(task.errors) if task.errors else None,
                    task.attempt_count,
                    task.max_attempts,
                    task.timestamp,
                    1 if task.human_approval_required else 0,
                    canonical_json(task.metadata),
                    fp,
                )
            )

            # Persistir conteúdo sensível exclusivamente no banco privado anexo
            self.db.execute(
                'INSERT INTO private.bridge_content VALUES(?,?,?)',
                (
                    task.task_id,
                    canonical_json(task.authorized_input),
                    canonical_json(task.result) if task.result else None,
                )
            )

            self.record_event(task.task_id, task.correlation_id, f'task_submitted_{initial_state}', {
                'source': task.source_agent,
                'target': task.target_agent,
                'approval_required': task.human_approval_required,
            })

            return task.task_id

    def recover(self) -> int:
        now = time.time()
        recovered = 0
        with self.transaction():
            expired = self.db.execute(
                "SELECT task_id, correlation_id, attempt_count, max_attempts FROM bridge_tasks WHERE state='running' AND lease<=?",
                (now,)
            ).fetchall()

            for row in expired:
                recovered += 1
                if row['attempt_count'] >= row['max_attempts']:
                    new_state = 'failed'
                else:
                    new_state = 'retryable'

                self.db.execute(
                    "UPDATE bridge_tasks SET state=?, owner=NULL, lease=NULL, available=? WHERE task_id=?",
                    (new_state, now + 1.0, row['task_id'])
                )
                self.record_event(row['task_id'], row['correlation_id'], 'lease_expired_recovered', {'new_state': new_state})

        return recovered

    def claim(self, owner: str, lease_seconds: float = 30.0) -> Optional[BridgeTask]:
        self.recover()
        now = time.time()

        with self.transaction():
            row = self.db.execute(
                '''
                SELECT * FROM bridge_tasks
                WHERE state IN ('queued', 'retryable')
                  AND available <= ?
                ORDER BY timestamp ASC
                LIMIT 1
                ''',
                (now,)
            ).fetchone()

            if not row:
                return None

            attempts = row['attempt_count'] + 1
            if attempts > row['max_attempts']:
                self.db.execute("UPDATE bridge_tasks SET state='failed' WHERE task_id=?", (row['task_id'],))
                self.record_event(row['task_id'], row['correlation_id'], 'max_attempts_exceeded')
                return None

            lease_until = now + lease_seconds
            self.db.execute(
                "UPDATE bridge_tasks SET state='running', owner=?, lease=?, attempt_count=? WHERE task_id=?",
                (owner, lease_until, attempts, row['task_id'])
            )

            self.record_event(row['task_id'], row['correlation_id'], 'task_claimed', {
                'owner': owner,
                'attempt': attempts,
            })

            # Carregar conteúdo do banco privado anexo
            private_content = self.db.execute(
                'SELECT input, output FROM private.bridge_content WHERE task_id=?',
                (row['task_id'],)
            ).fetchone()

            auth_input = json.loads(private_content['input']) if private_content and private_content['input'] else {}
            auth_output = json.loads(private_content['output']) if private_content and private_content['output'] else None

            task_dict = {
                'task_id': row['task_id'],
                'correlation_id': row['correlation_id'],
                'source_agent': row['source_agent'],
                'target_agent': row['target_agent'],
                'state': 'running',
                'authorized_input': auth_input,
                'result': auth_output,
                'errors': json.loads(row['errors']) if row['errors'] else None,
                'attempt_count': attempts,
                'max_attempts': row['max_attempts'],
                'timestamp': row['timestamp'],
                'human_approval_required': bool(row['human_approval_required']),
                'metadata': json.loads(row['metadata']),
            }
            return BridgeTask.from_dict(task_dict)

    def finish(self, task_id: str, owner: str, result: Dict[str, Any]) -> bool:
        result_fp = digest(canonical_json(result))
        with self.transaction():
            row = self.db.execute('SELECT owner, state, correlation_id FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row or row['state'] != 'running' or row['owner'] != owner:
                return False

            # Atualizar fingerprint no operacional
            self.db.execute(
                "UPDATE bridge_tasks SET state='done', result_fingerprint=?, owner=NULL, lease=NULL WHERE task_id=?",
                (result_fp, task_id)
            )

            # Gravar resultado no banco privado
            self.db.execute(
                'UPDATE private.bridge_content SET output=? WHERE task_id=?',
                (canonical_json(result), task_id)
            )

            self.record_event(task_id, row['correlation_id'], 'task_completed', {'result_fp': result_fp})
            return True

    def fail(self, task_id: str, owner: str, error_message: str, is_transient: bool = False) -> bool:
        now = time.time()
        with self.transaction():
            row = self.db.execute('SELECT owner, state, correlation_id, attempt_count, max_attempts, errors FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row or row['state'] != 'running' or row['owner'] != owner:
                return False

            existing_errors = json.loads(row['errors']) if row['errors'] else []
            existing_errors.append(error_message)

            if is_transient and row['attempt_count'] < row['max_attempts']:
                new_state = 'retryable'
                backoff = min(15.0, 1.5 ** row['attempt_count'])
                available = now + backoff
            else:
                new_state = 'failed'
                available = 0

            self.db.execute(
                "UPDATE bridge_tasks SET state=?, errors=?, owner=NULL, lease=NULL, available=? WHERE task_id=?",
                (new_state, canonical_json(existing_errors), available, task_id)
            )

            self.record_event(task_id, row['correlation_id'], f'task_{new_state}', {
                'error': error_message,
                'attempt': row['attempt_count'],
            })
            return True

    def request_approval(self, task_id: str, action_type: str, payload: Dict[str, Any]) -> Dict[str, str]:
        """Cria solicitação de aprovação humana com desafio criptográfico vinculado ao payload."""
        approval_id = uuid.uuid4().hex
        now = time.time()
        payload_fp = digest(canonical_json(payload))
        challenge = self.authenticator.create_challenge(approval_id, task_id, action_type, payload_fp)

        with self.transaction():
            row = self.db.execute('SELECT correlation_id FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
            if not row:
                raise KeyError(f'Tarefa inexistente: {task_id}')

            # 1. Registrar metadados e desafio no banco operacional
            self.db.execute(
                '''
                INSERT INTO bridge_approvals (
                    approval_id, task_id, correlation_id, action_type, payload_fingerprint,
                    challenge, state, requested_at, decided_at, reviewer_id, reason
                ) VALUES (?,?,?,?,?,?,'pending',?,NULL,NULL,NULL)
                ''',
                (approval_id, task_id, row['correlation_id'], action_type, payload_fp, challenge, now)
            )

            # 2. Registrar payload completo sensível no banco privado anexo
            self.db.execute(
                'INSERT INTO private.bridge_action_content VALUES(?,?)',
                (approval_id, canonical_json(payload))
            )

            self.db.execute("UPDATE bridge_tasks SET state='awaiting_approval' WHERE task_id=?", (task_id,))
            self.record_event(task_id, row['correlation_id'], 'approval_requested', {
                'approval_id': approval_id,
                'action_type': action_type,
            })

        return {'approval_id': approval_id, 'challenge': challenge}

    def approve(self, task_id: str, reviewer_id: str, token: str) -> bool:
        """Aprova a execução mediante validação criptográfica estrita do token."""
        now = time.time()
        with self.transaction():
            appr = self.db.execute(
                "SELECT approval_id, challenge FROM bridge_approvals WHERE task_id=? AND state='pending'",
                (task_id,)
            ).fetchone()

            if not appr:
                row = self.db.execute('SELECT state FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
                if not row or row['state'] != 'awaiting_approval':
                    return False
                # Criar aprovação implícita para a tarefa
                approval_id = uuid.uuid4().hex
                challenge = self.authenticator.create_challenge(approval_id, task_id, 'task_dispatch', 'default')
            else:
                approval_id = appr['approval_id']
                challenge = appr['challenge']

            # Validação criptográfica obrigatória: rejeitar simples strings
            if not self.authenticator.verify_token(reviewer_id, challenge, token):
                raise PermissionError('Aprovação rejeitada: autenticação verificável ausente ou token inválido')

            row_task = self.db.execute('SELECT correlation_id FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
            self.db.execute(
                "UPDATE bridge_approvals SET state='approved', decided_at=?, reviewer_id=? WHERE task_id=? AND state='pending'",
                (now, reviewer_id, task_id)
            )
            self.db.execute("UPDATE bridge_tasks SET state='queued', human_approval_required=0, available=0 WHERE task_id=?", (task_id,))
            self.record_event(task_id, row_task['correlation_id'], 'approval_granted', {'reviewer_id': reviewer_id})
            return True

    def reject(self, task_id: str, reviewer_id: str, token: str, reason: str = '') -> bool:
        """Rejeita a execução mediante validação criptográfica estrita do token."""
        now = time.time()
        with self.transaction():
            appr = self.db.execute(
                "SELECT approval_id, challenge FROM bridge_approvals WHERE task_id=? AND state='pending'",
                (task_id,)
            ).fetchone()

            challenge = appr['challenge'] if appr else 'default'
            if not self.authenticator.verify_token(reviewer_id, challenge, token):
                raise PermissionError('Rejeição recusada: autenticação verificável ausente ou token inválido')

            row_task = self.db.execute('SELECT correlation_id FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
            self.db.execute(
                "UPDATE bridge_approvals SET state='rejected', decided_at=?, reviewer_id=?, reason=? WHERE task_id=? AND state='pending'",
                (now, reviewer_id, reason, task_id)
            )
            self.db.execute("UPDATE bridge_tasks SET state='rejected' WHERE task_id=?", (task_id,))
            self.record_event(task_id, row_task['correlation_id'], 'approval_rejected', {'reviewer_id': reviewer_id, 'reason': reason})
            return True

    def get_task(self, task_id: str) -> Optional[BridgeTask]:
        row = self.db.execute('SELECT * FROM bridge_tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row:
            return None

        # Conteúdo reconstruído a partir do banco privado anexo
        private_content = self.db.execute(
            'SELECT input, output FROM private.bridge_content WHERE task_id=?',
            (task_id,)
        ).fetchone()

        auth_input = json.loads(private_content['input']) if private_content and private_content['input'] else {}
        auth_output = json.loads(private_content['output']) if private_content and private_content['output'] else None

        return BridgeTask(
            task_id=row['task_id'],
            correlation_id=row['correlation_id'],
            source_agent=row['source_agent'],
            target_agent=row['target_agent'],
            state=row['state'],
            authorized_input=auth_input,
            result=auth_output,
            errors=json.loads(row['errors']) if row['errors'] else None,
            attempt_count=row['attempt_count'],
            max_attempts=row['max_attempts'],
            timestamp=row['timestamp'],
            human_approval_required=bool(row['human_approval_required']),
            metadata=json.loads(row['metadata']),
        )

    def get_tasks_by_correlation(self, correlation_id: str) -> List[BridgeTask]:
        rows = self.db.execute('SELECT task_id FROM bridge_tasks WHERE correlation_id=? ORDER BY timestamp ASC', (correlation_id,)).fetchall()
        tasks = []
        for r in rows:
            t = self.get_task(r['task_id'])
            if t:
                tasks.append(t)
        return tasks

    def get_events(self, correlation_id: str) -> List[BridgeEvent]:
        rows = self.db.execute('SELECT * FROM bridge_events WHERE correlation_id=? ORDER BY at ASC', (correlation_id,)).fetchall()
        return [
            BridgeEvent(
                event_id=r['event_id'],
                task_id=r['task_id'],
                correlation_id=r['correlation_id'],
                at=r['at'],
                code=r['code'],
                details={'details_hash': r['details_hash']},
            )
            for r in rows
        ]

    def close(self) -> None:
        self.db.close()
