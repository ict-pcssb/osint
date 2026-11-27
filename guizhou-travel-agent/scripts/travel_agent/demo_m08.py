"""Local M8 browser fixture with authenticated real HTTP and temporary SQLite."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_agent import Conversation
from travel_candidate_context import ChoiceError
from travel_candidates import CandidateSkills, FoodAdapter, HotelAdapter, TaxiAdapter
from travel_contract import REPLY
from travel_state_app import AuthRequired, TravelApplication
from smoke_m08 import count

HTML="""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>打车、住宿和餐饮候选</title><link rel="stylesheet" href="/result.css">
<script defer src="/result.js"></script><script defer src="/candidate.js"></script>{checks}</head>
<body><main class="chat"><header><h1>打车、住宿和餐饮候选</h1><p>本页使用模拟的候车区、费用和餐馆。</p></header>
<form id="candidate-form"><label for="candidate-query">想查什么</label>
<select id="candidate-query"><option value="taxi">贵阳北站到青岩的打车估价</option>
<option value="hotel">贵阳两人两晚的住宿</option>
<option value="dining">青岩附近晚餐</option>
<option value="hotel_empty">住宿查询无结果</option></select><button type="submit">查询</button></form>
<section id="result" aria-live="polite"></section></main></body></html>"""
CLIENT="""let candidatePacket=null, candidateSchema=null;
const form=document.querySelector("#candidate-form"), result=document.querySelector("#result");
async function callApi(path,data){
  const r=await fetch(path,data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  const body=await r.json();
  if(!r.ok) throw new Error(body.error);
  return body;
}
function show(value){
  candidatePacket=value;
  result.replaceChildren(renderResult(value,candidateSchema,{
    pick:async (context_id,choice_id)=>{
      const buttons=Array.from(result.querySelectorAll("button"));
      for(const button of buttons) button.disabled=true;
      try{show(await callApi("/api/choose-candidate",{context_id,choice_id}));}
      catch(error){result.append(element("p",error.message));for(const button of buttons)button.disabled=false;}
    }
  }));
}
form.addEventListener("submit",async event=>{
  event.preventDefault();
  const button=form.querySelector("button");
  button.disabled=true;
  try{
    candidateSchema ||= await callApi("/schema");
    show(await callApi("/api/ask",{scenario:document.querySelector("#candidate-query").value}));
  }catch(error){result.replaceChildren(element("p",error.message));}
  finally{button.disabled=false;}
});"""


def fixture_app(path):
    fixed=json.loads((ROOT/"tests/travel_agent/fixtures/m08/candidates.json").read_text(encoding="utf-8"))
    now=datetime.fromisoformat(fixed["clock"])
    clock=lambda:now
    calls={"taxi":0}
    async def taxi(_,args):
        calls["taxi"]+=1
        return fixed["quote"] if args["businessData"] else fixed["point"]
    async def hotel(tool,_):
        return fixed["hotels"] if tool=="searchHotels" else fixed["rate"]
    async def empty(*_):
        return {"success":True,"hotelInformationList":[]}
    async def food(*_):
        return fixed["foods"]
    skills=CandidateSkills(clock,mode="fixture",taxi=TaxiAdapter(taxi,clock,mode="fixture"),
                           hotel=HotelAdapter(hotel,clock,mode="fixture"),
                           food=FoodAdapter(food,None,clock,mode="fixture"))
    app=TravelApplication(path,clock=clock,candidate_skills=skills)
    app.create_account("m08_demo","local-fixture-password")
    token=app.login("m08_demo","local-fixture-password")
    session=app.open_session(token)
    return app,token,session,calls,empty


@contextmanager
def local_page(app,token,session,calls,empty,*,testing):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def send(self,status,data,mime="application/json",*,cookie=False):
            self.send_response(status)
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Type",mime+"; charset=utf-8")
            self.send_header("Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie",f"m08={token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def packet(self,value,status=200):
            self.send(status,json.dumps(value,ensure_ascii=False).encode("utf-8"))
        def local(self):
            port=self.server.server_port
            origin=f"http://127.0.0.1:{port}"
            return self.headers.get("Host")==f"127.0.0.1:{port}" and self.headers.get("Origin",origin)==origin
        def authenticated(self):
            item=SimpleCookie(self.headers.get("Cookie","")).get("m08")
            return self.local() and item and item.value==token
        def do_GET(self):
            if not self.local():
                return self.packet({"error":"仅限本机访问"},403)
            if self.path=="/":
                markup=HTML.format(checks='<script defer src="/checks.js"></script>' if testing else "")
                self.send(200,markup.encode("utf-8"),"text/html",cookie=True)
            elif self.path=="/schema":
                self.packet(REPLY.schema)
            elif self.path=="/candidate.js":
                self.send(200,CLIENT.encode("utf-8"),"text/javascript")
            elif self.path in {"/result.js","/result.css"}:
                ext=self.path.rsplit(".",1)[-1]
                self.send(200,(ROOT/f"frontend/travel_result.{ext}").read_bytes(),"text/"+("css" if ext=="css" else "javascript"))
            elif testing and self.path=="/checks.js":
                self.send(200,(ROOT/"tests/travel_agent/m08/browser_checks.js").read_bytes(),"text/javascript")
            elif testing and self.path=="/api/count" and self.authenticated():
                self.packet({**calls,"writes":list(count(app).values())})
            else:
                self.packet({"error":"入口不可用"},404)
        def do_POST(self):
            if not self.authenticated():
                return self.packet({"error":"请在本机页面重新打开会话"},403)
            try:
                size=int(self.headers.get("Content-Length",0))
                if not 0<size<1000 or self.headers.get_content_type()!="application/json":
                    raise ValueError
                data=json.loads(self.rfile.read(size))
                if self.path=="/api/choose-candidate" and set(data)=={"context_id","choice_id"}:
                    packet=asyncio.run(app.choose_candidate(token,session,**data))
                elif self.path=="/api/ask" and set(data)=={"scenario"}:
                    scenarios={
                        "taxi":dict(candidate_kind="taxi",from_id="guiyang-north",to_id="qingyan-north-parking",vehicle_class="经济"),
                        "hotel":dict(candidate_kind="hotel",place_id="guiyang",check_in="2026-11-28",nights=2,adults=2,rooms=1),
                        "dining":dict(candidate_kind="dining",place_id="qingyan",meal_at="2026-11-28T19:00:00+08:00")}
                    scenario=data["scenario"]
                    fields=scenarios["hotel" if scenario=="hotel_empty" else scenario]
                    async def model(*_):
                        return json.dumps(dict(intent="candidates",confidence_band="high",requested_outputs=["candidates"],parameters=fields))
                    if scenario=="hotel_empty":
                        skills=CandidateSkills(app.clock,mode="fixture",hotel=HotelAdapter(empty,app.clock,mode="fixture"))
                        agent=Conversation(typed_output=True,clock=app.clock,intent_model=model,fixture_mode=True)
                        packet=asyncio.run(agent.ask("查询候选？",candidate_skills=skills))
                    else:
                        packet=asyncio.run(app.turn(token,"查询候选？",session_id=session,
                                      conversation_factory=lambda:Conversation(typed_output=True,clock=app.clock,
                                                                                intent_model=model,fixture_mode=True)))
                else:
                    return self.packet({"error":"入口不可用"},404)
                self.packet(packet)
            except ChoiceError as exc:
                self.packet({"error":str(exc)},exc.status)
            except AuthRequired:
                self.packet({"error":"会话已失效"},401)
            except (ValueError,KeyError,TypeError):
                self.packet({"error":"请输入有效的查询或候选点"},400)

    with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",type=Path)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.browser and not args.output:
        parser.error("--browser 需要 --output")
    with TemporaryDirectory(prefix="travel-m08-browser-") as folder:
        work=Path(folder)
        app,token,session,calls,empty=fixture_app(work/"travel_m08.sqlite3")
        with local_page(app,token,session,calls,empty,testing=args.browser is not None) as url:
            print(url,flush=True)
            if args.browser is None:
                try:
                    input("回车结束本机演示。")
                except (EOFError,KeyboardInterrupt):
                    pass
                return 0
            output=args.output.resolve()
            output.mkdir(parents=True,exist_ok=True)
            cmd=["node",str(ROOT/"tests/travel_agent/m08/browser_runner.mjs"),str(args.browser),url,
                 str(work/"chrome-profile"),str(output)]
            done=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",timeout=50,check=False)
            print(done.stdout+done.stderr)
            report=json.loads(done.stdout) if done.stdout.strip() else {"status":"FAIL","error":"browser_result_missing"}
            report.update(data_mode="fixture",layer="chrome_local_http_sqlite",provider_calls=calls,writes=count(app))
            (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            return done.returncode


if __name__=="__main__":
    raise SystemExit(main())

