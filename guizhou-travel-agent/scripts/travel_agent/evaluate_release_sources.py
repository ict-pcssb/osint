"""Evaluate source discovery separately from the model, limiting actual provider attempts."""
import argparse
import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_catalog import public_catalog
from travel_evidence import EvidenceService, public_url
from travel_evidence_reader import PublicOriginalReader
from travel_release import Measure, RunLimit, RunTrace
from travel_search_mcp import BEFORE_SEARCH_ATTEMPT, search_travel_sources
from travel_state import BEIJING

DATA=ROOT/"tests/travel_agent/fixtures/live_smoke/source_gold.json"
CALIBRATION=("S01","S05","S06","S11")


async def sources(case,catalog):
    today=lambda:datetime.now(BEIJING)
    entity=catalog.get(case["subject"])
    trace=RunTrace()
    started=monotonic()
    if not entity or not entity.authority_hosts:
        return dict(case_id=case["case_id"],status="BLOCKED",reason="no_registered_authority",trace=[],attempts=0,
                    elapsed_ms=0,cost_micros=None,family="来源核验")
    deadline=started+29
    attempts=[]

    def take(name):
        if monotonic()>=deadline or len(attempts)>=4:
            raise RunLimit("bounded_source_attempts")
        attempts.append(name)
        trace.record("source_attempt",provider=name,attempt=len(attempts))

    async def search(query,subject,*,limit):
        return await search_travel_sources(query,subject,source_domains=list(entity.authority_hosts),limit=limit)

    reader=PublicOriginalReader(entity.authority_hosts,clock=today,enabled=True,
                                before_request=lambda:take("public-original"))
    service=EvidenceService(catalog,clock=today,reader=reader,search=search,timeout=25,max_reads=2)
    marker=BEFORE_SEARCH_ATTEMPT.set(take)
    try:
        raw=await asyncio.wait_for(service.verify_claim(case["subject"],case["claim"],case["target_date"],
                              line_direction=case["direction"],trace=trace),timeout=28)
    except (TimeoutError,RunLimit,OSError,ValueError) as exc:
        raw=None
        cause=type(exc).__name__
    finally:
        BEFORE_SEARCH_ATTEMPT.reset(marker)
    elapsed=round((monotonic()-started)*1000)
    if raw is None:
        return dict(case_id=case["case_id"],status="BLOCKED",reason=cause,trace=trace.events,
                    attempts=len(attempts),provider_calls=attempts,elapsed_ms=elapsed,cost_micros=None,family="来源核验")
    source_url=public_url(case["original_url"]) if case["original_url"] else None
    found=bool(source_url and any(public_url(item["url"])==source_url for item in raw["summary_candidates"]))
    originals=raw["evidence"]
    reads=[row for row in originals if row["data_mode"]=="live" and row["excerpt"]]
    applicable=raw["claim"]["value"] is not None
    same=applicable and raw["claim"]["value"]==case["expected_value"] and any(
        public_url(row["source_url"])==source_url and row["evidence_id"] in raw["claim"]["evidence_ids"] for row in reads)
    gold=case["eligibility"]=="preverified" and bool(source_url) and case["expected_value"] is not None
    result="PASS" if gold and found and same else "FAIL" if gold else "BLOCKED"
    return dict(case_id=case["case_id"],status=result,case_is_approved_gold=gold,found_gold=found,
                read_gold_fact=same,original_count=len(originals),nonempty_excerpts=len(reads),
                result_status=raw["claim"]["status"],applicable=applicable,
                actual_candidate_count=len(raw["summary_candidates"]),provider_calls=attempts,
                attempts=len(attempts),elapsed_ms=elapsed,cost_micros=None,family="来源核验",trace_id=trace.trace_id,
                trace=trace.events,reason=None if gold else "candidate_only_not_gold")


async def batch(cases):
    catalog = public_catalog()
    rows = []
    for start in range(0, len(cases), 3):
        rows.extend(await asyncio.gather(*(sources(case, catalog) for case in cases[start:start+3])))
    return rows


def select_cases(cases, mode, enabled):
    approved = [case for case in cases if case["eligibility"] == "preverified"
                and public_url(case["original_url"]) and case["expected_value"] is not None]
    if not enabled:
        return approved, []
    if mode == "calibration":
        return approved, [case for case in cases if case["case_id"] in CALIBRATION]
    if mode == "candidates":
        return approved, [case for case in cases if case["eligibility"] == "candidate"]
    return approved, approved


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only",action="store_true")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--calibrate", action="store_true")
    mode_group.add_argument("--candidate-diagnostics", action="store_true")
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    data=json.loads(DATA.read_text(encoding="utf-8"))
    cases=data["cases"]
    blockers=[]
    if not args.read_only:
        blockers.append("requires_read_only_flag")
    if os.getenv("ANYSEARCH_AUTH_MODE","").strip().lower()=="key":
        blockers.append("calibration_requires_anonymous_anysearch")
    mode = "calibration" if args.calibrate else "candidates" if args.candidate_diagnostics else "gold"
    approved, selected = select_cases(cases, mode, not blockers)
    if len(approved) != data["required_gold_count"]:
        blockers.append("authoritative_gold_set_not_verified")
    rows=asyncio.run(batch(selected))
    gold=[row for row in rows if row.get("case_is_approved_gold")]
    found=sum(row["found_gold"] for row in gold)
    passed=sum(row["read_gold_fact"] for row in gold)
    errors=sum(row["applicable"] and not row["read_gold_fact"] for row in gold)
    status="BLOCKED" if blockers or mode != "gold" else "PASS" if found>=18 and passed>=18 and errors==0 and len(gold)==20 else "FAIL"
    report=dict(status=status,layer=f"real_source_{mode}",
                gold_required=data["required_gold_count"],gold_verified=len(approved),gold_executed=len(gold),
                dataset_sha256=sha256(DATA.read_bytes()).hexdigest(),executed=len(rows),found_gold=found,
                extracted_gold=passed,false_confirmations=errors,blockers=blockers,stats=Measure.summary(rows),cases=rows)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="cases"},ensure_ascii=False))
    return {"PASS":0,"FAIL":1,"BLOCKED":2}[status]


if __name__=="__main__":
    raise SystemExit(main())
