"""Render M7 packets returned by the typed Agent in a local, offline chat preview."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from preview_m02 import chrome_run
from travel_agent import Conversation
from travel_catalog import Catalog
from travel_contract import REPLY
from travel_transport import RailAdapter, WeatherAdapter
from travel_transport_skills import TransportSkills


async def example_packets():
    data=json.loads((ROOT/"tests/travel_agent/fixtures/m07/transport.json").read_text(encoding="utf-8"))
    catalog=Catalog(data["entities"])
    fixed=datetime.fromisoformat(data["clock"])
    clock=lambda:fixed
    responses={"weather_forecast":data["weather"],"route":data["route"],
               "get-station-code-by-names":data["codes"],"get-tickets":data["trains"]}
    calls=[]
    async def fixture(name,args):
        calls.append(dict(tool=name,arguments=args))
        return responses[name]
    day="2026-11-28"
    schedules={("tongren","fanjing-east",day):{"schedule":data["schedule"],"reservation":data["booking"]}}
    skills=TransportSkills(catalog,clock,mode="fixture",weather=WeatherAdapter(catalog,fixture,clock,mode="fixture"),
                           rail=RailAdapter(catalog,fixture,clock,mode="fixture"),route=fixture,schedules=schedules)
    fixtures=[
        ("明天青岩下雨吗？","weather",{"place_id":"qingyan"}),
        ("铜仁站深夜到站，怎么接驳梵净山东门？","transfer",
         {"from_id":"tongren","to_id":"fanjing-east","date":day,"arrival_at":day+"T23:40:00+08:00"}),
        ("两个人从贵阳北到遵义，可以坐哪班车？","candidates",
         {"candidate_kind":"train","from_id":"guiyang-north","to_id":"zunyi","date":day,"adults":2}),
    ]
    packets={}
    for text,kind,fields in fixtures:
        async def model(*_, k=kind, params=fields):
            return json.dumps(dict(intent=k,confidence_band="high",requested_outputs=[k],parameters=params))
        agent=Conversation(typed_output=True,clock=clock,intent_model=model,fixture_mode=True,transport_skills=skills)
        packet=await agent.ask(text)
        packets[text]=dict(probe=kind,packet=packet)
    return packets,calls


def html(path,packets,*,checks=False,selected=None):
    src=(ROOT/"frontend/travel_result.js").read_text(encoding="utf-8")
    style=(ROOT/"frontend/travel_result.css").read_text(encoding="utf-8")
    assertions=(ROOT/"tests/travel_agent/m07/browser_assertions.js").read_text(encoding="utf-8") if checks else ""
    data=json.dumps(dict(schema=REPLY.schema,samples=packets),ensure_ascii=False).replace("<","\\u003c")
    display=json.dumps(selected,ensure_ascii=False)
    page=f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'">
<title>贵州旅行交通查询示例</title><style>{style}</style></head>
<body><main class="chat"><header><h1>贵州旅行交通查询</h1><p>以下由固定天气、地图和车次数据生成。</p></header>
<section id="chat-prompts" aria-label="示例问题"></section><section id="result" aria-live="polite"></section>
<form id="chat-form"><input id="question" aria-label="输入示例问题"><button type="submit">发送</button></form></main>
<script>
window.m7Network = 0;
window.fetch = () => {{ window.m7Network++; throw new Error("Offline preview"); }};
{src}
const m7={data};
const question=document.querySelector("#question");
const prompts=document.querySelector("#chat-prompts");
const output=document.querySelector("#result");
const form=document.querySelector("#chat-form");
form.addEventListener("submit",event=>{{
  event.preventDefault();
  const sample=m7.samples[question.value.trim()];
  output.replaceChildren(sample ? renderResult(sample.packet,m7.schema) : element("p","请选择一个示例问题。"));
}});
for(const label of Object.keys(m7.samples)){{
   const option=element("button",label); option.type="button";
   option.addEventListener("click",()=>{{ question.value=label; form.requestSubmit(); }});
   prompts.append(option);
}}
const selected={display};
if(selected){{ question.value=selected; form.requestSubmit(); }}
{assertions}
</script></body></html>"""
    path.write_text(page,encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    packets,calls=asyncio.run(example_packets())
    html(output/"chat.html",packets)
    records={}
    with TemporaryDirectory(prefix="travel-m07-browser-") as folder:
        root=Path(folder)
        profile=root/"profile"
        page=root/"checks.html"
        html(page,packets,checks=True)
        for label,width in (("desktop",1280),("mobile",430)):
            result=chrome_run(args.browser,profile,page.as_uri(),"--dump-dom",f"--window-size={width},1020")
            dom=result.stdout.decode("utf-8",errors="replace")
            match=re.search(r'data-m07-status="(PASS|FAIL)"[^>]*data-m07-checks="([^"]+)"',dom)
            records[label]=dict(status=match[1] if match else "FAIL",checks=match[2].split(",") if match else [])
        for query,case in packets.items():
            kind=case["probe"]
            shot=output/f"{kind}.png"
            page=root/f"{kind}.html"
            html(page,packets,selected=query)
            chrome_run(args.browser,profile,page.as_uri(),"--window-size=1100,1070",f"--screenshot={shot}")
            if not shot.is_file() or shot.stat().st_size<1000:
                raise RuntimeError(f"未生成截图 {kind}")
    audit=dict(data_mode="fixture",layer="browser_component",results=records,calls=calls,screenshots=["weather.png","transfer.png","candidates.png"])
    (output/"report.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(records,ensure_ascii=False))
    return 0 if all(record["status"]=="PASS" for record in records.values()) else 1


if __name__=="__main__":
    raise SystemExit(main())

