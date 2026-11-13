"""Interface local para agentes consultarem e confirmarem orientações do Jira."""
import argparse
import json
import logging
import os
import sys
from typing import List, Optional

from .client import JiraClient, JiraSecurityError
from .models import AgentProfile, JiraIssue
from .triage import TriageEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("central.director.local_agent")


class LocalAgentProtocol:
    """Protocolo de comunicação local entre os agentes e as orientações do Jira."""

    def __init__(self, client: JiraClient) -> None:
        self.client = client
        self.triage = TriageEngine()

    def get_pending_orientations_for_agent(
        self,
        agent: AgentProfile,
        jql: str = "project = KAN ORDER BY priority DESC, updated DESC",
    ) -> List[dict]:
        """Busca todas as atividades com orientações pendentes de ACK para o perfil especificado."""
        pending: List[dict] = []
        for issue in self.client.search_issues_paginated(jql=jql):
            last_director = self.triage.extract_latest_director_comment(issue)
            if not last_director:
                continue

            # Verifica se é para este perfil
            if f"DESTINATÁRIO: {agent.value}" not in last_director.body:
                continue

            ack_status, ack_author = self.triage.check_ack_status(issue, last_director)
            if ack_status == "PENDENTE":
                pending.append({
                    "key": issue.key,
                    "summary": issue.summary,
                    "status": issue.status,
                    "priority": issue.priority,
                    "orientation": last_director.body,
                    "comment_id": last_director.id,
                })

        return pending

    def post_agent_ack(self, issue_key: str, agent: AgentProfile, note: str = "") -> dict:
        """Registra a confirmação de recebimento (ACK) do agente destinatário no Jira."""
        comment_text = (
            f"[{agent.value} / ACK]\n\n"
            f"CONFIRMAÇÃO (ACK): CONFIRMADO\n"
            f"AGENTE EXECUTOR: {agent.value}\n"
            f"DETALHES: {note or 'Orientação recebida pelo agente local. Trabalho inicial em modo seguro.'}"
        )
        return self.client.post_comment(issue_key, comment_text)

    def post_agent_update(
        self,
        issue_key: str,
        agent: AgentProfile,
        update_type: str,
        message: str,
    ) -> dict:
        """Registra atualizações (ENTREGA, BLOQUEIO, REVISAO, DESCOBERTA)."""
        valid_types = ("ENTREGA", "BLOQUEIO", "REVISAO", "DESCOBERTA")
        u_type = update_type.upper()
        if u_type not in valid_types:
            raise ValueError(f"Tipo de atualização inválido: {update_type}. Válidos: {valid_types}")

        comment_text = (
            f"[{agent.value} / {u_type}]\n\n"
            f"TIPO: {u_type}\n"
            f"AGENTE: {agent.value}\n"
            f"MENSAGEM: {message}"
        )
        return self.client.post_comment(issue_key, comment_text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Interface do Agente Local com o Diretor Cloud")
    parser.add_argument("--agent", required=True, choices=[p.value for p in AgentProfile], help="Perfil do agente local")
    parser.add_argument("--action", choices=["check", "ack", "update"], default="check", help="Ação a executar")
    parser.add_argument("--issue", help="Chave da atividade (ex: KAN-6) para ACK ou Update")
    parser.add_argument("--note", default="", help="Nota adicional para ACK ou mensagem do Update")
    parser.add_argument("--update-type", choices=["ENTREGA", "BLOQUEIO", "REVISAO", "DESCOBERTA"], default="ENTREGA")
    parser.add_argument("--dry-run", action="store_true", default=False)

    args = parser.parse_args()
    client = JiraClient(dry_run=args.dry_run)
    protocol = LocalAgentProtocol(client)
    agent_profile = AgentProfile(args.agent)

    if args.action == "check":
        logger.info("Verificando orientações pendentes para %s...", agent_profile.value)
        try:
            pending = protocol.get_pending_orientations_for_agent(agent_profile)
            logger.info("Total de orientações pendentes para %s: %d", agent_profile.value, len(pending))
            for item in pending:
                print(f"\n--- [{item['key']}] {item['summary']} ---")
                print(item["orientation"])
        except JiraSecurityError as e:
            logger.error("Falha ao consultar Jira: %s", e)
            sys.exit(1)

    elif args.action == "ack":
        if not args.issue:
            logger.error("--issue é obrigatório para confirmar ACK.")
            sys.exit(1)
        res = protocol.post_agent_ack(args.issue, agent_profile, args.note)
        logger.info("ACK registrado com sucesso: %s", res)

    elif args.action == "update":
        if not args.issue or not args.note:
            logger.error("--issue e --note são obrigatórios para registrar atualização.")
            sys.exit(1)
        res = protocol.post_agent_update(args.issue, agent_profile, args.update_type, args.note)
        logger.info("Atualização registrada com sucesso: %s", res)


if __name__ == "__main__":
    main()
