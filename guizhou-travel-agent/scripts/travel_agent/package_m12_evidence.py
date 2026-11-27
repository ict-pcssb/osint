"""Assemble a release evidence index and a source-hash manifest without credentials."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_agent import MODEL
from travel_release import Measure
from travel_routing import SKILLS

STORAGE=ROOT/"docs/travel_agent"
BASE=STORAGE/"evidence/m12"


def read(name):
    return json.loads((BASE/name).read_text(encoding="utf-8"))


def write(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def checksum(p):
    return sha256(p.read_bytes()).hexdigest()


def capture():
    allowed={".py",".js",".mjs",".css",".html",".sql",".json"}
    source=set(ROOT.glob("travel_*.py"))
    for directory in ("frontend","contracts/travel_agent","scripts/travel_agent","tests/travel_agent","data"):
        for p in (ROOT/directory).rglob("*"):
            if p.is_file() and p.suffix in allowed and not any(part.startswith(".") or part=="__pycache__"
                                                               for part in p.relative_to(ROOT).parts):
                source.add(p)
    source.update(ROOT/path for path in ("requirements-lock-windows-py313.json",
                                        "提示词/旅游助手系统提示词.md","test_travel_search_mcp.py"))
    source_index={p.relative_to(ROOT).as_posix():checksum(p) for p in sorted(source)}
    runtime=read("rollback/report.json")
    emit=dict(created_at=datetime.now(timezone.utc).isoformat(),model=MODEL,code_commit=None,
        commit_reason="desktop source snapshot has no .git",
        source_sha256=sha256(json.dumps(source_index,sort_keys=True).encode()).hexdigest(),files=source_index,
        lock_sha256=source_index["requirements-lock-windows-py313.json"],
        prompt_sha256=source_index["提示词/旅游助手系统提示词.md"],
        datasets={name:source_index[name] for name in (
            "tests/travel_agent/fixtures/model_eval/cases.json","tests/travel_agent/fixtures/live_smoke/source_gold.json",
            "tests/travel_agent/fixtures/live_smoke/provider_scope.json")},
        skills={k:{field:getattr(v,field) for field in ("skill_id","tool","version")} for k,v in SKILLS.items()},
        config={"default_search":["exa","anysearch"],"parallel_separate":True,"paid_model_default":False,
                "new_provider_override_without_opt_in":False,"patch_commit":"TRAVEL_M09_COMMIT",
                "open_recheck":"TRAVEL_M10_AUTO_OPEN","pdf_export":"TRAVEL_M11_PDF_BROWSER",
                "typed_output":"Conversation(typed_output=...)",
                "provider_flags":list(runtime["flags"])[:3]},
        target={"os":platform.platform(),"python":platform.python_version(),"sqlite":sqlite3.sqlite_version,
                "browser":"Chrome 158.0.8127.37","isolation":"loopback and disposable SQLite"},
        rollback_archive_sha256=runtime["previous_source_sha256"],production_deployment=False)
    write(STORAGE/"release_manifest.json",emit)
    return emit["source_sha256"]


def summarize():
    fixture=read("fixed/model-shaped-replay.json")
    model=read("model/report.json")
    sources=read("sources/gold-report.json")
    calibration=read("sources/calibration.json")
    anysearch=read("live/anysearch-direct.json")
    providers=read("live/workbench/report.json")["cases"]
    combined=read("live/rail-flight.json")["cases"]
    taxi=read("live/taxi-quote.json")
    dining=read("live/dining.json")
    independent=read("trace/report.json")
    loss=read("trace/response-lost-browser/desktop/report.json")
    rollback=read("rollback/report.json")
    browser=[read(f"e2e/{view}/report.json") for view in ("desktop","narrow","cards")]
    target_pdf=read("e2e/pdf/report.json")
    pdf_pages=read("e2e/pdf/text-and-pages.json")
    history={path.parent.name:json.loads(path.read_text(encoding="utf-8"))["summary"]["counts"]["PASS"]
             for path in (BASE/"regression").glob("M*/report.json")}
    live_rows=[]
    for row in providers:
        live_rows.append(dict(family=row["kind"],elapsed_ms=row["trace"][-1]["elapsed_ms"],
                              attempts=sum(e["event"] in {"m07_provider","m08_provider"} for e in row["trace"]),
                              cost_micros=None))
    assert fixture["status"]=="PASS" and history.get("M12")==6
    assert all(row["status"]=="PASS" for row in browser)
    assert all(row["status"]=="PASS" for row in (independent,loss,rollback,target_pdf,pdf_pages))
    assert any(e["event"]=="m09_http_response_lost" for e in loss["trace"])
    assert any(e["event"]=="m09_operation_status" for e in loss["trace"])
    rows_by_trace={row["trace_id"]:dict(case=row["case"],path="report.json")
                   for row in independent["traces"]}
    timed_out=next(e["trace_id"] for e in loss["trace"] if e["event"]=="m09_http_response_lost")
    rows_by_trace[timed_out]=dict(case="committed_response_lost",path="response-lost-browser/desktop/report.json")
    write(BASE/"trace/index.json",rows_by_trace)
    enabled_statuses=[x["status"] for x in providers+combined]+[taxi["status"],dining["status"],anysearch["status"]]
    provider_gate="FAIL" if "FAIL" in enabled_statuses else "BLOCKED" if "BLOCKED" in enabled_statuses else "PASS"
    statuses={f"M12-{i:02}":status for i,status in enumerate((
        model["status"],model["status"],provider_gate,
        sources["status"],"PASS","PASS","PASS","PASS","PASS","BLOCKED"),1)}
    rows=dict(status="BLOCKED" if "BLOCKED" in statuses.values() else "FAIL" if "FAIL" in statuses.values() else "PASS",
        production_operations=0,module_statuses=statuses,history_by_module=history,
        model_blockers=model["blockers"],source_blockers=sources["blockers"],
        sampled_providers={**{x["kind"]:x["status"] for x in providers},**{x["provider"]:x["status"] for x in combined},
                           "dining":dining["status"],"taxi_quote":taxi["status"],"anysearch_official":anysearch["status"]},
        calibration_count=calibration["executed"],live_business_observed=live_rows,
        live_timing_summary=Measure.summary(live_rows),model_executed=model["summary"]["executed"],
        fixed_summary=fixture["summary"],rollback_ms=rollback["elapsed_ms"],target_pdf_pages=pdf_pages["page_count"])
    rows["source_sha256"]=capture()
    write(BASE/"release-summary.json",rows)
    print(json.dumps(dict(status=rows["status"],matrix=rows["module_statuses"],source_sha256=rows["source_sha256"],
                          regression=sum(history.values()),rollback_ms=rows["rollback_ms"]),ensure_ascii=False))


if __name__=="__main__":
    summarize()
