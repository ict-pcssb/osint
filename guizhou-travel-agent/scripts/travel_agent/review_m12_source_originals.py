"""Read registered originals for a separate review of the 20 fixed M12 source questions."""
import argparse
import asyncio
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from travel_catalog import public_catalog
from travel_evidence import EvidenceService, ToolResult, public_url
from travel_evidence_reader import ParallelOriginalReader, PublicOriginalReader
from travel_state import BEIJING

DATASET = ROOT/"tests/travel_agent/fixtures/live_smoke/source_gold.json"
REFERENCES = {
    "heritage_schedule": ("jiaxiulou",
        "https://www.kaiyang.gov.cn/zmhd/rdhy/202508/t20250806_88405688.html",
        ("甲秀楼", "2025年7月21日", "避暑季", "18:00")),
    "booking_policy": ("fanjingshan", "https://fanjingshan.gz.cn/?page_id=1188", ("梵净山", "实名", "分时")),
    "park_home": ("fanjingshan", "https://fanjingshan.gz.cn/", ("东山门", "西山门", "15:00")),
    "park_october_update": ("fanjingshan", "https://fanjingshan.gz.cn/?p=12007", ("10月8日", "入园", "15")),
    "railway_line": ("guiyang-metro-l1", "https://english.guiyang.gov.cn/2026-07/01/c_1056570.htm",
        ("Metro Line 1", "Dougong Station", "Xiaomeng Industrial Park")),
    "provincial_price_pdf": ("huangguoshu",
        "https://fgw.guizhou.gov.cn/zwgk/zdlyxx/jffw/jyfwxsfml/202601/P020260205668047355774.pdf",
        ("黄果树", "160", "150")),
}
HISTORICAL_CORROBORATION = "https://finance.sina.com.cn/jjxw/2025-10-11/doc-inftmwzc5636231.shtml"
OPERATOR_REPRINT = "https://www.gzstv.com/a/ff915100bd124a0fa077f928ffdce550"


def review_rows(cases, readable):
    results = []
    for case in cases:
        item = {key:case[key] for key in ("case_id","subject","claim","target_date","direction")}
        item["formal_gold"] = False
        item["proposed_value"] = None
        if case["subject"] == "jiaxiulou":
            item["reference_ids"] = ["heritage_schedule"]
            if "2025-07-21" <= case["target_date"] <= "2025-10-08" and readable.get("heritage_schedule"):
                item["review_state"] = "ready_for_human_check"
                item["proposed_value"] = "published_opening_hours"
                item["review_note"] = "日期位于文旅局公布的2025年避暑季内，文保中心消息给出10月8日末日。"
                monday = date.fromisoformat(case["target_date"]).weekday() == 0
                item["published_hours"] = ["14:00" if monday else "09:00", "18:00"]
                item["corroboration"] = HISTORICAL_CORROBORATION
            else:
                item.update(review_state="target_date_unverified",
                            review_note="现有通告适用于2025年避暑季，仍需对应日期的公告。")
        elif case["subject"] == "fanjingshan" and case["claim"] == "booking":
            item.update(reference_ids=["booking_policy"], review_state="standing_rule_without_target_period",
                        review_note="官网可确认实名分时预约的常设说明，正文未明确覆盖题目日期。")
        elif case["subject"] == "fanjingshan":
            item.update(reference_ids=["park_home","park_october_update"], review_state="entrance_and_period_unverified",
                        review_note="运营方首页与10月通知出现入园起始时间差异，仍需覆盖目标日的东、西门口径。")
        elif case["subject"] == "guiyang-metro-l1":
            item.update(reference_ids=["railway_line"], review_state="target_timetable_missing",
                        review_note="政府页面可核实线路与端点，仍缺具体车站、方向和目标日的运营方时刻表。",
                        corroboration=OPERATOR_REPRINT)
        else:
            item.update(reference_ids=["provincial_price_pdf"], review_state="season_boundaries_unverified",
                        review_note="价目表有旺淡季两档，需景区官方文件绑定题目中的跨季日期。")
        results.append(item)
    return results


async def read_one(catalog, reference_id, ref, clock):
    subject, url, anchors = ref
    hosts = catalog.get(subject).authority_hosts
    if not public_url(url) or urlsplit(url).hostname not in hosts:
        raise ValueError("original_url_outside_catalog")
    attempts = []

    def before_request():
        attempts.append("public_original")

    start = monotonic()
    direct = await PublicOriginalReader(hosts, clock=clock, enabled=True, before_request=before_request)(url)
    result = direct
    if direct.status != "success":
        attempts.append("parallel")
        result = await ParallelOriginalReader([url], clock=clock)(url)
    material = result.data if result.status == "success" and result.data is not None and public_url(result.data.url) == public_url(url) else None
    body = material.text if material else ""
    compact = "".join(body.split())
    matched = [anchor for anchor in anchors if anchor in body or "".join(anchor.split()) in compact]
    row = dict(reference_id=reference_id, subject=subject, url=url, direct_status=direct.status,
               read_status=result.status, provider=result.provider, attempts=attempts,
               elapsed_ms=round((monotonic()-start)*1000), cost_micros=None, body_characters=len(body),
               body_sha256=sha256(body.encode()).hexdigest() if body else None,
               required_terms=list(anchors), matched_terms=matched, public_body_verified=bool(body and len(matched)==len(anchors)),
               collected_at=material.retrieved_at if material else None)
    return row, result if material else None


async def run(data):
    catalog = public_catalog()
    clock = lambda: datetime.now(BEIJING)
    checks, originals = {}, {}
    for ref_id, config in REFERENCES.items():
        row, content = await read_one(catalog, ref_id, config, clock)
        checks[ref_id] = row
        if content is not None:
            originals[public_url(row["url"])] = content

    rows = review_rows(data["cases"], {key: row["public_body_verified"] for key,row in checks.items()})

    async def reader(url):
        return originals.get(public_url(url), ToolResult("empty"))

    verifier = EvidenceService(catalog, clock=clock, reader=reader)
    for row in rows:
        urls = [checks[key]["url"] for key in row["reference_ids"]]
        case = await verifier.verify_claim(row["subject"], row["claim"], row["target_date"],
                                          line_direction=row["direction"], known_urls=urls)
        row.update(extractor_status=case["claim"]["status"], extractor_value=case["claim"]["value"],
                   linked_evidence=case["claim"]["evidence_ids"], original_urls=urls,
                   extracted_nonempty_excerpts=sum(bool(item["excerpt"]) for item in case["evidence"]))

    return dict(layer="real_exact_original_review", status="BLOCKED", run_at=clock().isoformat(),
                dataset_sha256=sha256(DATASET.read_bytes()).hexdigest(), unchanged_questions=len(rows),
                formal_gold_executed=0, drafts_ready=sum(row["review_state"]=="ready_for_human_check" for row in rows),
                originals_read=sum(bool(row["body_characters"]) for row in checks.values()),
                originals_with_expected_terms=sum(row["public_body_verified"] for row in checks.values()),
                originals=list(checks.values()), cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    result = asyncio.run(run(data))
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in {"cases","originals"}}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

