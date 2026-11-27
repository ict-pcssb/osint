"""Read-only M05 smoke, with a per-run gate for authorized free providers."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_catalog import public_catalog
from travel_evidence import EvidenceService
from travel_evidence_card import verify_turn
from travel_evidence_reader import ParallelOriginalReader, PublicOriginalReader
from travel_search_mcp import search_travel_sources

BEIJING = timezone(timedelta(hours=8))


class Trace:
    def __init__(self):
        self.trace_id = "m05-live-"+uuid4().hex
        self.events = []

    def record(self,event,**metadata):
        self.events.append({"event":event,**metadata})


async def run(args):
    directory = public_catalog()
    entity = directory.get(args.subject)
    if not entity or not entity.authority_hosts:
        return {"status":"BLOCKED","attempts":0,"reason":"须选择有已登记原文域的目录实体"}
    clock = lambda:datetime.now(BEIJING)
    trace = Trace()
    reader = (ParallelOriginalReader([link["url"] for link in entity.links],clock=clock)
              if args.parallel_original else PublicOriginalReader(entity.authority_hosts,clock=clock,enabled=True))
    if args.source_host and args.source_host not in entity.authority_hosts:
        return {"status":"BLOCKED","attempts":0,"reason":"显式搜索域未登记在该实体目录"}
    async def official_search(query, subject, *, limit):
        if args.search_terms:
            query = subject + " " + args.search_terms
        if args.source_host:
            query += " site:" + args.source_host
        return await search_travel_sources(query,subject,limit=limit,
                                           source_domains=[args.source_host] if args.source_host else list(entity.authority_hosts))
    service = EvidenceService(directory,clock=clock,reader=reader,search=official_search)
    reply, record = await verify_turn(service,entity.id,args.kind,args.date,direction=args.direction,trace=trace)
    stages = record.get("stages",[])
    discovery = any(row["stage"]=="discover" and row["status"]=="success" for row in stages)
    read = any(row["stage"]=="read" and row["status"]=="success" and row.get("attempted") for row in stages)
    extracted = any(row["stage"]=="extract" and row["status"]=="success" for row in stages)
    applied = any(row["stage"]=="applicability" for row in stages)
    complete = all((discovery,read,extracted,applied)) and reply["result"]["kind"]=="verification"
    return {"status":"PASS" if complete else "BLOCKED","attempts":record.get("tool_calls",0),
            "checks":{"discovery":discovery,"original_read":read,"fact_extracted":extracted,"applicability_checked":applied},
            "claim":record.get("claim"),"evidence":record.get("evidence",[]),"stages":stages,
            "summary_candidates":record.get("summary_candidates",[]),"trace_id":trace.trace_id,
            "reason":"" if complete else "缺发现、正式原文读取、抽取或适用性证据"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-free",action="store_true")
    parser.add_argument("--subject")
    parser.add_argument("--kind",choices=("opening","booking","ticket_price","last_service"),default="opening")
    parser.add_argument("--date")
    parser.add_argument("--direction")
    parser.add_argument("--source-host")
    parser.add_argument("--search-terms")
    parser.add_argument("--parallel-original",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if not args.allow_free:
        result={"status":"BLOCKED","attempts":0,"reason":"缺 M05 真实 Exa/匿名 AnySearch 和公开原文只读许可"}
    elif os.getenv("ANYSEARCH_AUTH_MODE","").strip().lower()=="key":
        result={"status":"BLOCKED","attempts":0,"reason":"当前环境不是匿名 AnySearch 模式"}
    elif not args.subject or not args.date or (args.kind=="last_service" and not args.direction):
        result={"status":"BLOCKED","attempts":0,"reason":"缺目录实体、适用日期或所需线路方向"}
    else:
        try:
            result=asyncio.run(run(args))
        except (ValueError,TimeoutError) as exc:
            result={"status":"FAIL","reason":type(exc).__name__}
    result.update(case_id="M5-10",data_mode="real_provider_read_only")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n","utf-8")
    print(json.dumps(result,ensure_ascii=False))
    return {"PASS":0,"FAIL":1,"BLOCKED":2}[result["status"]]


if __name__=="__main__":
    raise SystemExit(main())

