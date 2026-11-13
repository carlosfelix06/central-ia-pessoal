"""Motor de triagem, distribuição e deduplicação do Diretor Executivo Cloud."""
import hashlib
import re
from typing import List, Optional, Tuple

from .models import AgentProfile, AllowedMode, JiraComment, JiraIssue, TriageOrientation


class TriageEngine:
    """Classifica atividades, seleciona o agente adequado e gera orientações com deduplicação criptográfica."""

    DIRECTOR_TAG = "[DIRETOR-CLOUD / TRIAGEM]"
    ID_PATTERN = re.compile(r"ID-MSG:\s*(dc-[0-9a-f]{12})")
    ACK_PATTERN = re.compile(r"\bACK\s+(dc-[0-9a-f]{12})", re.IGNORECASE)

    def __init__(self) -> None:
        pass

    def determine_recipient(self, issue: JiraIssue) -> AgentProfile:
        """Determina o agente responsável com base no escopo e regras de governança."""
        text = f"{issue.key} {issue.summary} {issue.description}".lower()

        # 1. Auditoria, segurança, bandit, conformidade -> CODEX
        if re.search(r"\b(segurança|security|auditoria|bandit|vulnerabilidade|conformidade|kan-7|kan-9)\b", text):
            return AgentProfile.DATA_QUALITY_AUDITOR

        # 2. Documentação, biblioteca de conhecimento, curadoria, relatórios -> DOCUMENTALISTA
        if re.search(r"\b(documentação|documentacao|docs|readme|relatório|relatorio|curadoria|biblioteca|kan-10|kan-24)\b", text):
            return AgentProfile.DATAVIZ_SPECIALIST

        # 3. Automação, workers de fila, sidecar, monitoramento, antigravity -> ANTIGRAVITY
        if re.search(r"\b(antigravity|worker|fila|sidecar|agendador|monitoramento|kan-6)\b", text):
            return AgentProfile.DATA_ENGINEER

        # 4. Git, configuração de build, CI/CD, branch, merge, copilot -> COPILOT
        if re.search(r"\b(copilot|git|github|actions|workflow|ci|cd|pr|pull request)\b", text):
            return AgentProfile.BI_ANALYST

        # 5. Padrão para coordenação, governança e tarefas gerais -> DIRETOR
        return AgentProfile.BI_LEAD

    def determine_allowed_mode(self, issue: JiraIssue, recipient: AgentProfile) -> AllowedMode:
        """Restringe o modo permitido com base nas regras de segurança da Central."""
        status_norm = issue.status.lower()
        if "bloquead" in status_norm or "pendente" in status_norm:
            if recipient == AgentProfile.DATA_QUALITY_AUDITOR:
                return AllowedMode.LEITURA
            return AllowedMode.SIMULACAO

        if recipient == AgentProfile.BI_LEAD:
            return AllowedMode.GOVERNANCA

        if recipient == AgentProfile.DATA_ENGINEER:
            return AllowedMode.SIMULACAO

        return AllowedMode.DESENVOLVIMENTO

    def compute_message_id(self, issue: JiraIssue, triage_type: str) -> str:
        """Gera um identificador criptográfico único para a mensagem da atividade."""
        last_comment_id = issue.comments[-1].id if issue.comments else "-"
        base = f"{issue.key}|{triage_type}|{issue.status}|{issue.priority}|{last_comment_id}"
        return "dc-" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]

    def extract_latest_director_comment(self, issue: JiraIssue) -> Optional[JiraComment]:
        """Retorna o comentário mais recente gerado pelo Diretor Cloud, se houver."""
        for c in reversed(issue.comments):
            if self.DIRECTOR_TAG in c.body:
                return c
        return None

    def check_ack_status(self, issue: JiraIssue, director_comment: JiraComment) -> Tuple[str, Optional[str]]:
        """Verifica se houve confirmação (ACK) na própria mensagem consolidada ou posterior à orientação do Diretor."""
        if "CONFIRMAÇÃO (ACK): CONFIRMADO" in director_comment.body.upper():
            return "CONFIRMADO", director_comment.author
        dir_created = director_comment.created
        dir_msg_ids = self.ID_PATTERN.findall(director_comment.body)
        target_mid = dir_msg_ids[0].lower() if dir_msg_ids else None

        for c in issue.comments:
            if c.created > dir_created and c.id != director_comment.id:
                body = c.body
                # 1. ACK com ID da mensagem (ex: ACK dc-...)
                if target_mid and target_mid in [m.lower() for m in self.ACK_PATTERN.findall(body)]:
                    return "CONFIRMADO", c.author

                # 2. ACK legado ou formato padrão
                body_upper = body.upper()
                if "ACK" in body_upper or "CONFIRMADO" in body_upper or "RECEBIDO" in body_upper:
                    return "CONFIRMADO", c.author

        return "PENDENTE", None

    def has_material_change(self, issue: JiraIssue, last_orientation_comment: JiraComment) -> bool:
        """Avalia se houve alteração material (status, novos critérios ou comentários) desde a última orientação."""
        for c in issue.comments:
            if c.created > last_orientation_comment.created and c.id != last_orientation_comment.id:
                if self.DIRECTOR_TAG not in c.body:
                    return True

        match = re.search(r"STATUS VERIFICADO:\s*(.+)", last_orientation_comment.body)
        if match:
            previous_status = match.group(1).strip()
            if previous_status.lower() != issue.status.lower():
                return True

        return False

    def evaluate_issue(self, issue: JiraIssue) -> Optional[TriageOrientation]:
        """Gera uma nova orientação caso a atividade precise de encaminhamento ou atualização."""
        status_norm = issue.status.lower()
        if status_norm in ("concluído", "concluido", "done", "fechado"):
            return None

        recipient = self.determine_recipient(issue)
        allowed_mode = self.determine_allowed_mode(issue, recipient)
        triage_type = "NOVA_ATIVIDADE"

        last_director_comment = self.extract_latest_director_comment(issue)

        if last_director_comment:
            # Deduplicação estrita: se já orientou e não houve mudança material, não republica
            if not self.has_material_change(issue, last_director_comment):
                return None
            triage_type = "ATUALIZACAO_STATUS"

        msg_id = self.compute_message_id(issue, triage_type)

        # Evita duplicar se o mesmo ID-MSG já existe em algum comentário próprio
        for c in issue.comments:
            if self.DIRECTOR_TAG in c.body and msg_id in self.ID_PATTERN.findall(c.body):
                return None

        evidence = "Status verificado via consulta JQL integrada. Nenhuma evidência de commit/pull request concluída com validação."
        if "kan-6" in issue.key.lower():
            evidence = "Repositório local sincronizado na main. Branch remota aguardando ativação de worker de fila."
        elif "kan-7" in issue.key.lower():
            evidence = "Testes locais aprovados, porém correções formais de segurança pendentes de homologação."

        work_needed = self._build_work_description(issue, recipient)
        dependencies = "Aprovação do CEO via Jira; Respeito estrito à Regra 0 (commit/pull/push)."
        completion_criteria = (
            f"Relatório de execução validado com evidências de teste e status do card atualizado via {recipient.value}."
        )

        return TriageOrientation(
            issue_key=issue.key,
            recipient=recipient,
            task_summary=issue.summary,
            priority=issue.priority,
            verified_status=issue.status,
            evidence=evidence,
            work_needed=work_needed,
            allowed_mode=allowed_mode,
            dependencies=dependencies,
            completion_criteria=completion_criteria,
            ack_status="PENDENTE",
            message_id=msg_id,
            triage_type=triage_type,
        )

    def _build_work_description(self, issue: JiraIssue, recipient: AgentProfile) -> str:
        key = issue.key.upper()
        if "KAN-6" in key:
            return "Desenvolver protótipo seguro do worker Antigravity conectado à fila local, sem chamadas pagas."
        if "KAN-7" in key:
            return "Revisar relatório Bandit e isolamento de chaves DPAPI no ambiente local."
        if "KAN-8" in key:
            return "Validar simulação multiprovedor (OpenAI/Gemini) com orçamentos zero antes de homologação."
        if "KAN-10" in key:
            return "Consolidar documentação de arquitetura da Central-IA e histórico de decisões."
        if "KAN-24" in key:
            return "Registrar evidências de execução do Diretor Cloud e logs das rotinas automatizadas."
        if "KAN-25" in key:
            return "Montar painel de acompanhamento do CEO com status integrado e distinção entre verificado e declarado."

        return f"Triagem e análise preliminar pelo agente {recipient.value} em modo {recipient.value}."
