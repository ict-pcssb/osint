"""Exercise the actual M11 HTTP entry for local PDF or real read-only providers."""
import argparse
import asyncio
from datetime import datetime
from http.cookiejar import CookieJar
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import urlopen, HTTPCookieProcessor, Request, build_opener

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from demo_m11 import LIVE_EXAMPLES, NOW, Workbench, configure_live, make_case, serve
from fixture_m11 import advance
from fixture_m09 import make_trip
from support.guards import isolated_database
from travel_state import BEIJING


def exercise(args):
    if args.live:
        configure_live(args)
    with TemporaryDirectory(prefix="travel-m11-smoke-") as folder:
        work=Path(folder)
        clock=(lambda:datetime.now(BEIJING)) if args.live else lambda:NOW
        with isolated_database(work/"travel_m11.sqlite3",work) as db:
            app,token,other_token,trip=make_case(db,clock=clock)
            # Save r9 by the actual M9 path so the PDF includes a genuinely unknown fee.
            if not args.live:
                trip=advance(app,token,trip)
            ui=Workbench(app,token,trip,live=args.live,testing=False,pdf_browser=args.pdf_browser)
            try:
                with serve(ui) as url:
                    client=build_opener(HTTPCookieProcessor(CookieJar()))
                    base=url.rstrip("/")
                    client.open(url,timeout=60).close()
                    endpoint="/api/trips/"+trip.trip_id

                    def post(path,data):
                        request=Request(base+path,data=json.dumps(data,ensure_ascii=False).encode(),
                                        headers={"Content-Type":"application/json","Origin":base})
                        with client.open(request,timeout=60) as response:
                            return json.load(response)

                    def download(card):
                        with client.open(base+"/api/exports/"+card["artifact_id"]+"?download=1",timeout=60) as response:
                            return response.headers.get("Content-Type"),response.read()

                    if args.live:
                        cases=[]
                        for kind in ("weather","transfer","pickup_choices","candidates"):
                            entry=post("/api/turn",dict(mode=kind,message=LIVE_EXAMPLES[kind],trip_id=None))
                            card=entry["value"]["result"]
                            providers=[row["provider"] for row in entry["trace"] if row.get("provider")]
                            expected=card.get("data_mode")=="live" and card["kind"]==kind
                            if expected and kind=="weather":
                                expected=("open-meteo" in providers and
                                          card["forecast"]["units"]=={"temperature":"°C","probability":"%"} and bool(card.get("as_of")))
                            elif expected and kind=="transfer":
                                expected=("osm" in providers and card["last_service"]["state"]=="unknown"
                                          and any(x["status"]=="estimate" and x["travel_min"]>0 for x in card["segments"]))
                            elif expected and kind=="pickup_choices":
                                expected="amap-taxi" in providers and bool(card["options"])
                            elif expected and kind=="candidates":
                                expected=("rollinggo" in providers and card["candidate_kind"]=="hotel" and bool(card["options"])
                                          and card["booking_status"]=="not_booked")
                            cases.append(dict(kind=kind,status="PASS" if expected else "BLOCKED",trace=entry["trace"],
                                              target_date=card.get("target_date"),choices=len(card.get("options",[])),
                                              response_kind=card["kind"],observation=card.get("forecast"),
                                              reason=card.get("reason"),
                                              evidence_clock=[{k:item.get(k) for k in ("provider","evidence_id","collected_at","applies_to")}
                                                              for item in entry["value"]["evidence"]]))
                        return dict(status="PASS" if all(row["status"]=="PASS" for row in cases) else "BLOCKED",
                                    layer="real_provider_read_only",cases=cases,actual_model=False,
                                    original_unchanged=app.read_trip(token,trip.trip_id)==trip)
                    cases=[]
                    owner_checks=[]
                    if args.verify_owner:
                        other_trip=asyncio.run(make_trip(app,other_token))
                        control=ui.exports.create(other_token,other_trip.trip_id,
                                                  revision=other_trip.revision,format="html")["result"]
                        def probe(card, credential=None):
                            headers={"Cookie":"m11="+credential} if credential else {}
                            request=Request(base+"/api/exports/"+card["artifact_id"]+"?download=1", headers=headers)
                            try:
                                with urlopen(request,timeout=30) as response:
                                    return response.status,response.read()
                            except HTTPError as error:
                                with error:
                                    return error.code,error.read()
                        status,html=probe(control,other_token)
                        owner_checks.append(dict(case="B_downloads_B_HTML",http_status=status,
                                                 passed=status==200 and b"<!doctype html>" in html))
                    for fmt in ("html","pdf"):
                        result=post(endpoint+"/export",dict(revision=trip.revision,format=fmt))["value"]["result"]
                        mime,content=download(result)
                        if args.verify_owner:
                            blocked,body=probe(result,other_token)
                            anonymous,anonymous_body=probe(result)
                            generic=body==json.dumps({"error":"记录不存在"},ensure_ascii=False).encode()
                            owner_checks.append(dict(case="B_cannot_download_A_"+fmt.upper(),http_status=blocked,
                                                      anonymous_status=anonymous,
                                                      passed=blocked==404 and anonymous==401 and generic
                                                      and not anonymous_body.startswith(b"%PDF-")))
                        actual="pdf" if mime=="application/pdf" else "html"
                        if fmt==actual:
                            (args.output/f"guidebook-r{trip.revision}.{fmt}").write_bytes(content)
                        cases.append(dict(requested=fmt,status=result["status"],reason=result["reason"],mime=mime,bytes=len(content)))
                    expected=cases[0]["status"]=="ready" and cases[1]["status"]=="ready" and cases[1]["mime"]=="application/pdf"
                    html_file=args.output/f"guidebook-r{trip.revision}.html"
                    html_content=html_file.read_text(encoding="utf-8") if html_file.is_file() else ""
                    markers=("行程版本 r9","模拟数据","室内展馆","待核实","全团","来源与证据")
                    html_complete=all(mark in html_content for mark in markers)
                    unchanged=app.read_trip(token,trip.trip_id)==trip
                    ok=expected and html_complete and unchanged and all(item["passed"] for item in owner_checks)
                    return dict(status="PASS" if ok else "FAIL" if expected else "BLOCKED",
                                layer="local_chrome_http_export",revision=trip.revision,cases=cases,
                                original_unchanged=unchanged,html_markers=html_complete,owner_checks=owner_checks)
            finally:
                ui.recheck.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--pdf-browser",type=Path)
    parser.add_argument("--weather-entry",type=Path)
    parser.add_argument("--osm-entry",type=Path)
    parser.add_argument("--rail-entry",type=Path)
    parser.add_argument("--verify-owner",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.live and args.verify_owner:
        parser.error("--verify-owner requires the fixed itinerary")
    args.output.mkdir(parents=True,exist_ok=True)
    report=exercise(args)
    (args.output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit({"PASS":0,"FAIL":1,"BLOCKED":2}[report["status"]])
