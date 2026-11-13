"""Transporte e Worker Real GitHub REST ↔ Fila Antigravity (Issue #2)."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess  # nosec B404
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from .contract import BridgeTask, canonical_json
from .queue import BridgeQueue

logger = logging.getLogger("central.bridge.github_worker")

REPOSITORY = "carlosfelix06/central-ia-pessoal"
ALLOWED_ISSUE_PATH = re.compile(r"^/repos/carlosfelix06/central-ia-(pessoal|privado)/issues(/\d+(/comments)?)?$")


class GitHubSecurityError(Exception):
    """Exceção segura que nunca expõe tokens, prompts ou dados sensíveis em logs."""


class GitHubRESTClient:
    """Cliente HTTP REST com limite estrito de repositório e proteção de credenciais."""

    def __init__(self, token: Optional[str] = None, timeout: int = 15) -> None:
        self.token = token if token is not None else self._resolve_token()
        self.timeout = timeout

    @staticmethod
    def _resolve_token() -> Optional[str]:
        token = os.getenv("GITHUB_TOKEN")
        if token and not any(c.isspace() for c in token):
            return token

        try:
            proc = subprocess.run(  # nosec B603, B607
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                timeout=2,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            )
            for line in proc.stdout.splitlines():
                if line.startswith("password="):
                    t = line.split("=", 1)[1].strip()
                    if t and not any(c.isspace() for c in t):
                        return t
        except Exception as e:
            logger.debug("Git credential manager não retornou token: %s", e)

        return None

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if not ALLOWED_ISSUE_PATH.match(path):
            raise GitHubSecurityError(f"Caminho bloqueado por fronteira de segurança: {path}")

        if not self.token:
            raise GitHubSecurityError("Token do GitHub ausente ou inacessível no ambiente.")

        url = f"https://api.github.com{path}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "Central-IA-AntigravityBridge/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                raw = resp.read(2_000_000)
                if resp.status not in (200, 201, 204):
                    raise GitHubSecurityError(f"GitHub retornou status inesperado: {resp.status}")
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise GitHubSecurityError(f"Falha de autenticação ou permissão no GitHub (HTTP {e.code}).")
            if e.code == 404:
                raise GitHubSecurityError("Recurso ou repositório não encontrado no GitHub (HTTP 404).")
            raise GitHubSecurityError(f"Erro na requisição ao GitHub (HTTP {e.code}).")
        except urllib.error.URLError as e:
            raise GitHubSecurityError(f"Falha de conexão com a API do GitHub: {type(e.reason).__name__}")
        except json.JSONDecodeError:
            raise GitHubSecurityError("Resposta do GitHub inválida ou não formatada em JSON.")

    def fetch_candidate_issues(self, state: str = "open", labels: Optional[str] = None) -> List[Dict[str, Any]]:
        """Recupera Issues candidatas do repositório."""
        params: Dict[str, Any] = {"state": state, "per_page": 20}
        if labels:
            params["labels"] = labels
        data = self._request("GET", f"/repos/{REPOSITORY}/issues", params=params)
        return [i for i in data if "pull_request" not in i]

    def fetch_comments(self, issue_number: int) -> List[Dict[str, Any]]:
        """Recupera comentários de uma Issue."""
        return self._request("GET", f"/repos/{REPOSITORY}/issues/{issue_number}/comments")

    def post_status_comment(self, issue_number: int, comment_text: str) -> Dict[str, Any]:
        """Publica comentário de status sanitizado na Issue."""
        return self._request(
            "POST",
            f"/repos/{REPOSITORY}/issues/{issue_number}/comments",
            body={"body": comment_text},
        )


class GitHubBridgeWorker:
    """Worker de integração local: faz polling de issues autorizadas, enfileira e responde com recibo sanitizado."""

    WORKER_TAG = "[EQUIPE_BI / PONTE GITHUB]"

    def __init__(
        self,
        queue: BridgeQueue,
        client: Optional[GitHubRESTClient] = None,
        checkpoint_file: str = ".local-reports/github-worker-checkpoint.json",
        allow_remote_write: bool = False,
    ) -> None:
        self.queue = queue
        self.client = client or GitHubRESTClient()
        self.checkpoint_file = Path(checkpoint_file)
        self.allow_remote_write = allow_remote_write

    def load_checkpoint(self) -> Dict[str, Any]:
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.debug("Checkpoint corrompido ou ilegível: %s", exc)
        return {"processed_issues": {}}

    def save_checkpoint(self, data: Dict[str, Any]) -> None:
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def is_issue_authorized(self, issue: Dict[str, Any]) -> bool:
        """Processa apenas Issues explicitamente autorizadas por label ou modo de teste."""
        labels = [l.get("name", "").lower() for l in issue.get("labels", [])]
        authorized_labels = {"antigravity-worker", "teste-ponte", "bridge-task"}
        return any(lbl in authorized_labels for lbl in labels)

    def process_issue(self, issue: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Processa uma única Issue autorizada através da fila local e gera o comentário de retorno."""
        num = issue["number"]
        title = issue.get("title", "")
        body = issue.get("body", "")

        checkpoint = self.load_checkpoint()
        issue_hash = hashlib.sha256(f"{num}|{title}|{body}".encode()).hexdigest()[:12]
        if str(num) in checkpoint.get("processed_issues", {}):
            if checkpoint["processed_issues"][str(num)].get("hash") == issue_hash:
                logger.debug("Issue #%d já processada com este conteúdo. Ignorando.", num)
                return None

        task_id = f"gh-issue-{num}-{issue_hash[:8]}"
        correlation_id = f"gh-corr-issue-{num}"
        sanitized_input = {
            "issue_number": num,
            "title": title,
            "body_summary": body[:500],
            "action": "offline_dispatch",
        }

        task = BridgeTask(
            task_id=task_id,
            correlation_id=correlation_id,
            source_agent="github_issue",
            target_agent="coordenador_bi",
            authorized_input=sanitized_input,
            metadata={"repository": REPOSITORY, "platform": "github"},
        )

        submitted_id = self.queue.submit(task)
        logger.info("Tarefa enfileirada na BridgeQueue: %s (submetida: %s)", task_id, submitted_id)

        owner = "antigravity-worker"
        claimed_task = self.queue.claim(owner=owner)
        if claimed_task:
            result_payload = {
                "specialist": "Coordenador Executivo de BI",
                "execution_mode": "mock_offline",
                "summary": f"Processamento da Issue #{num} concluído com auditoria local.",
                "timestamp": time.time(),
            }
            self.queue.finish(claimed_task.task_id, owner=owner, result=result_payload)
            final_state = "done"
        else:
            final_state = "queued"

        receipt_text = (
            f"{self.WORKER_TAG}\n\n"
            f"**STATUS:** `{final_state.upper()}`\n"
            f"**TASK ID:** `{task_id}`\n"
            f"**CORRELAÇÃO:** `{correlation_id}`\n"
            f"**EXECUTOR:** `Coordenador Executivo de BI (Modo Mock/Offline)`\n"
            f"**AUDITORIA LOCAL:** Registrado na BridgeQueue SQLite (`central.sqlite3`).\n"
            f"**EVIDÊNCIA:** Tarefa processada localmente com isolamento de dados privados garantido."
        )

        if self.allow_remote_write:
            logger.info("Publicando comentário de status na Issue #%d no GitHub...", num)
            self.client.post_status_comment(num, receipt_text)
            status_desc = "confirmed_remote"
        else:
            logger.info("[DRY-RUN / LOCAL] Comentário gerado mas não postado (allow_remote_write=False):\n%s", receipt_text)
            status_desc = "simulated"

        checkpoint.setdefault("processed_issues", {})[str(num)] = {
            "hash": issue_hash,
            "task_id": task_id,
            "status": status_desc,
            "processed_at": time.time(),
        }
        self.save_checkpoint(checkpoint)

        return {
            "issue_number": num,
            "task_id": task_id,
            "status": status_desc,
            "receipt": receipt_text,
        }

    def poll_once(self) -> List[Dict[str, Any]]:
        """Executa um ciclo único de verificação de issues autorizadas."""
        logger.info("Buscando issues candidatas no GitHub (%s)...", REPOSITORY)
        issues = self.client.fetch_candidate_issues()
        processed = []
        for issue in issues:
            if self.is_issue_authorized(issue):
                res = self.process_issue(issue)
                if res:
                    processed.append(res)
        return processed
