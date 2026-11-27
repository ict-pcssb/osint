"""Capture the current snapshot, direct dependencies and local tool declarations."""
import argparse
import asyncio
from datetime import datetime
from importlib.metadata import distribution, distributions, PackageNotFoundError
import inspect
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests/travel_agent"), str(ROOT)]
from support.audit import (
    DECLARATIONS, PROMPT, audit_dependencies, audit_mcp, digest, json_digest,
    required_argument_gaps, snapshot_fingerprint,
)
from support.guards import Trace, offline_guard
from support.reporting import classify_evidence, redact

ENVIRONMENT = f"windows-ict-flight-python-{platform.python_version()}"
HANDLERS = (
    ("S1", "travel_plan.py:execute; travel_planner_rules.py:arrange", "M4"),
    ("S2", "travel_skill_api.py:plan", "M2"),
    ("S3", "travel_api.py:rail_day; see standalone flights MCP", "M7"),
    ("S4", "travel_plan.py:budget; travel_constraints.py:quote_lines", "M6"),
    ("S5", "travel_api.py:forecast_day", "M7"),
    ("S6", "travel_plan.py:retrieve; see standalone travel-search MCP", "M5"),
    ("S7", "travel_skill_api.py:weekend; travel_weekend.py:research", "M8"),
    ("S8", "travel_tools.py:route; travel_skill_api.py:osm_tools", "M4"),
    ("S9", "server.py:export_guidebook", "M11"),
    ("E1", "travel_api.py:preview", "M3"),
    ("E2", "旧手册载有选点和报价样例，未见新项目接线", "M8"),
    ("E3", "travel_hotels.py (历史酒店实现)", "M8"),
    ("E4", "travel_skill_api.py:weekend; travel_weekend.py:research", "M8"),
    ("E5", "travel_plan.py:retrieve; notice.lookup 待绑定", "M5"),
    ("E6", "travel_plan.py:budget; ticket.quote 待绑定", "M6"),
    ("E7", "新旧源码均未核得该完整流程", "M10"),
)


def git_value(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if not result.returncode else None


def runtime_dependencies():
    installed = {}
    for name in ("mcp", "httpx2", "pydantic", "flights-mcp", "tzdata"):
        try:
            package = distribution(name)
        except PackageNotFoundError:
            continue
        installed[name] = {"version": package.version}
    if "flights-mcp" in installed:
        package = distribution("flights-mcp")
        origin = json.loads(package.read_text("direct_url.json") or "{}")
        commit = origin.get("vcs_info", {}).get("commit_id")
        ref = urlsplit(origin.get("url", ""))
        if not commit and ref.scheme == "file" and not ref.netloc:
            source = Path(unquote(ref.path.lstrip("/") if sys.platform == "win32" else ref.path))
            tracked = sorted(package.locate_file("flights_mcp").glob("*.py"))
            source_root = next((loc for loc in (source / "flights_mcp", source / "src/flights_mcp")
                                if (loc / "__init__.py").is_file()), None)
            if source_root is not None and (source / ".git").exists() and tracked:
                commit_candidate = git_value(source, "rev-parse", "HEAD")
                matches = sum(
                    (source_root / entry.name).is_file()
                    and entry.read_bytes() == (source_root / entry.name).read_bytes()
                    for entry in tracked
                )
                clean = git_value(source, "status", "--porcelain", "--untracked-files=no") == ""
                if clean and matches == len(tracked):
                    commit = commit_candidate
                    installed["flights-mcp"].update(origin_kind="local_checkout",
                                                    matching_python_files=matches)
        installed["flights-mcp"]["commit"] = commit
    return installed


def capability(capability_id, handler, tool, provider, evidence, status, owner, checked_at, *,
               source=None, schema=None, required_gaps=None):
    return {
        "capability_id": capability_id, "handler_path": handler, "actual_tool_name": tool,
        "schema_hash": json_digest(schema) if schema is not None else None, "provider": provider,
        "evidence_level": evidence, "source_reference": source, "checked_at": checked_at,
        "environment_id": ENVIRONMENT, "status": status, "owner_module": owner,
        "required_parameters": sorted(schema.get("required", [])) if schema is not None else None,
        "required_argument_gaps": required_gaps,
    }


async def registered_tools():
    from flights_mcp.server import mcp as flight_server
    from mcp.types import CallToolResult
    import travel_agent
    import travel_search_mcp

    weather_schema = travel_agent.WEATHER["function"]["parameters"]
    tools = {
        "get_destination_weather": (weather_schema, inspect.signature(travel_agent.weather).parameters),
    }
    signatures = {
        "search_travel_sources": inspect.signature(travel_search_mcp.search_travel_sources).parameters,
    }
    from flights_mcp.server import resolve_airports_tool, search_flights
    signatures.update(
        search_flights=inspect.signature(search_flights).parameters,
        resolve_airports=inspect.signature(resolve_airports_tool).parameters,
    )
    for server in (travel_search_mcp.server, flight_server):
        for tool in await server.list_tools():
            item = tool.model_dump(by_alias=True)
            tools[item["name"]] = (item["inputSchema"], signatures[item["name"]])
    return tools, sorted(CallToolResult.model_fields), travel_agent.MODEL


def evidence_records():
    historical = "开发手册/MCP接口调用手册.md"
    return [
        {"evidence_level": "registration", "scope": "current", "status": "PASS",
         "source_reference": ".mcp.json + local declarations", "coverage": "进程内静态登记"},
        {"evidence_level": "handshake", "scope": "historical", "status": "BLOCKED",
         "source_reference": historical + " §Parallel", "coverage": "手册记载握手，未附协议原始日志"},
        {"evidence_level": "business_data", "scope": "historical", "status": "PASS",
         "source_reference": "测试记录/全量旅行接口复测-2026-11-25.md + 测试记录/Skills测试结果-2026-11-26.json",
         "coverage": "既有样本的保存观察记录，当前轮无供应商取数"},
        {"evidence_level": "legacy_project", "scope": "historical", "status": "PASS",
         "source_reference": "测试记录/Skills测试结果-2026-11-26.json",
         "coverage": "历史回归与固定回放原有报告"},
        {"evidence_level": "new_agent", "scope": "historical", "status": "BLOCKED",
         "source_reference": "测试记录/青岩两轮实测记录.txt",
         "coverage": "已有 CLI 追问和一次天气工具消息，缺 trace_id、页面和完整验收断言"},
        {"evidence_level": "new_agent", "scope": "current", "status": "BLOCKED",
         "coverage": "未取得当前聊天到工具、校验和 UI 的完整链路证据"},
        {"evidence_level": "target_environment", "scope": "current", "status": "BLOCKED",
         "coverage": "当前主机仅为本地 M1 测试，目标环境未指定和验收"},
    ]


LOCK_NAME = "requirements-lock-windows-py313.json"


def installed_lock(runtime):
    packages = {
        re.sub(r"[-_.]+", "-", package.metadata["Name"]).lower(): package.version
        for package in distributions()
    }
    return {
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "packages": dict(sorted(packages.items())),
        "flight_source_commit": runtime.get("flights-mcp", {}).get("commit"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    checked = datetime.now().astimezone().isoformat(timespec="seconds")
    cfg = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8-sig"))
    services = audit_mcp(cfg, ROOT)
    runtime = runtime_dependencies()
    lock_path = ROOT / LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {}
    pins = dict(lock.get("packages", {}))
    if lock.get("flight_source_commit"):
        pins["flights-mcp"] = lock["flight_source_commit"]
    deps = {name: audit_dependencies((ROOT / name).read_text(encoding="utf-8-sig"),
                                    runtime, pins, ENVIRONMENT) for name in DECLARATIONS}
    trace = Trace("m01-static-declarations")
    with offline_guard(trace):
        tools, mcp_response_fields, model = asyncio.run(registered_tools())
    capabilities = []
    registrations = (
        ("S5", "travel_agent.py:weather / Conversation.ask", "get_destination_weather", "Open-Meteo", "M7"),
        ("S6.search", "travel_search_mcp.py:search_travel_sources", "search_travel_sources", "Exa / AnySearch", "M5"),
        ("S3.flights", "flights_mcp/server.py:search_flights", "search_flights", "Google Flights", "M7"),
        ("S3.airports", "flights_mcp/server.py:resolve_airports_tool", "resolve_airports", "flights-mcp airport catalog", "M7"),
    )
    for capability_id, handler, name, provider, owner in registrations:
        schema, mapping = tools[name]
        capabilities.append(capability(capability_id, handler, name, provider, "registration",
                "registered_in_agent_source" if capability_id == "S5" else "registered_standalone",
                owner, checked, source=handler, schema=schema, required_gaps=required_argument_gaps(schema, mapping)))
    for capability_id, source, owner in HANDLERS:
        if capability_id != "S5":
            capabilities.append(capability(capability_id, None, None, None, "legacy_reference",
                "unbound", owner, checked, source=f"samples-1124/{source}"))
    for capability_id, tool, provider, owner in (
        ("S6.parallel", "web_search / web_fetch", "Parallel", "M5"),
        ("E5.notice", "notice.lookup", None, "M5"),
        ("E6.ticket", "ticket.quote", None, "M6"),
    ):
        capabilities.append(capability(capability_id, None, None, provider,
            "registration" if provider else "unverified",
            "registered_remote_only" if provider else "unbound",
            owner, checked, source=f"开发手册/MCP接口调用手册.md ({tool})"))
    snapshot_sha, files, configuration_sha = snapshot_fingerprint(ROOT)
    records = evidence_records()
    git_root = git_value(ROOT, "rev-parse", "--show-toplevel")
    is_repo = git_root is not None and Path(git_root).resolve() == ROOT
    env_names = []
    if (ROOT / ".env").is_file():
        with (ROOT / ".env").open(encoding="utf-8-sig") as inputs:
            for line in inputs:
                name = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=", line)
                if name:
                    env_names.append(name[1])
    lock_passed = (
        installed_lock(runtime) == lock
        and bool(lock.get("flight_source_commit"))
        and all(row["status"] == "PASS" for rows in deps.values() for row in rows)
    )
    lock_status = "PASS" if lock_passed else "BLOCKED"
    report = {
        "schema_version": "1.0", "checked_at": checked, "environment_id": ENVIRONMENT,
        "repository_root": str(ROOT), "source_origin": "用户提供的本地源码快照",
        "branch": git_value(ROOT, "rev-parse", "--abbrev-ref", "HEAD") if is_repo else None,
        "commit": git_value(ROOT, "rev-parse", "HEAD") if is_repo else None,
        "uncommitted_state": "see git status" if is_repo else "unavailable_without_git",
        "snapshot_sha256": snapshot_sha,
        "source_sha256": files, "sanitized_config_sha256": configuration_sha,
        "model_id": model, "model_env_names": ["DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL"],
        "env_file_present": (ROOT / ".env").is_file(), "dotenv_variable_names": sorted(env_names),
        "dependency_lock_files": [LOCK_NAME] if lock else [],
        "dependency_lock_status": lock_status,
        "dependency_lock_sha256": digest(lock_path) if lock else None,
        "locked_package_count": len(lock.get("packages", {})),
        "dependencies": deps, "runtime": runtime,
        "interpreter": sys.executable, "mcp_services": services, "mcp_response_fields": mcp_response_fields,
        "versions": {"prompt_sha256": files[PROMPT], "search_adapter_sha256": files["travel_search_mcp.py"],
                     "skill_registry": None, "domain_rules": None, "application_schema": None,
                     "poi_reference_sha256": digest(ROOT.parent / "samples-1124/static/travel/data/pois.json"),
                     "m01_contract": "1.0"},
        "evidence_records": records, "evidence_summary": classify_evidence(records),
        "capabilities": capabilities,
        "capability_manifest_status": lock_status,
        "reason": "仅核对本地依赖和登记，目标环境及新 Agent 联通另行验收",
        "network_attempts": trace.events,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(redact(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"baseline manifest: static registrations recorded; local dependency lock {lock_status}")
    return 0 if lock_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

