"""Testes unitários para o Transporte e Worker Real GitHub REST (Issue #2)."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from central.bridge.contract import BridgeTask
from central.bridge.github_worker import (
    GitHubBridgeWorker,
    GitHubRESTClient,
    GitHubSecurityError,
)
from central.bridge.queue import BridgeQueue


class TestGitHubWorker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_queue.sqlite3")
        self.checkpoint_path = os.path.join(self.temp_dir.name, "checkpoint.json")
        self.queue = BridgeQueue(self.db_path)

    def tearDown(self):
        self.queue.close()
        self.temp_dir.cleanup()

    def test_security_boundary_enforcement(self):
        """Valida que requisições fora do repositório autorizado são sumariamente bloqueadas."""
        client = GitHubRESTClient(token="mock_token")
        with self.assertRaises(GitHubSecurityError) as ctx:
            client._request("GET", "/repos/outro_usuario/outro_repo/issues")
        self.assertIn("fronteira de segurança", str(ctx.exception))

    def test_missing_token_safety(self):
        """Valida tratamento seguro quando o token não está configurado."""
        client = GitHubRESTClient(token="")
        with self.assertRaises(GitHubSecurityError) as ctx:
            client._request("GET", "/repos/carlosfelix06/central-ia-pessoal/issues")
        self.assertIn("Token do GitHub ausente", str(ctx.exception))

    def test_authorization_label_filter(self):
        """Garante que apenas Issues com as labels autorizadas sejam processadas."""
        worker = GitHubBridgeWorker(self.queue, checkpoint_file=self.checkpoint_path)

        unauth_issue = {"number": 1, "labels": [{"name": "bug"}, {"name": "doc"}]}
        auth_issue = {"number": 2, "labels": [{"name": "teste-ponte"}]}
        auth_issue_2 = {"number": 3, "labels": [{"name": "antigravity-worker"}]}

        self.assertFalse(worker.is_issue_authorized(unauth_issue))
        self.assertTrue(worker.is_issue_authorized(auth_issue))
        self.assertTrue(worker.is_issue_authorized(auth_issue_2))

    def test_process_issue_offline_and_checkpoint(self):
        """Valida o ciclo completo de enfileiramento, execução offline e gravação de checkpoint."""
        mock_client = MagicMock()
        worker = GitHubBridgeWorker(
            self.queue,
            client=mock_client,
            checkpoint_file=self.checkpoint_path,
            allow_remote_write=False,
        )

        issue_payload = {
            "number": 99,
            "title": "Teste de Integração de Fila",
            "body": "Instrução de teste offline",
            "labels": [{"name": "teste-ponte"}],
        }

        # 1. Primeira execução: deve processar e criar recibo
        res = worker.process_issue(issue_payload)
        self.assertIsNotNone(res)
        self.assertEqual(res["issue_number"], 99)
        self.assertEqual(res["status"], "simulated")
        self.assertIn("[EQUIPE_BI / PONTE GITHUB]", res["receipt"])
        self.assertIn("gh-corr-issue-99", res["receipt"])

        # O client remoto não deve ter sido chamado para postar em dry-run
        mock_client.post_status_comment.assert_not_called()

        # Checkpoint gravado no disco
        self.assertTrue(os.path.exists(self.checkpoint_path))

        # 2. Segunda execução com mesmo payload: deve ser ignorada por deduplicação
        res2 = worker.process_issue(issue_payload)
        self.assertIsNone(res2)

    def test_remote_write_mode(self):
        """Valida que quando allow_remote_write=True, o comentário é enviado ao GitHub."""
        mock_client = MagicMock()
        mock_client.post_status_comment.return_value = {"id": 12345}

        worker = GitHubBridgeWorker(
            self.queue,
            client=mock_client,
            checkpoint_file=self.checkpoint_path,
            allow_remote_write=True,
        )

        issue_payload = {
            "number": 100,
            "title": "Teste Escrita Remota",
            "body": "Payload de teste",
            "labels": [{"name": "antigravity-worker"}],
        }

        res = worker.process_issue(issue_payload)
        self.assertEqual(res["status"], "confirmed_remote")
        mock_client.post_status_comment.assert_called_once()
        args = mock_client.post_status_comment.call_args[0]
        self.assertEqual(args[0], 100)
        self.assertIn("[EQUIPE_BI / PONTE GITHUB]", args[1])


if __name__ == "__main__":
    unittest.main()
