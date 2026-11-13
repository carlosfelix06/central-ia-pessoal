"""Modelos de dados e tipos para o Diretor Executivo Cloud."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentProfile(str, Enum):
    BI_LEAD = "BI_LEAD"
    BI_ANALYST = "BI_ANALYST"
    DATA_ENGINEER = "DATA_ENGINEER"
    DATA_QUALITY_AUDITOR = "DATA_QUALITY_AUDITOR"
    DATAVIZ_SPECIALIST = "DATAVIZ_SPECIALIST"


class AllowedMode(str, Enum):
    LEITURA = "SOMENTE LEITURA"
    SIMULACAO = "SIMULACAO / TESTES OFFLINE"
    DESENVOLVIMENTO = "DESENVOLVIMENTO ISOLADO"
    HOMOLOGACAO = "HOMOLOGACAO SUPERVISIONADA"
    GOVERNANCA = "GOVERNANCA E DECISAO"


@dataclass
class JiraComment:
    id: str
    author: str
    body: str
    created: str
    updated: Optional[str] = None


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    priority: str
    assignee: Optional[str] = None
    description: str = ""
    created: str = ""
    updated: str = ""
    comments: List[JiraComment] = field(default_factory=list)
    raw_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TriageOrientation:
    issue_key: str
    recipient: AgentProfile
    task_summary: str
    priority: str
    verified_status: str
    evidence: str
    work_needed: str
    allowed_mode: AllowedMode
    dependencies: str
    completion_criteria: str
    ack_status: str = "PENDENTE"
    message_id: str = ""
    triage_type: str = "NOVA_ATIVIDADE"

    def to_jira_comment(self) -> str:
        """Formata o comentário no padrão oficial e estrito do Diretor Cloud com rastreabilidade."""
        lines = [
            "[DIRETOR-CLOUD / TRIAGEM]",
            "",
            f"DESTINATÁRIO: {self.recipient.value}",
            f"TAREFA: {self.task_summary}",
            f"PRIORIDADE: {self.priority}",
            f"STATUS VERIFICADO: {self.verified_status}",
            f"EVIDÊNCIA: {self.evidence}",
            f"TRABALHO NECESSÁRIO: {self.work_needed}",
            f"MODO PERMITIDO: {self.allowed_mode.value}",
            f"DEPENDÊNCIAS: {self.dependencies}",
            f"CRITÉRIO DE CONCLUSÃO: {self.completion_criteria}",
            f"CONFIRMAÇÃO (ACK): {self.ack_status}",
        ]
        if self.message_id:
            lines.extend([
                f"TIPO: {self.triage_type}",
                f"ID-MSG: {self.message_id}",
                f"Para confirmar, comente: ACK {self.message_id} — {self.recipient.value}",
            ])
        return "\n".join(lines)


@dataclass
class DashboardSummary:
    timestamp: str
    total_issues: int
    pending_by_agent: Dict[str, int]
    in_progress: List[Dict[str, str]]
    awaiting_ack: List[Dict[str, str]]
    security_blockers: List[str]
    last_jira_review: str
    next_scheduled_run: str
    auth_status: str
    latest_proven_deliveries: List[Dict[str, str]]
    cloud_schedules: List[Dict[str, str]]
    local_schedules: List[Dict[str, str]]
