"""Render the M05 fixture through the existing Conversation and result card."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from preview_m02 import chrome_run
from travel_catalog import Catalog
from travel_contract import REPLY
from travel_evidence import EvidenceService, Material, ToolResult
from travel_evidence_card import verify_turn

NOW = "2026-11-26T10:00:00+08:00"


async def fixture():
    data = json.loads((ROOT/"tests/travel_agent/fixtures/m05/sources.json").read_text("utf-8"))
    url = "https://operator.example/notice"
    async def read_original(original_url):
        if original_url != url:
            return ToolResult("invalid_response")
        return ToolResult("success",Material(url,data["texts"]["future"],NOW,"fixture",published_at="2026-11-26"))
    svc=EvidenceService(Catalog(data["entities"]),clock=lambda:datetime.fromisoformat(NOW),reader=read_original)
    return await verify_turn(svc,"hgs-east","opening","2026-11-28",known_urls=(url,),fixture_mode=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    path=args.output.resolve()
    path.mkdir(parents=True,exist_ok=True)
    packet, audit=asyncio.run(fixture())
    css=(ROOT/"frontend/travel_result.css").read_text("utf-8")
    script=(ROOT/"frontend/travel_result.js").read_text("utf-8")
    payload=json.dumps([packet,REPLY.schema],ensure_ascii=False).replace("<","\\u003c")
    page=path/"verification.html"
    page.write_text("""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'">
<title>M5 核验卡</title><style>"""+css+"""</style></head>
<body><main><h1>目标日开放核验</h1><section id="card"></section></main><script>"""+script+
                    "\nconst [packet,schema]="+payload+""";
const card=renderResult(packet,schema);
document.querySelector("#card").append(card);
card.querySelector("details").open=true;
</script></body></html>""",encoding="utf-8")
    (path/"fixture-audit.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2)+"\n","utf-8")
    results={}
    with TemporaryDirectory(prefix="travel-m05-chrome-") as temp:
        profile=Path(temp)/"profile"
        dom=chrome_run(args.browser,profile,page.as_uri(),"--dump-dom","--window-size=1100,900").stdout.decode("utf-8")
        rendered=dom.split("<script>",1)[0]
        results.update(card='data-kind="verification"' in rendered, pending="材料不足以确认" in rendered,
                       booking="官方预约入口" in rendered, effective="适用日期 2026-12-01" in rendered,
                       published="发布时间 2026-11-26" in rendered)
        png=path/"verification.png"
        chrome_run(args.browser,profile,page.as_uri(),"--window-size=1100,900",f"--screenshot={png}")
        results["screenshot"]=png.is_file() and png.stat().st_size>1000
    (path/"report.json").write_text(json.dumps({"status":"PASS" if all(results.values()) else "FAIL",
      "data_mode":"fixture","checks":results},indent=2)+"\n","utf-8")
    print(json.dumps(results))
    return 0 if all(results.values()) else 1


if __name__=="__main__":
    raise SystemExit(main())

