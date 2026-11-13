"""Testes unitários e de integração offline para o Diretor Executivo Cloud."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from central.director.client import JiraClient, JiraSecurityError
from central.director.dashboard import DashboardGenerator
from central.director.listener import check_incoming_instructions, update_heartbeat
from central.director.local_agent import LocalAgentProtocol
from central.director.models import AgentProfile, AllowedMode, JiraComment, JiraIssue, TriageOrientation
from central.director.runner import run_cycle
from central.director.triage import TriageEngine


class TestDirectorCloud(unittest.TestCase):
    def setUp(self):
        self.triage = TriageEngine()

    def test_comment_format_strictness(self):
        """Valida que o comentário de orientação segue rigorosamente o formato exigido."""
        orientation = TriageOrientation(
            issue_key="KAN-6",
            recipient=AgentProfile.DATA_ENGINEER,
            task_summary="Ponte Real GitHub <-> Fila Antigravity",
            priority="Alta",
            verified_status="Tarefas pendentes",
            evidence="Repositório sincronizado em main",
            work_needed="Desenvolver worker seguro conectado à fila local",
            allowed_mode=AllowedMode.SIMULACAO,
            dependencies="Aprovação do CEO",
            completion_criteria="Testes de worker aprovados",
            ack_status="PENDENTE",
        )
        text = orientation.to_jira_comment()

        required_headers = [
            "[DIRETOR-CLOUD / TRIAGEM]",
            "DESTINATÁRIO: DATA_ENGINEER",
            "TAREFA: Ponte Real GitHub <-> Fila Antigravity",
            "PRIORIDADE: Alta",
            "STATUS VERIFICADO: Tarefas pendentes",
            "EVIDÊNCIA: Repositório sincronizado em main",
            "TRABALHO NECESSÁRIO: Desenvolver worker seguro conectado à fila local",
            "MODO PERMITIDO: SIMULACAO / TESTES OFFLINE",
            "DEPENDÊNCIAS: Aprovação do CEO",
            "CRITÉRIO DE CONCLUSÃO: Testes de worker aprovados",
            "CONFIRMAÇÃO (ACK): PENDENTE",
        ]
        for h in required_headers:
            self.assertIn(h, text)

    def test_agent_routing_profiles(self):
        """Valida a distribuição correta de trabalho para cada perfil de IA."""
        # 1. CODEX: Segurança / Auditoria / Bandit / KAN-7
        issue_security = JiraIssue(
            key="KAN-7",
            summary="Auditoria de vulnerabilidades Bandit e isolamento de chaves",
            status="Tarefas pendentes",
            priority="Alta",
        )
        self.assertEqual(self.triage.determine_recipient(issue_security), AgentProfile.DATA_QUALITY_AUDITOR)

        # 2. ANTIGRAVITY: Fila / Worker / Monitoramento / KAN-6
        issue_worker = JiraIssue(
            key="KAN-6",
            summary="Implementação do worker local Antigravity para fila",
            status="Tarefas pendentes",
            priority="Alta",
        )
        self.assertEqual(self.triage.determine_recipient(issue_worker), AgentProfile.DATA_ENGINEER)

        # 3. COPILOT: Git / CI / Workflow / Pull Request
        issue_git = JiraIssue(
            key="KAN-12",
            summary="Configurar pipeline CI do GitHub Actions e rever Pull Request",
            status="Tarefas pendentes",
            priority="Média",
        )
        self.assertEqual(self.triage.determine_recipient(issue_git), AgentProfile.BI_ANALYST)

        # 4. DOCUMENTALISTA: Curadoria / Docs / README / KAN-10
        issue_docs = JiraIssue(
            key="KAN-10",
            summary="Curadoria da biblioteca de conhecimento e documentação",
            status="Tarefas pendentes",
            priority="Baixa",
        )
        self.assertEqual(self.triage.determine_recipient(issue_docs), AgentProfile.DATAVIZ_SPECIALIST)

        # 5. DIRETOR: Governança / KAN-11
        issue_gov = JiraIssue(
            key="KAN-11",
            summary="Alinhamento estratégico do roadmap do projeto",
            status="Tarefas pendentes",
            priority="Alta",
        )
        self.assertEqual(self.triage.determine_recipient(issue_gov), AgentProfile.BI_LEAD)

    def test_deduplication_prevents_duplicate_comments(self):
        """Garante que a mesma orientação não seja publicada repetidamente sem alteração material."""
        issue = JiraIssue(
            key="KAN-6",
            summary="Ponte Real GitHub <-> Fila Antigravity",
            status="Tarefas pendentes",
            priority="Alta",
            comments=[
                JiraComment(
                    id="101",
                    author="Diretor Cloud",
                    body=(
                        "[DIRETOR-CLOUD / TRIAGEM]\n\n"
                        "DESTINATÁRIO: DATA_ENGINEER\n"
                        "STATUS VERIFICADO: Tarefas pendentes\n"
                        "CONFIRMAÇÃO (ACK): PENDENTE"
                    ),
                    created="2026-10-31T10:00:00Z",
                )
            ],
        )

        # Sem alteração material -> retorna None (deduplicado)
        self.assertIsNone(self.triage.evaluate_issue(issue))

        # Com alteração de status -> gera nova orientação
        issue.status = "Em andamento"
        new_ori = self.triage.evaluate_issue(issue)
        self.assertIsNotNone(new_ori)
        self.assertEqual(new_ori.verified_status, "Em andamento")

    def test_ack_detection(self):
        """Verifica detecção de confirmação (ACK) por parte do agente local."""
        issue_pending = JiraIssue(
            key="KAN-8",
            summary="Integração provedores",
            status="Tarefas pendentes",
            priority="Média",
            comments=[
                JiraComment(
                    id="1",
                    author="Diretor Cloud",
                    body="[DIRETOR-CLOUD / TRIAGEM]\nCONFIRMAÇÃO (ACK): PENDENTE",
                    created="2026-10-31T10:00:00Z",
                )
            ],
        )
        director_comment = issue_pending.comments[0]
        status, author = self.triage.check_ack_status(issue_pending, director_comment)
        self.assertEqual(status, "PENDENTE")

        # Adiciona resposta com ACK
        issue_pending.comments.append(
            JiraComment(
                id="2",
                author="Antigravity",
                body="[ACK] Mensagem recebida. Iniciando análise em modo simulado.",
                created="2026-10-31T10:15:00Z",
            )
        )
        status, author = self.triage.check_ack_status(issue_pending, director_comment)
        self.assertEqual(status, "CONFIRMADO")
        self.assertEqual(author, "Antigravity")

    def test_dry_run_safety(self):
        """Verifica que o modo dry-run não dispara chamadas HTTP POST reais."""
        client = JiraClient(user_email="test@test.com", api_token="secret", dry_run=True)
        res = client.post_comment("KAN-6", "Comentário de teste")
        self.assertEqual(res["status"], "simulated")
        self.assertIn("dry-run-KAN-6", res["id"])

    def test_auth_error_handling(self):
        """Verifica falha segura sem expor senhas ou tokens."""
        client = JiraClient(user_email="", api_token="")
        with self.assertRaises(JiraSecurityError) as ctx:
            client._get_auth_header()
        self.assertIn("Credenciais do Jira ausentes", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_pagination(self, mock_urlopen):
        """Valida que o cliente percorre todas as páginas até atingir o total."""
        client = JiraClient(user_email="a@b.com", api_token="tkn")

        page1 = json.dumps({
            "startAt": 0,
            "maxResults": 2,
            "total": 3,
            "issues": [
                {"key": "KAN-1", "fields": {"summary": "Item 1", "status": {"name": "Pendente"}}},
                {"key": "KAN-2", "fields": {"summary": "Item 2", "status": {"name": "Pendente"}}},
            ],
        }).encode("utf-8")

        page2 = json.dumps({
            "startAt": 2,
            "maxResults": 2,
            "total": 3,
            "issues": [
                {"key": "KAN-3", "fields": {"summary": "Item 3", "status": {"name": "Concluído"}}},
            ],
        }).encode("utf-8")

        mock_resp1 = MagicMock()
        mock_resp1.status = 200
        mock_resp1.read.return_value = page1
        mock_resp1.__enter__.return_value = mock_resp1

        mock_resp2 = MagicMock()
        mock_resp2.status = 200
        mock_resp2.read.return_value = page2
        mock_resp2.__enter__.return_value = mock_resp2

        mock_urlopen.side_effect = [mock_resp1, mock_resp2]

        results = list(client.search_issues_paginated(jql="project = KAN", max_results_per_page=2))
        self.assertEqual(len(results), 3)
        self.assertEqual([i.key for i in results], ["KAN-1", "KAN-2", "KAN-3"])

    def test_dashboard_generation(self):
        """Valida a renderização correta do painel do CEO com distinção entre verificado e declarado."""
        issues = [
            JiraIssue(key="KAN-6", summary="Ponte Real", status="Tarefas pendentes", priority="Alta"),
            JiraIssue(key="KAN-25", summary="Painel do CEO", status="Em andamento", priority="Alta", assignee="BI-Lead"),
            JiraIssue(key="KAN-1", summary="Setup Inicial", status="Concluído", priority="Média"),
        ]
        orientations = [
            TriageOrientation(
                issue_key="KAN-6",
                recipient=AgentProfile.DATA_ENGINEER,
                task_summary="Ponte Real",
                priority="Alta",
                verified_status="Tarefas pendentes",
                evidence="Evidência",
                work_needed="Trabalho",
                allowed_mode=AllowedMode.SIMULACAO,
                dependencies="Nenhuma",
                completion_criteria="Critério",
            )
        ]

        generator = DashboardGenerator(issues, orientations)
        summary = generator.build_summary("2026-10-31T23:55:00Z")
        md = generator.render_markdown(summary)

        self.assertIn("PAINEL EXECUTIVO DO CEO — CENTRAL-IA (KAN-25)", md)
        self.assertIn("Estado Verificado", md)
        self.assertIn("Estado Declarado", md)
        self.assertIn("DATA_ENGINEER", md)
        self.assertIn("KAN-25", md)
        self.assertIn("KAN-6", md)

    def test_run_cycle_with_simulation_file(self):
        """Valida execução completa do ciclo com arquivo de dados simulado."""
        mock_data = {
            "issues": [
                {
                    "key": "KAN-6",
                    "fields": {
                        "summary": "Ponte Real GitHub <-> Fila Antigravity",
                        "status": {"name": "Tarefas pendentes"},
                        "priority": {"name": "Alta"},
                        "description": "Desenvolvimento do worker",
                        "comment": {"comments": []},
                    },
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_file = os.path.join(temp_dir, "issues_mock.json")
            dash_file = os.path.join(temp_dir, "dashboard.md")
            json_file = os.path.join(temp_dir, "dashboard.json")

            with open(mock_file, "w", encoding="utf-8") as f:
                json.dump(mock_data, f)

            code = run_cycle(
                dry_run=True,
                simulated_issues_file=mock_file,
                output_dashboard=dash_file,
                output_json=json_file,
            )
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(dash_file))
            self.assertTrue(os.path.exists(json_file))

    def test_local_agent_protocol_ack_and_update(self):
        """Valida que o protocolo local do agente emite comentários de ACK e Update estruturados."""
        client = JiraClient(user_email="agent@central.ia", api_token="sec", dry_run=True)
        protocol = LocalAgentProtocol(client)

        # 1. Teste ACK
        ack_res = protocol.post_agent_ack("KAN-6", AgentProfile.DATA_ENGINEER, "Worker recebido")
        self.assertEqual(ack_res["status"], "simulated")
        self.assertIn("[DATA_ENGINEER / ACK]", ack_res["comment"])
        self.assertIn("CONFIRMAÇÃO (ACK): CONFIRMADO", ack_res["comment"])

        # 2. Teste Update
        update_res = protocol.post_agent_update(
            "KAN-6",
            AgentProfile.DATA_ENGINEER,
            "ENTREGA",
            "Protótipo offline concluído com testes",
        )
        self.assertEqual(update_res["status"], "simulated")
        self.assertIn("[DATA_ENGINEER / ENTREGA]", update_res["comment"])
        self.assertIn("Protótipo offline", update_res["comment"])

        # 3. Teste tipo de update inválido
        with self.assertRaises(ValueError):
            protocol.post_agent_update("KAN-6", AgentProfile.DATA_ENGINEER, "INVALIDO", "msg")

    def test_listener_heartbeat_and_check(self):
        """Valida que o ouvinte registra batimento cardíaco e verifica instruções sem travar."""
        hb = update_heartbeat(duration_hours=4.0)
        self.assertEqual(hb["status"], "LISTENING")
        self.assertEqual(hb["agent"], "DATA_ENGINEER")
        self.assertEqual(hb["interval_minutes"], 15)

        res = check_incoming_instructions(dry_run=True)
        self.assertIn("heartbeat", res)
        self.assertIn("pending_count", res)


if __name__ == "__main__":
    unittest.main()
