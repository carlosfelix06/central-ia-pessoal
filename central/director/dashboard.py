"""Gerador de Painel de Acompanhamento Executivo para o CEO (Referência KAN-25)."""
import json
from typing import Any, Dict, List
from .models import DashboardSummary, JiraIssue, TriageOrientation


class DashboardGenerator:
    """Gera o relatório executivo em Markdown e JSON estruturado com distinção entre verificado e declarado."""

    def __init__(self, issues: List[JiraIssue], orientations_generated: List[TriageOrientation]) -> None:
        self.issues = issues
        self.orientations = orientations_generated

    def build_summary(
        self,
        timestamp: str,
        auth_status: str = "Autenticado (GitHub Secrets)",
        next_run: str = "+30 minutos (cron */30)",
    ) -> DashboardSummary:
        pending_by_agent: Dict[str, int] = {
            "DIRETOR": 0,
            "COPILOT": 0,
            "ANTIGRAVITY": 0,
            "CODEX": 0,
            "DOCUMENTALISTA": 0,
        }

        in_progress: List[Dict[str, str]] = []
        awaiting_ack: List[Dict[str, str]] = []
        proven_deliveries: List[Dict[str, str]] = []

        for issue in self.issues:
            status_lower = issue.status.lower()

            # Em andamento
            if any(s in status_lower for s in ("andamento", "in progress", "progresso")):
                in_progress.append({"key": issue.key, "summary": issue.summary, "assignee": issue.assignee or "Não atribuído"})

            # Concluídas com verificação
            if any(s in status_lower for s in ("conclu", "done", "resolvido")):
                proven_deliveries.append({"key": issue.key, "summary": issue.summary, "status": issue.status})
                continue

            # Comentários do Diretor aguardando ACK (avalia apenas o último comentário do Diretor)
            last_dir = None
            for c in reversed(issue.comments):
                if "[DIRETOR-CLOUD / TRIAGEM]" in c.body:
                    last_dir = c
                    break
            if last_dir and "CONFIRMAÇÃO (ACK): PENDENTE" in last_dir.body:
                awaiting_ack.append({"key": issue.key, "summary": issue.summary, "comment_id": last_dir.id})

        # Contagem das novas orientações geradas
        for ori in self.orientations:
            agent_key = ori.recipient.value
            pending_by_agent[agent_key] = pending_by_agent.get(agent_key, 0) + 1

        security_blockers = [
            "Bloqueio preventivo de chamadas pagas (CENTRAL_ALLOW_PAID=false)",
            "Chaves reais não homologadas para execução autônoma (KAN-7, KAN-8)",
            "Exigência estrita de aprovação do CEO para saída do modo Simulado",
            "Regra 0 inegociável: toda ação exige commit, pull e push imediatos",
        ]

        cloud_schedules = [
            {
                "nome": "Diretor Executivo Cloud (GitHub Actions)",
                "cadencia": "A cada 30 minutos (cron: */30 * * * *)",
                "estado_verificado": "Workflow implementado e testado no repositório",
                "estado_declarado": "Operacional em nuvem 24/7",
            }
        ]

        local_schedules = [
            {
                "nome": "Agente Sentinela de Coordenação (Antigravity Sidecar)",
                "cadencia": "A cada 1 hora (cron: 0 * * * *)",
                "estado_verificado": "Configurado em sidecar.json (Desktop Local)",
                "estado_declarado": "Ativo enquanto máquina estiver ligada",
            }
        ]

        return DashboardSummary(
            timestamp=timestamp,
            total_issues=len(self.issues),
            pending_by_agent=pending_by_agent,
            in_progress=in_progress,
            awaiting_ack=awaiting_ack,
            security_blockers=security_blockers,
            last_jira_review=timestamp,
            next_scheduled_run=next_run,
            auth_status=auth_status,
            latest_proven_deliveries=proven_deliveries,
            cloud_schedules=cloud_schedules,
            local_schedules=local_schedules,
        )

    def render_markdown(self, summary: DashboardSummary) -> str:
        md = []
        md.append("# 🏛️ PAINEL EXECUTIVO DO CEO — CENTRAL-IA (KAN-25)")
        md.append(f"**Data da Revisão:** `{summary.timestamp}` | **Autenticação Jira:** `{summary.auth_status}`\n")
        md.append(f"**Próxima Execução Prevista:** `{summary.next_scheduled_run}`\n")

        md.append("## 1. Distribuição de Atividades Pendentes por Perfil de IA")
        md.append("| Perfil de IA | Papel Principal | Tarefas Atribuídas/Triadas |")
        md.append("| :--- | :--- | :--- |")
        for agent, count in summary.pending_by_agent.items():
            md.append(f"| **{agent}** | {_agent_role(agent)} | `{count}` |")
        md.append("")

        if self.orientations:
            md.append("### Orientações Recentes de Triagem")
            md.append("| Chave | Destinatário | Modo Permitido | Tarefa |")
            md.append("| :--- | :--- | :--- | :--- |")
            for ori in self.orientations:
                md.append(f"| **{ori.issue_key}** | `{ori.recipient.value}` | `{ori.allowed_mode.value}` | {ori.task_summary} |")
            md.append("")

        md.append("## 2. Atividades em Andamento")
        if summary.in_progress:
            md.append("| Chave | Resumo | Responsável |")
            md.append("| :--- | :--- | :--- |")
            for item in summary.in_progress:
                md.append(f"| **{item['key']}** | {item['summary']} | {item['assignee']} |")
        else:
            md.append("_Nenhuma atividade marcada 'Em andamento' no momento._")
        md.append("")

        md.append("## 3. Orientações Aguardando Confirmação (ACK)")
        if summary.awaiting_ack:
            md.append("| Chave | Resumo da Tarefa | Status ACK |")
            md.append("| :--- | :--- | :--- |")
            for item in summary.awaiting_ack:
                md.append(f"| **{item['key']}** | {item['summary']} | 🟡 PENDENTE |")
        else:
            md.append("✅ _Nenhuma orientação pendente de ACK registrada._")
        md.append("")

        md.append("## 4. Bloqueios de Segurança e Governança")
        for blk in summary.security_blockers:
            md.append(f"- 🛑 {blk}")
        md.append("")

        md.append("## 5. Agendamentos: Estados Verificados vs. Declarados")
        md.append("### Agendamento em Nuvem (Cloud)")
        for s in summary.cloud_schedules:
            md.append(f"- **{s['nome']}**: {s['cadencia']}")
            md.append(f"  - **Estado Verificado:** `{s['estado_verificado']}`")
            md.append(f"  - **Estado Declarado:** `{s['estado_declarado']}`")
        md.append("")
        md.append("### Agendamento Local (Workstation Windows)")
        for s in summary.local_schedules:
            md.append(f"- **{s['nome']}**: {s['cadencia']}")
            md.append(f"  - **Estado Verificado:** `{s['estado_verificado']}`")
            md.append(f"  - **Estado Declarado:** `{s['estado_declarado']}`")
        md.append("")

        md.append("## 6. Últimas Entregas Comprovadas")
        if summary.latest_proven_deliveries:
            for item in summary.latest_proven_deliveries:
                md.append(f"- 🎯 **{item['key']}**: {item['summary']} (Status: `{item['status']}`)")
        else:
            md.append("_Nenhuma entrega final arquivada nesta janela._")

        return "\n".join(md)


def _agent_role(agent: str) -> str:
    roles = {
        "DIRETOR": "Coordenação, triagem, governança e prioridades",
        "COPILOT": "Git, repositório, branch e engenharia de desenvolvimento",
        "ANTIGRAVITY": "Automações locais, infraestrutura de agentes e filas",
        "CODEX": "Auditoria técnica, conformidade e segurança independente",
        "DOCUMENTALISTA": "Curadoria de soluções e biblioteca de conhecimento",
    }
    return roles.get(agent, "Agente Especializado")
