"""Contrato padronizado de tarefas, eventos e mensagens da Ponte Central."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional
import uuid

VALID_STATES = (
    'created',
    'queued',
    'running',
    'done',
    'retryable',
    'failed',
    'awaiting_approval',
    'approved',
    'rejected',
    'uncertain',
)


def canonical_json(value: Any) -> str:
    """Serialização JSON determinística e canônica para integridade e hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(text: str) -> str:
    """Hash SHA-256 de texto UTF-8."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass
class BridgeTask:
    """Contrato padronizado de tarefa executada por agentes da Central."""
    task_id: str
    correlation_id: str
    source_agent: str
    target_agent: str
    state: str = 'created'
    authorized_input: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None
    attempt_count: int = 0
    max_attempts: int = 3
    timestamp: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    human_approval_required: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Valida integridade e restrições de tipos e limites do contrato."""
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError('task_id inválido ou vazio')
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError('correlation_id inválido ou vazio')
        if not self.source_agent or not isinstance(self.source_agent, str):
            raise ValueError('source_agent obrigatório')
        if not self.target_agent or not isinstance(self.target_agent, str):
            raise ValueError('target_agent obrigatório')
        if self.state not in VALID_STATES:
            raise ValueError(f'Estado inválido: {self.state}')
        if not isinstance(self.authorized_input, dict):
            raise ValueError('authorized_input deve ser um dicionário')
        if self.attempt_count < 0:
            raise ValueError('attempt_count não pode ser negativo')
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ValueError('max_attempts deve estar entre 1 e 10')

    def to_dict(self) -> Dict[str, Any]:
        """Converte a tarefa em dicionário serializável."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BridgeTask':
        """Reconstrói uma instância de BridgeTask a partir de um dicionário."""
        return cls(**data)

    def fingerprint(self) -> str:
        """Gera assinatura SHA-256 única para garantia de idempotência."""
        payload = {
            'correlation_id': self.correlation_id,
            'source_agent': self.source_agent,
            'target_agent': self.target_agent,
            'authorized_input': self.authorized_input,
            'human_approval_required': self.human_approval_required,
        }
        return digest(canonical_json(payload))


@dataclass
class BridgeEvent:
    """Registro imutável de evento no ciclo de vida da tarefa na Ponte."""
    event_id: str
    task_id: str
    correlation_id: str
    at: float
    code: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(cls, task_id: str, correlation_id: str, code: str, details: Optional[Dict[str, Any]] = None) -> 'BridgeEvent':
        return cls(
            event_id=uuid.uuid4().hex,
            task_id=task_id,
            correlation_id=correlation_id,
            at=datetime.now(timezone.utc).timestamp(),
            code=code,
            details=details or {},
        )
