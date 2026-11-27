"""Read-only baseline audits. Never include MCP arguments, URLs or env values."""
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import re
from shutil import which
import sys
from urllib.parse import urlsplit

DECLARATIONS = ("requirements-search.txt", "requirements-flight.txt")
PROMPT = "提示词/旅游助手系统提示词.md"
SOURCE_FILES = ("travel_agent.py", "travel_search_mcp.py", "test_travel_search_mcp.py",
                ".gitignore", PROMPT, *DECLARATIONS)


def digest(file):
    return sha256(file.read_bytes()).hexdigest()


def json_digest(value):
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def audit_dependencies(text, runtime, locks, environment):
    rows = []
    for raw in text.splitlines():
        definition, _, marker = raw.strip().partition(";")
        if not definition or definition.startswith("#"):
            continue
        if marker.strip() not in ("", 'sys_platform == "win32"'):
            raise ValueError("Unsupported requirement marker in baseline declaration")
        if marker and sys.platform != "win32":
            continue
        match = re.fullmatch(r"([\w.-]+)(?:(?:==([\w.-]+))|(?:\s*@\s*git\+\S+@([a-f0-9]{40})))?", definition.strip())
        if not match:
            raise ValueError("Unsupported requirement format in baseline declaration")
        name, pin, vcs = match.groups()
        name = name.lower().replace("_", "-")
        installed = runtime.get(name)
        installed_version = installed.get("version") if isinstance(installed, dict) else installed
        installed_commit = installed.get("commit") if isinstance(installed, dict) else None
        lock = locks.get(name)
        reasons = []
        if not installed_version:
            reasons.append("dependency_missing")
        elif pin and pin != installed_version:
            reasons.append("declared_runtime_drift")
        elif vcs and vcs != installed_commit:
            reasons.append("vcs_origin_unverified" if installed_commit is None else "vcs_commit_drift")
        if lock and ((pin or vcs) and lock != (pin or vcs) or lock != (installed_commit if vcs else installed_version)):
            reasons.append("lock_drift")
        rows.append({
            "dependency": name, "environment": environment, "declaration": pin or vcs,
            "lock": lock, "runtime": installed_version, "runtime_commit": installed_commit,
            "status": "BLOCKED" if reasons else "PASS",
            "reason": ",".join(reasons),
        })
    return rows


def required_argument_gaps(schema, mapping):
    return sorted(param for param in schema.get("required", []) if not mapping.get(param))


def audit_mcp(config, root):
    services = {}
    for name, entry in config.get("mcpServers", {}).items():
        command = entry.get("command")
        summary = {"env_names": sorted(entry.get("env", {})), "external_verified": False,
                   "status": "BLOCKED"}
        if command:
            path = Path(command)
            looks_like_path = len(Path(command).parts) > 1 or PureWindowsPath(command).is_absolute()
            executable = (root / path).is_file() if looks_like_path else bool(which(command))
            scripts = [arg for arg in entry.get("args", []) if arg.lower().endswith(".py") and "://" not in arg]
            entrypoints_ok = all((root / script).is_file() for script in scripts)
            summary.update(
                transport="stdio", hardcoded_absolute_path=path.is_absolute() or PureWindowsPath(command).is_absolute(),
                command_exists=executable, script_paths_exist=entrypoints_ok,
                static_ready=executable and entrypoints_ok,
                reason="command_missing" if not executable else
                "script_missing" if not entrypoints_ok else "handshake_not_run",
            )
        else:
            parsed = urlsplit(entry.get("url", ""))
            summary.update(transport=entry.get("type", "remote"), endpoint_present=bool(parsed.hostname),
                           endpoint_has_query=bool(parsed.query), inline_auth_detected=bool(parsed.username or parsed.password or parsed.query),
                           reason="remote_not_verified")
        services[name] = summary
    return services



def snapshot_fingerprint(root):
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8-sig"))
    files = {name: digest(root / name) for name in SOURCE_FILES}
    sanitized_config = json_digest(audit_mcp(config, root))
    return json_digest({"source_files": files, "safe_config": sanitized_config}), files, sanitized_config
