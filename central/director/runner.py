"""Executor principal do Diretor Executivo Cloud."""
import argparse
import datetime as dt
import json
import logging
import os
import sys
from typing import List, Optional

from .client import JiraClient, JiraSecurityError
from .dashboard import DashboardGenerator
from .models import JiraIssue, TriageOrientation
from .triage import TriageEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("central.director.runner")


def run_cycle(
    base_url: str = "https://bi-enterprise.atlassian.net",
    user_email: Optional[str] = None,
    api_token: Optional[str] = None,
    jql: str = "project = KAN ORDER BY priority DESC, updated DESC",
    dry_run: bool = True,
    output_dashboard: Optional[str] = None,
    output_json: Optional[str] = None,
    simulated_issues_file: Optional[str] = None,
) -> int:
    """Executa um ciclo completo de triagem de atividades do Jira."""
    now_iso = dt.datetime.now(dt.timezone.utc).astimezone().isoformat()
    logger.info("Iniciando ciclo do Diretor Executivo Cloud. Dry-run: %s", dry_run)

    client = JiraClient(
        base_url=base_url,
        user_email=user_email,
        api_token=api_token,
        dry_run=dry_run,
    )
    triage = TriageEngine()

    issues: List[JiraIssue] = []
    auth_status = "Simulação / Sem credenciais"

    # 1. Obtenção de Atividades (Real ou Simulado)
    if simulated_issues_file and os.path.exists(simulated_issues_file):
        logger.info("Carregando atividades simuladas de: %s", simulated_issues_file)
        with open(simulated_issues_file, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            issues = [client._parse_issue(item) for item in raw_data.get("issues", [])]
        auth_status = "Mock Offline (Arquivo Local)"
    else:
        try:
            logger.info("Consultando Jira Cloud (%s) via JQL: %s", base_url, jql)
            for issue in client.search_issues_paginated(jql=jql):
                issues.append(issue)
            auth_status = "Conectado e Autenticado com Sucesso"
            logger.info("Total de atividades recuperadas do Jira: %d", len(issues))
        except JiraSecurityError as e:
            logger.warning("Não foi possível conectar ao Jira com credenciais reais: %s", e)
            auth_status = f"Falha de Autenticação / Pendente de Secrets ({e})"
            if not dry_run:
                # Falha estrita em execução real sem credenciais
                logger.error("Execução real interrompida por falta de credenciais válidas.")
                return 1

    # 2. Triagem e Processamento de Orientações
    orientations_posted: List[TriageOrientation] = []

    for issue in issues:
        orientation = triage.evaluate_issue(issue)
        if orientation:
            logger.info(
                "Nova orientação para %s -> Destinatário: %s (%s)",
                issue.key,
                orientation.recipient.value,
                orientation.allowed_mode.value,
            )
            comment_text = orientation.to_jira_comment()
            client.post_comment(issue.key, comment_text)
            orientations_posted.append(orientation)
        else:
            logger.debug("Atividade %s sem novas ações requeridas (deduplicada ou concluída).", issue.key)

    # 3. Geração do Painel do CEO (KAN-25)
    generator = DashboardGenerator(issues, orientations_posted)
    summary = generator.build_summary(timestamp=now_iso, auth_status=auth_status)
    dashboard_md = generator.render_markdown(summary)

    # 4. Gravação de Saídas
    if output_dashboard:
        os.makedirs(os.path.dirname(os.path.abspath(output_dashboard)), exist_ok=True)
        with open(output_dashboard, "w", encoding="utf-8") as f:
            f.write(dashboard_md)
        logger.info("Painel Markdown salvo em: %s", output_dashboard)

    if output_json:
        os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(summary.__dict__, f, indent=2, ensure_ascii=False)
        logger.info("Painel JSON salvo em: %s", output_json)

    # 5. Integração com GitHub Step Summary (se rodando no GitHub Actions)
    gh_step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if gh_step_summary:
        with open(gh_step_summary, "a", encoding="utf-8") as f:
            f.write("\n" + dashboard_md + "\n")

    logger.info(
        "Ciclo concluído com sucesso. Atividades triadas: %d | Novas orientações: %d",
        len(issues),
        len(orientations_posted),
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Diretor Executivo Cloud da Central-IA")
    parser.add_argument("--base-url", default="https://bi-enterprise.atlassian.net", help="URL base da instância Jira")
    parser.add_argument("--jql", default="project = KAN ORDER BY priority DESC, updated DESC", help="Consulta JQL")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("JIRA_DRY_RUN", "false").lower() == "true", help="Execução em modo de simulação (sem postar comentários reais)")
    parser.add_argument("--output-dashboard", default="docs/PAINEL-CEO.md", help="Caminho do arquivo Markdown para o painel do CEO")
    parser.add_argument("--output-json", default=".local-reports/painel-ceo.json", help="Caminho do arquivo JSON do painel")
    parser.add_argument("--simulated-issues", default=None, help="Caminho de arquivo JSON com mock de issues para testes")

    args = parser.parse_args()

    exit_code = run_cycle(
        base_url=args.base_url,
        jql=args.jql,
        dry_run=args.dry_run,
        output_dashboard=args.output_dashboard,
        output_json=args.output_json,
        simulated_issues_file=args.simulated_issues,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
