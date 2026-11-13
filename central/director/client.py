"""Cliente seguro e enxuto para a API REST do Jira Cloud via biblioteca padrão."""
import base64
import json
import logging
import os
import re
from typing import Any, Dict, Generator, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from .models import JiraComment, JiraIssue

logger = logging.getLogger("central.director.client")

_GET_ALLOWED = (
    re.compile(r"^/rest/api/3/search/jql$"),
    re.compile(r"^/rest/api/3/issue/KAN-\d+/comment$"),
)
_POST_ALLOWED = re.compile(r"^/rest/api/3/issue/KAN-\d+/comment$")


class JiraSecurityError(Exception):
    """Erro seguro que nunca expõe tokens, senhas ou dados sensíveis em logs."""


class JiraClient:
    """Cliente HTTP com suporte a paginação JQL, allowlist de segurança e modo dry-run."""

    def __init__(
        self,
        base_url: str = "https://bi-enterprise.atlassian.net",
        user_email: Optional[str] = None,
        api_token: Optional[str] = None,
        dry_run: bool = False,
        timeout_seconds: int = 20,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url or "")
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".atlassian.net"):
            raise JiraSecurityError("JIRA_BASE_URL deve ser no formato https://<site>.atlassian.net")
        self.base_url = f"https://{parsed.hostname}"
        self.user_email = user_email or os.getenv("JIRA_USER_EMAIL", "")
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN", "")
        self.dry_run = dry_run
        self.timeout = timeout_seconds

    def _get_auth_header(self) -> str:
        if not self.user_email or not self.api_token:
            raise JiraSecurityError("Credenciais do Jira ausentes (JIRA_USER_EMAIL ou JIRA_API_TOKEN não definidos).")
        token_str = f"{self.user_email}:{self.api_token}"
        encoded = base64.b64encode(token_str.encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Validação estrita contra a Allowlist de URLs permitidas
        allowed = (
            any(p.match(path) for p in _GET_ALLOWED) if method == "GET" else bool(_POST_ALLOWED.match(path))
        )
        if method not in ("GET", "POST") or not allowed:
            raise JiraSecurityError(f"Operação {method} {path} bloqueada: fora da allowlist estrita do Jira.")

        url = f"{self.base_url}{path}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Central-IA-DiretorCloud/1.0",
        }

        if self.user_email and self.api_token:
            headers["Authorization"] = self._get_auth_header()
        else:
            raise JiraSecurityError("Credenciais do Jira ausentes (JIRA_USER_EMAIL ou JIRA_API_TOKEN não definidos).")

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:  # nosec B310
                raw_bytes = response.read(10_000_000)
                if response.status not in (200, 201, 204):
                    raise JiraSecurityError(f"Jira retornou status inesperado: {response.status}")
                if not raw_bytes:
                    return {}
                return json.loads(raw_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise JiraSecurityError(f"Falha de autenticação ou permissão insuficiente no Jira (HTTP {e.code}).")
            if e.code == 404:
                raise JiraSecurityError(f"Recurso não encontrado no Jira (HTTP 404): {path}")
            raise JiraSecurityError(f"Erro na requisição ao Jira (HTTP {e.code}).")
        except urllib.error.URLError as e:
            raise JiraSecurityError(f"Falha de conexão com o Jira: {type(e.reason).__name__}")
        except json.JSONDecodeError:
            raise JiraSecurityError("Resposta do Jira inválida ou não formatada em JSON.")

    def search_issues_paginated(
        self,
        jql: str = "project = KAN ORDER BY priority DESC, updated DESC",
        max_results_per_page: int = 20,
        max_pages: int = 20,
    ) -> Generator[JiraIssue, None, None]:
        """Consulta todas as páginas de resultados do JQL no Jira."""
        start_at = 0
        pages_retrieved = 0
        next_token = None

        while pages_retrieved < max_pages:
            params: Dict[str, Any] = {
                "jql": jql,
                "maxResults": max_results_per_page,
                "fields": "summary,status,priority,assignee,description,created,updated,comment",
            }
            if next_token:
                params["nextPageToken"] = next_token
            else:
                params["startAt"] = start_at

            data = self._request("GET", "/rest/api/3/search/jql", params=params)
            issues_data = data.get("issues", [])
            total = data.get("total", 0)
            next_token = data.get("nextPageToken")

            for item in issues_data:
                yield self._parse_issue(item)

            pages_retrieved += 1
            start_at += len(issues_data)

            if data.get("isLast") is True or (not next_token and start_at >= total) or not issues_data:
                break

    def get_comments(self, issue_key: str) -> List[JiraComment]:
        """Obtém o histórico de comentários de uma atividade."""
        data = self._request("GET", f"/rest/api/3/issue/{issue_key}/comment")
        comments_list: List[JiraComment] = []
        for c in data.get("comments", []):
            body_text = self._extract_adf_or_text(c.get("body"))
            author = c.get("author", {}).get("displayName") or c.get("author", {}).get("emailAddress") or "unknown"
            comments_list.append(
                JiraComment(
                    id=str(c.get("id")),
                    author=author,
                    body=body_text,
                    created=c.get("created", ""),
                    updated=c.get("updated"),
                )
            )
        return comments_list

    def post_comment(self, issue_key: str, comment_text: str) -> Dict[str, Any]:
        """Publica um comentário na atividade. Respeita estritamente o modo dry-run."""
        if self.dry_run:
            logger.info("[DRY-RUN] Comentário suprimido para %s:\n%s", issue_key, comment_text)
            return {
                "id": f"dry-run-{issue_key}",
                "status": "simulated",
                "issue_key": issue_key,
                "comment": comment_text,
            }

        body_payload = {
            "body": {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": comment_text,
                            }
                        ],
                    }
                ],
            }
        }

        return self._request("POST", f"/rest/api/3/issue/{issue_key}/comment", body=body_payload)

    def _parse_issue(self, raw: Dict[str, Any]) -> JiraIssue:
        fields = raw.get("fields", {})
        key = raw.get("key", "")
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "Desconhecido")
        priority = fields.get("priority", {}).get("name", "Normal")
        assignee_data = fields.get("assignee")
        assignee = assignee_data.get("displayName") if isinstance(assignee_data, dict) else None
        description = self._extract_adf_or_text(fields.get("description"))
        created = fields.get("created", "")
        updated = fields.get("updated", "")

        comments_raw = fields.get("comment", {}).get("comments", [])
        comments: List[JiraComment] = []
        for c in comments_raw:
            c_body = self._extract_adf_or_text(c.get("body"))
            author = c.get("author", {}).get("displayName") or "unknown"
            comments.append(
                JiraComment(
                    id=str(c.get("id")),
                    author=author,
                    body=c_body,
                    created=c.get("created", ""),
                    updated=c.get("updated"),
                )
            )

        return JiraIssue(
            key=key,
            summary=summary,
            status=status,
            priority=priority,
            assignee=assignee,
            description=description,
            created=created,
            updated=updated,
            comments=comments,
            raw_fields=fields,
        )

    def _extract_adf_or_text(self, content: Any) -> str:
        """Converte nós ADF ou strings legadas em texto plano legível."""
        if isinstance(content, str):
            return content
        if not isinstance(content, dict):
            return ""

        text_parts: List[str] = []

        def _traverse(node: Any):
            if isinstance(node, dict):
                if node.get("type") == "text" and "text" in node:
                    text_parts.append(node["text"])
                for sub in node.get("content", []):
                    _traverse(sub)
            elif isinstance(node, list):
                for sub in node:
                    _traverse(sub)

        _traverse(content)
        return "".join(text_parts).strip()
