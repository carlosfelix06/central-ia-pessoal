"""Publication boundary check. Prints paths/categories only, never matching secret values."""
from pathlib import Path, PurePosixPath
import re
import subprocess  # nosec B404 â€” fixed local git invocation, no shell or input arguments

PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
FORBIDDEN_DIRS = {".local-tools", ".pytest_cache", "__pycache__", ".local-reports", ".local-backups", "local-history", "fase3-live", "fase3-local", "fase3-demo", "integrations-local", "integrations-live", "integrations-demo"}


def inspect_file(name, raw):
    path = PurePosixPath(name.replace("\\", "/"))
    lower = path.name.lower()
    if (any(part in FORBIDDEN_DIRS or part.startswith(".venv") for part in path.parts)
            or lower == ".env" or (lower.startswith(".env.") and lower != ".env.example")
            or any(ext in lower for ext in (".sqlite3", ".db", ".dpapi"))
            or path.suffix.lower() in (".pem", ".key", ".pfx", ".p12", ".log")
            or lower.startswith(("relatorio-", "preservacao-", "validacao-"))):
        return "private_artifact"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "unexpected_binary_requires_review"
    if any(pattern.search(text) for pattern in PATTERNS):
        return "secret_pattern"
    if lower == ".env.example":
        for line in text.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.strip().endswith(("_API_KEY", "_TOKEN", "_SECRET")) and value.strip():
                    return "filled_example_secret"
    return None


def candidates():
    # Fixed git invocation, no shell, no user-provided arguments.
    output = subprocess.check_output(  # nosec B603 B607
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    return sorted(set(name for name in output.decode().split("\0") if name))


def staged_files():
    # Inspect the actual index too: a safe working copy can hide an older staged secret.
    output = subprocess.check_output(  # nosec B603 B607
        ["git", "ls-files", "--stage", "-z"])
    for entry in output.decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, oid, stage = metadata.split()
        if mode != "100644" and mode != "100755":
            yield name, None
            continue
        # OID comes only from git's index, never from user input or a shell.
        raw = subprocess.check_output(["git", "cat-file", "blob", oid])  # nosec B603 B607
        yield name, raw


def main():
    findings = []
    for name in candidates():
        if not Path(name).is_file():
            continue
        reason = inspect_file(name, Path(name).read_bytes())
        if reason:
            findings.append((name, reason))
    for name, raw in staged_files():
        reason = inspect_file(name, raw) if raw is not None else "unexpected_index_mode_requires_review"
        if reason:
            findings.append((name, "index_" + reason))
    for name, reason in findings:
        print(name + ": " + reason)
    if findings:
        raise SystemExit(1)
    print("Publication boundary: no known secret patterns or private artifacts found.")


if __name__ == "__main__":
    main()
