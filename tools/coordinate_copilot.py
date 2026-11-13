"""Bridge workflow_run -> Copilot Automations API. Disabled unless explicitly enabled.

No checkout of PR code, no artifacts/log text, no remote retry. Receipt BEFORE task POST.
"""
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlencode

REPOSITORY = "carlosfelix06/central-ia-pessoal"
PREFIX = "Central dispatch: "
RELEVANT = ("central/", "tests/", "tools/", ".github/", "config/", "scripts/")
CONFIG_FILES = {"pyproject.toml", "requirements-dev.lock", ".env.example", ".gitignore"}


class SafeError(Exception):
    """Never contains raw HTTP bodies, prompts or tokens."""


class GitHub:
    def __init__(self, token):
        if not token or any(c.isspace() for c in token):
            raise SafeError("missing_token")
        self.token = token

    def request(self, method, path, body=None):
        if not path.startswith(("/repos/" + REPOSITORY + "/", "/agents/repos/" + REPOSITORY + "/")):
            raise SafeError("repository_boundary")
        conn = http.client.HTTPSConnection("api.github.com", timeout=15)
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                         headers={"Authorization": "Bearer " + self.token,
                                  "Accept": "application/vnd.github+json",
                                  "Content-Type": "application/json",
                                  "X-GitHub-Api-Version": "2026-03-10",
                                  "User-Agent": "central-event-coordinator"})
            response = conn.getresponse()
            raw = response.read(2_000_001)
            if response.status not in (200, 201, 202) or len(raw) > 2_000_000:
                raise SafeError("github_request_failed")
            return json.loads(raw)
        except SafeError:
            raise
        except Exception:
            raise SafeError("github_outcome_unknown_no_retry") from None
        finally:
            conn.close()


def is_bot(user):
    return not isinstance(user, dict) or user.get("type") != "User" or user.get("login", "").endswith("[bot]")


def relevant(paths):
    return any(p.startswith(RELEVANT) or p in CONFIG_FILES for p in paths)


def run_gate(event, config):
    if config.get("CENTRAL_AI_ENABLED") != "true":
        return "disabled"
    if event.get("action") != "completed" or event.get("repository", {}).get("full_name") != REPOSITORY:
        return "wrong_event"
    run = event.get("workflow_run", {})
    if run.get("name") != "CI" or run.get("status") != "completed" or run.get("run_attempt") != 1:
        return "wrong_run_or_retry"
    if run.get("event") not in ("push", "pull_request") or is_bot(run.get("actor")):
        return "untrusted_actor_or_event"
    if run.get("head_repository", {}).get("full_name") != REPOSITORY:
        return "fork"
    if run.get("conclusion") not in ("failure", "success"):
        return "conclusion"
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        return "invalid_sha"
    if type(run.get("id")) is not int or run["id"] <= 0:
        return "invalid_id"
    return None


class Coordinator:
    def __init__(self, metadata, agents, config):
        self.metadata, self.agents, self.config = metadata, agents, config
        self.base = "/repos/" + REPOSITORY

    def pages(self, path, maximum=20):
        for page in range(1, maximum + 1):
            separator = "&" if "?" in path else "?"
            rows = self.metadata.request("GET", path + separator + urlencode({"per_page": 100, "page": page}))
            if not isinstance(rows, list):
                raise SafeError("invalid_metadata")
            yield from rows
            if len(rows) < 100:
                return
        raise SafeError("pagination_limit_fail_closed")

    def dispatch(self, event):
        reason = run_gate(event, self.config)
        if reason:
            return {"state": "skipped", "reason": reason}
        run = event["workflow_run"]
        # Validate the authoritative run again; event data alone grants no authority.
        current = self.metadata.request("GET", self.base + f"/actions/runs/{run['id']}")
        for key in ("id", "name", "head_sha", "event", "conclusion", "run_attempt"):
            if current.get(key) != run.get(key):
                return {"state": "skipped", "reason": "run_changed"}
        if current.get("path") != ".github/workflows/ci.yml":
            return {"state": "skipped", "reason": "wrong_workflow_file"}
        sha = run["head_sha"]
        commit = self.metadata.request("GET", self.base + "/commits/" + sha)
        if is_bot(commit.get("author")):
            return {"state": "skipped", "reason": "bot_commit"}
        repo = self.metadata.request("GET", self.base + "/branches/main")
        pr_number = None
        if run["event"] == "pull_request":
            prs = current.get("pull_requests", [])
            if len(prs) != 1 or type(prs[0].get("number")) is not int:
                return {"state": "skipped", "reason": "ambiguous_pr"}
            pr_number = prs[0]["number"]
            pr = self.metadata.request("GET", self.base + f"/pulls/{pr_number}")
            if (pr.get("state") != "open" or pr.get("draft") is not False or is_bot(pr.get("user"))
                    or pr.get("base", {}).get("ref") != "main"
                    or pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
                    or pr.get("head", {}).get("sha") != sha):
                return {"state": "skipped", "reason": "draft_fork_bot_or_stale_pr"}
            labels = {label["name"] for label in pr.get("labels", [])}
            if "no-ai" in labels:
                return {"state": "skipped", "reason": "no_ai_label"}
            kind = "failure" if run["conclusion"] == "failure" else "review"
            if "copilot:" + ("investigate" if kind == "failure" else "review") not in labels:
                return {"state": "skipped", "reason": "approval_label_missing"}
            paths = [row["filename"] for row in self.pages(self.base + f"/pulls/{pr_number}/files", maximum=5)]
            if not relevant(paths):
                return {"state": "skipped", "reason": "docs_only"}
        else:
            if (run.get("head_branch") != "main" or repo.get("commit", {}).get("sha") != sha
                    or run["conclusion"] != "failure"):
                return {"state": "skipped", "reason": "not_current_main_failure"}
            kind = "failure"
        if self.config.get("CENTRAL_COPILOT_" + kind.upper() + "_ENABLED") != "true":
            return {"state": "skipped", "reason": "feature_disabled"}
        if self.config.get("CENTRAL_COPILOT_TOOLS_REVIEWED") != "true":
            return {"state": "skipped", "reason": "tools_not_reviewed"}
        try:
            automation = str(uuid.UUID(self.config.get("COPILOT_" + kind.upper() + "_AUTOMATION_ID", "")))
        except ValueError:
            raise SafeError("invalid_automation_id") from None
        approved = self.config.get("APPROVED_READ_TOOLS", {}).get(kind, [])
        if not approved or any(not isinstance(tool, str) for tool in approved):
            raise SafeError("read_tool_catalog_not_configured")
        definition = self.agents.request("GET", "/agents/repos/" + REPOSITORY + "/automations/" + automation)
        if sorted(definition.get("tools", [])) != sorted(approved):
            raise SafeError("automation_tools_changed")
        if (definition.get("enabled") is not True or definition.get("triggers")
                or definition.get("require_actor_write_permission") is not True):
            raise SafeError("automation_not_enabled_or_duplicate_native_triggers")
        permission = self.metadata.request("GET", self.base + "/collaborators/" + run["actor"]["login"] + "/permission")
        if permission.get("permission") not in ("admin", "maintain", "write"):
            return {"state": "skipped", "reason": "actor_cannot_write"}
        scope = f"pr-{pr_number}" if pr_number else "main"
        title = PREFIX + kind + ":" + scope + ":" + sha
        receipts = [r for r in self.pages(self.base + "/issues?state=all")
                    if r.get("title", "").startswith(PREFIX) and not r.get("pull_request")]
        if any(r["title"] == title for r in receipts):
            return {"state": "skipped", "reason": "receipt_exists_no_retry"}
        now = dt.datetime.now(dt.timezone.utc)
        recent = sum((now - dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))).total_seconds() < 3600
                     for r in receipts)
        per_pr = sum((":" + scope + ":") in r["title"] for r in receipts) if pr_number else 0
        if recent >= 2 or per_pr >= 2:
            return {"state": "skipped", "reason": "session_cap"}
        # No raw log, title, body, branch name or user prompt is transmitted.
        context = json.dumps({"repository": REPOSITORY, "sha": sha, "pr_number": pr_number,
                              "workflow_run_id": run["id"], "kind": kind,
                              "workflow_url": f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}"}, sort_keys=True)
        receipt = self.metadata.request("POST", self.base + "/issues", {
            "title": title, "body": "State: dispatching. Reserved before API request. No automatic replay.\n\n" + context})
        number = receipt["number"]
        try:
            answer = self.agents.request("POST", "/agents/repos/" + REPOSITORY + "/automations/" + automation + "/tasks",
                                         {"event": "manual", "context": context})
            task = answer.get("task_id")
            if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task):
                raise SafeError("acceptance_without_task_id")
            state = "accepted_not_confirmed_running"
        except Exception:
            task, state = None, "uncertain_no_retry"
        self.metadata.request("PATCH", self.base + f"/issues/{number}", {
            "body": "State: " + state + ".\nTask ID: " + (task or "unknown") + "\n\n" + context})
        return {"state": state, "receipt": number, "task_id": task}


def main():
    config = dict(os.environ)
    if config.get("CENTRAL_AI_ENABLED") != "true":
        print(json.dumps({"state": "disabled", "network_requests": 0}))
        return
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    reason = run_gate(event, config)
    if reason:
        print(json.dumps({"state": "skipped", "reason": reason}))
        return
    config["APPROVED_READ_TOOLS"] = json.loads(Path("config/copilot-approved-tools.json").read_text(encoding="utf-8"))
    result = Coordinator(GitHub(os.getenv("GH_METADATA_TOKEN")),
                         GitHub(os.getenv("COPILOT_AUTOMATION_TOKEN")), config).dispatch(event)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Coordinator failed closed; no raw errors or automatic retry. Inspect receipt and task history.") from None
