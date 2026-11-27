"""Public read-only follow-up checks for M12 search suppliers."""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from time import monotonic
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from travel_catalog import public_catalog
from travel_evidence import public_url
from travel_evidence_reader import PublicOriginalReader
from travel_search_mcp import search_anysearch, failure_reason
from travel_state import BEIJING

HISTORICAL = 'https://www.kaiyang.gov.cn/zmhd/rdhy/202508/t20250806_88405688.html'
BOOKING = 'https://fanjingshan.gz.cn/?page_id=1188'
OPENING = 'https://fanjingshan.gz.cn/?p=12007'
METRO = 'https://english.guiyang.gov.cn/2026-07/01/c_1056570.htm'
PRICE = 'https://fgw.guizhou.gov.cn/zwgk/zdlyxx/jffw/jyfwxsfml/202601/P020260205668047355774.pdf'
SEARCHES = [
    ('jiaxiulou', '关于调整避暑季期间甲秀楼（翠微园）等文物景区开放时间的通告 开阳县 贵阳市文化和旅游局', HISTORICAL),
    ('fanjingshan', '梵净山旅游区购票须知 分时预约实名购票 梵净山官方网站', BOOKING),
    ('fanjingshan', '梵净山旅游区 关于梵净山旅游区门票售罄的公告 2026年10月7日', OPENING),
]
ORIGINALS = [
    ('jiaxiulou', HISTORICAL), ('fanjingshan', BOOKING), ('fanjingshan', OPENING),
    ('guiyang-metro-l1', METRO), ('huangguoshu', PRICE),
]


async def search(catalog, subject, query, expected):
    start = monotonic()
    entity = catalog.get(subject)
    try:
        results = await asyncio.wait_for(search_anysearch(query, 5), timeout=18)
    except Exception as error:
        return dict(subject=subject, query=query, status='BLOCKED', reason=failure_reason(error),
                    attempts=1, elapsed_ms=round((monotonic()-start)*1000), cost_micros=None)
    official = []
    for row in results:
        url = public_url(row['url'])
        text = row['title'] + '\n' + row['excerpt']
        if url and urlsplit(url).hostname in entity.authority_hosts and entity.label in text:
            official.append(dict(url=url, title=row['title'][:160], matches_sample=url == public_url(expected)))
    return dict(subject=subject, query=query, status='PASS' if official else 'BLOCKED',
                reason=None if official else 'no_entity_matched_official_lead', attempts=1,
                elapsed_ms=round((monotonic()-start)*1000), cost_micros=None, returned=len(results),
                official_leads=official, facts_confirmed=0)


async def read(catalog, subject, url):
    requests = []

    def before_request():
        requests.append(1)

    reader = PublicOriginalReader(catalog.get(subject).authority_hosts, clock=lambda: datetime.now(BEIJING),
                                  enabled=True, before_request=before_request)
    start = monotonic()
    result = await reader(url)
    return dict(subject=subject, url=url, status=result.status, attempts=len(requests),
                elapsed_ms=round((monotonic()-start)*1000), cost_micros=None,
                text_chars=len(result.data.text) if result.data else 0)


async def run():
    catalog = public_catalog()
    probes = await asyncio.gather(*(search(catalog, *item) for item in SEARCHES))
    originals = await asyncio.gather(*(read(catalog, *item) for item in ORIGINALS))
    return dict(provider='anysearch', checked_at=datetime.now(BEIJING).isoformat(),
                status='PASS' if any(row['status'] == 'PASS' for row in probes) else 'BLOCKED',
                search_probes=probes, original_reader_probes=originals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not args.read_only or os.getenv('ANYSEARCH_AUTH_MODE', '').strip().casefold() == 'key':
        parser.error('the read-only flag and anonymous AnySearch are required')
    report = asyncio.run(run())
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())

