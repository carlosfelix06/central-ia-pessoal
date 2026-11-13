"""Bandit gate with one reviewed, hash-bound legacy finding; rejects new findings."""
import hashlib
import json
from pathlib import Path
import subprocess  # nosec B404 â€” fixed invocation of the installed Bandit module
import sys


def unaccepted(report, root=Path(".")):
    accepted = json.loads((root / "config/bandit-baseline.json").read_text(encoding="utf-8"))["accepted_findings"]
    unexpected = []
    for finding in report.get("results", []):
        full = Path(finding["filename"])
        full = full if full.is_absolute() else root / full
        rel = full.resolve().relative_to(root.resolve()).as_posix()
        allowed = any(row["path"] == rel and row["test_id"] == finding["test_id"]
                      and row["line_number"] == finding["line_number"]
                      and row["file_sha256"] == hashlib.sha256(full.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                      for row in accepted)
        if not allowed:
            unexpected.append({"path":rel,"test_id":finding["test_id"],"line":finding["line_number"]})
    if report.get("errors"):
        unexpected.append({"error":"bandit_scan_errors"})
    return unexpected


def main():
    result = subprocess.run(  # nosec B603 â€” interpreter and arguments are fixed, no shell
        [sys.executable, "-m", "bandit", "-c", "pyproject.toml", "-r", "central", "tools", "-f", "json", "-q"],
        capture_output=True, text=True, timeout=90, check=False)
    if result.returncode not in (0, 1):
        raise SystemExit("Bandit did not complete; raw output omitted.")
    unexpected = unaccepted(json.loads(result.stdout))
    print(json.dumps({"unexpected_findings":unexpected,"total_findings":len(json.loads(result.stdout).get("results",[]))}))
    if unexpected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
