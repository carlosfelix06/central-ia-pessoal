"""Offline YAML and security contracts; complements actionlint, not GitHub execution."""
import json
from pathlib import Path
import re
import yaml


def validate(root=Path(".")):
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    ai = yaml.safe_load((root / ".github/workflows/copilot-events.yml").read_text(encoding="utf-8"))
    assert ci["name"] == "CI"
    assert set(ci["on"]) == {"push", "pull_request"}
    assert ci["on"]["push"]["branches"] == ["main"]
    assert ci["permissions"] == {"contents": "read"}
    assert "CENTRAL_CI_ENABLED" in ci["jobs"]["checks"]["if"]
    assert "CENTRAL_AI_ENABLED" in ai["jobs"]["coordinate"]["if"]
    assert ai["on"] == {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}}
    assert ai["concurrency"]["cancel-in-progress"] is False
    assert ai["jobs"]["coordinate"]["environment"] == "copilot-automation"
    assert ai["permissions"] == {"contents":"read","actions":"read","pull-requests":"read","issues":"write"}
    for workflow in (ci, ai):
        assert "pull_request_target" not in workflow["on"]
        for job in workflow["jobs"].values():
            assert 1 <= job["timeout-minutes"] <= 10
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/(checkout|setup-python)@[0-9a-f]{40}", step["uses"])
                if step.get("uses", "").startswith("actions/checkout"):
                    assert step["with"]["persist-credentials"] is False
    steps = ai["jobs"]["coordinate"]["steps"]
    assert steps[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    for name in ("failure", "review"):
        template = json.loads((root / f"config/copilot-automations/{name}.json").read_text(encoding="utf-8"))
        assert template["enabled"] is False
        assert template["triggers"] == {}
        assert template["require_actor_write_permission"] is True
        assert template["tools"] == []
    ci_text = (root / ".github/workflows/ci.yml").read_text()
    ai_text = (root / ".github/workflows/copilot-events.yml").read_text()
    assert ci["jobs"]["checks"]["env"]["INTEGRATIONS_ENABLE_REAL"] == "false"
    assert ci["jobs"]["checks"]["env"]["GEMINI_API_KEY"] == ""
    assert "secrets." not in ci_text
    for word in ("upload-artifact", "download-artifact", "DPAPI", "Save-OpenAIKey", "Prepare-FirstOpenAITest"):
        assert word not in ci_text + ai_text
    assert "OPENAI_API_KEY" not in ai_text
    return {"workflows": 2, "disabled_copilot_templates": 2}


if __name__ == "__main__":
    print(json.dumps(validate()))
