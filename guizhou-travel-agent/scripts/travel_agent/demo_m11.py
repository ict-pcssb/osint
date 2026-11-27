"""M11 unified local workbench over the existing TravelApplication and Skills."""
import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from getpass import GetPassWarning, getpass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread
from urllib.parse import urlsplit
import warnings

ROOT=Path(__file__).resolve().parents[2]
TEST=ROOT/"tests/travel_agent"
sys.path[:0]=[str(ROOT),str(TEST),str(TEST/"m11")]
from fixture_m11 import make_case, NOW
from support.guards import isolated_database
from travel_release import RunTrace
from travel_radeon import RADEON_KEY_ENV, RadeonIntent
from travel_agent import Conversation, classify_intent
from travel_candidate_context import ChoiceError
from travel_candidates import CandidateSkills, HotelAdapter, TaxiAdapter
from travel_contract import ContractError, REPLY
from travel_guidebook import ChromePdf, ExportError, GuidebookService
from travel_patch import PatchError
from travel_recheck import RecheckError, RecheckService
from travel_routing import SKILLS
from travel_state import BEIJING
from travel_state_app import AuthRequired, Conflict, NotFound
from travel_timeline import TimelineWorkflow, load_fixture
from travel_timeline_route import RouteAdapter

ASSETS={"/result.js":("travel_result.js","text/javascript"),"/result.css":("travel_result.css","text/css"),
        "/workbench.js":("travel_workbench.js","text/javascript"),"/workbench.css":("travel_workbench.css","text/css")}
SAMPLES=json.loads((TEST/"fixtures/m02/replies.json").read_text(encoding="utf-8"))
EXAMPLES={"ack":"我周六准备去青岩。","weather":"明天青岩下雨吗？",
          "verification":"黄果树水帘洞周六开放吗？","transfer":"荔波站到小七孔东门怎么接驳？",
          "cost_breakdown":"两位成人一位儿童去小七孔要花多少钱？","candidates":"比较贵阳两位成人住两晚的酒店",
          "itinerary":"生成贵阳两天演示时间轴","clarification":"我想知道天气","pickup_choices":"贵阳北站到青岩北门停车场怎么打车？",
          "bundle":"明天青岩下雨吗？荔波站到小七孔怎么走？",
          "patch_preview":"第二天改成室内，返程票不变","recheck_report":"复查这份已保存的行程",
          "export_result":"把这份行程导出为 HTML 路书"}
LIVE_EXAMPLES={**EXAMPLES,"transfer":"铜仁站到梵净山东门怎么接驳？"}


def trace_for(name):
    trace=RunTrace()
    trace.record("entry",stage=name)
    return trace


def fixture_candidates(clock):
    fixed=json.loads((TEST/"fixtures/m08/candidates.json").read_text(encoding="utf-8"))
    async def taxi(_,args):
        return fixed["quote"] if args["businessData"] else fixed["point"]
    async def hotel(name,_):
        return fixed["hotels"] if name=="searchHotels" else fixed["rate"]
    return CandidateSkills(clock,mode="fixture",taxi=TaxiAdapter(taxi,clock,mode="fixture"),
                           hotel=HotelAdapter(hotel,clock,mode="fixture"))


def configure_live(args):
    if not args.live:
        return
    for key in ("TRAVEL_M07_NETWORK","TRAVEL_M08_NETWORK","TRAVEL_M08_AMAP","TRAVEL_M08_HOTEL","TRAVEL_M08_FOOD",
                "TRAVEL_M10_NETWORK","TRAVEL_M10_NOTICES","TRAVEL_M10_HOTEL"):
        os.environ[key]="1"
    for name,path in (("WEATHER",args.weather_entry),("OSM",args.osm_entry),("RAIL",args.rail_entry)):
        if path:
            os.environ["TRAVEL_M07_"+name+"_ENTRY"]=str(path.resolve())


class Workbench:
    def __init__(self,app,token,trip,*,live,testing,pdf_browser,model_provider="dashscope"):
        self.app,self.token,self.sample=app,token,trip.trip_id
        self.live,self.testing=live,testing
        self.model_provider = model_provider
        key = os.getenv(RADEON_KEY_ENV if model_provider == "radeon" else "DASHSCOPE_API_KEY", "").strip()
        self.model_on = bool(live and key)
        self.radeon = RadeonIntent(key) if self.model_on and model_provider == "radeon" else None
        self.session=app.open_session(token)
        self.agent_lock=RLock()
        self.current_mode=None
        self.form_fixture=load_fixture()
        mock=self.form_fixture
        for line in mock.costs:
            if line["item_id"]=="d2-ride":
                line["unit_fen"]=None
        self.timeline=TimelineWorkflow(app,mock.catalog,RouteAdapter(mock.route,app.clock,fixture=True),mock.costs)
        self.exports=GuidebookService(app,pdf=ChromePdf(pdf_browser) if pdf_browser else None)
        app.guidebook_service=self.exports
        self.recheck=app.get_recheck_service()
        if not live:
            app.candidate_skills=fixture_candidates(app.clock)
        from travel_transport_live import configured_skills
        self.agent=Conversation(typed_output=True,clock=app.clock,intent_model=self.intent,
                                fixture_mode=True,transport_skills=configured_skills(app.clock) if live else None,
                                verification_handler=self.verify if live else None,
                                intent_timeout=self.radeon.turn_timeout if self.radeon else 15)

    def handlers(self):
        mapping={}
        for name in ("weather","verification","transfer","cost_breakdown","candidates"):
            async def read(_,name=name):
                return deepcopy(SAMPLES[name])
            mapping[SKILLS[name].tool]=read
        return mapping

    async def intent(self,message,now,known,repair):
        kind=self.current_mode
        if self.live and kind=="model":
            if self.radeon:
                return await self.radeon.intent(message,now,known,repair,trace=self.current_trace)
            return await classify_intent(message,now,known,repair)
        mode={"clarification":"weather","bundle":"weather","pickup_choices":"candidates"}.get(kind,kind)
        parameters={}
        if not self.live:
            if kind=="weather":
                parameters={"place_id":"qingyan","date":"2026-11-27"}
            if kind in {"bundle","transfer"}:
                parameters.update(from_id="libo-station",to_id="xiaoqikong-east-gate",date="2026-11-28")
            if kind=="verification":
                parameters["date"]="2026-11-28"
            if kind=="cost_breakdown":
                parameters.update(place_id="qingyan",date="2026-11-28",adults=2,children=1)
            if kind=="candidates":
                parameters.update(candidate_kind="hotel",check_in="2026-11-28",place_id="guiyang",nights=2,rooms=1,adults=2)
        day=(now.date()+timedelta(days=1)).isoformat()
        if self.live and kind=="weather":
            parameters={"place_id":"qingyan","date":day}
        if self.live and kind=="verification":
            parameters={"place_id":"huangguoshu-water-curtain","date":day}
        if self.live and kind=="transfer":
            parameters={"from_id":"tongren","to_id":"fanjing-east","date":day,"mode":"bus"}
        if self.live and kind=="candidates":
            parameters=dict(candidate_kind="hotel",check_in=day,place_id="guiyang",nights=2,rooms=1,adults=2)
        if kind=="pickup_choices":
            parameters.update(candidate_kind="taxi",from_id="guiyang-north",to_id="qingyan-north-parking",vehicle_class="经济")
        if kind=="export_result":
            parameters["format"]="html"
        return json.dumps(dict(intent=mode,confidence_band="high",
                              requested_outputs=["weather","transfer"] if kind=="bundle" else [mode],parameters=parameters))

    async def verify(self,args):
        from travel_catalog import public_catalog
        from travel_evidence import EvidenceService
        from travel_evidence_card import verification_packet
        from travel_evidence_reader import PublicOriginalReader
        from travel_search_mcp import search_travel_sources
        catalog=public_catalog()
        entity=catalog.get(args.get("place_id") or "huangguoshu-water-curtain")
        if not entity or not entity.authority_hosts:
            raise ValueError("请选有登记原文来源的景点")
        async def search(query,subject,*,limit):
            return await search_travel_sources(query,subject,limit=limit,source_domains=list(entity.authority_hosts))
        logic=EvidenceService(catalog,clock=self.app.clock,reader=PublicOriginalReader(entity.authority_hosts,
                              clock=self.app.clock,enabled=True),search=search,timeout=12,max_reads=2)
        report=await logic.verify_claim(entity.id,"opening",args["date"],trace=self.current_trace)
        return verification_packet(report,entity,fixture_mode=False)

    def setup(self):
        form=deepcopy(self.form_fixture.form)
        if self.live:
            date=self.app.clock().date()+timedelta(days=1)
            form.update(start_date=date.isoformat(),end_date=(date+timedelta(days=1)).isoformat())
        examples=EXAMPLES if not self.live else {k:LIVE_EXAMPLES[k] for k in
            ("ack","weather","verification","transfer","candidates","pickup_choices","itinerary",
             "patch_preview","recheck_report","export_result")}
        return dict(live=self.live,model=self.model_on,model_provider=self.model_provider if self.model_on else None,
                    examples=examples,sample_trip=self.sample,
                    form=form,pdf=bool(self.exports.pdf),schema=REPLY.schema)

    def plan(self,data,trace):
        if set(data)!={"start_date","budget","return_time","confirmed"}:
            raise ValueError("invalid form")
        from datetime import date
        start=date.fromisoformat(data["start_date"])
        end=(start+timedelta(days=1)).isoformat()
        if type(data["budget"]) is not int or not 1<=data["budget"]<=10_000_000:
            raise ValueError("invalid budget")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",data["return_time"]):
            raise ValueError("invalid time")
        form=deepcopy(self.form_fixture.form)
        form.update(start_date=start.isoformat(),end_date=end,budget_fen=data["budget"]*100)
        form["return"].update(departure_at=end+"T"+data["return_time"]+":00+08:00",confirmed=data["confirmed"] is True)
        return asyncio.run(self.timeline.preview(self.token,form,trace=trace))

    def turn(self,data,trace):
        mode,message,trip_id=data["mode"],data["message"],data.get("trip_id")
        if not isinstance(message,str) or not 0<len(message.strip())<=600:
            raise ValueError("invalid message")
        if mode=="itinerary":
            return self.plan(data["plan"],trace)
        if mode=="bundle" and not self.live:
            sample=deepcopy(SAMPLES["bundle"])
            sample["result"]["supplements"]=[deepcopy(SAMPLES["transfer"]["result"])]
            sample["evidence"]=deepcopy(SAMPLES["transfer"]["evidence"])
            return sample
        if self.testing and mode in {"bad","bad_fields"}:
            sample=deepcopy(SAMPLES["weather"])
            if mode=="bad":
                sample["result"]["kind"]="unknown"
            if mode=="bad_fields":
                del sample["result"]["forecast"]
            return sample
        if mode=="model":
            if not self.model_on:
                name = "Radeon" if self.model_provider == "radeon" else "百炼"
                raise WorkbenchError(f"当前未私密配置{name} Key。仍可用本页示例验证工具。",503)
        elif not self.testing and mode not in self.setup()["examples"] or mode not in EXAMPLES:
            raise NotFound()
        if not self.live and mode=="clarification":
            return deepcopy(SAMPLES["clarification"])
        home={"scenic_area":"黄果树"} if mode=="verification" else {}
        # Per test turn begins with fixed context. Live chat keeps the authenticated session across messages.
        with self.agent_lock:
            self.current_mode=mode
            self.current_trace=trace
            agent=self.agent if self.live else Conversation(typed_output=True,clock=self.app.clock,
                      intent_model=self.intent,handlers=self.handlers(),fixture_mode=True)
            return asyncio.run(self.app.turn(self.token,message,home,trip_id=trip_id,
                              session_id=self.session,trace=trace,conversation_factory=lambda:agent))

    def opened(self,trip_id,trace):
        opened=self.recheck.open(self.token,trip_id,trace=trace)
        version=self.app.read_trip(self.token,trip_id)
        opened["locked_labels"]={x.stable_id:{"rail":"返程票","hotel":"酒店"}.get(x.kind,"锁定安排") for x in version.locked_items}
        return opened

    def action(self,path,data,trace):
        if path=="/api/turn":
            return self.turn(data,trace)
        if path=="/api/plan":
            return self.plan(data,trace)
        if path=="/api/draft/edit":
            if set(data)!={"draft_id","stop_id","arrival","departure"}:
                raise ValueError("invalid edit")
            return self.timeline.edit(self.token,**data)
        if path=="/api/draft/cancel":
            self.timeline.cancel(self.token,data["draft_id"])
            return {"cancelled":True}
        if path=="/api/draft/save":
            version=self.timeline.save(self.token,data["draft_id"],data["key"],confirmed=data["confirmed"])
            return self.opened(version.trip_id,trace)
        if path=="/api/choice":
            return asyncio.run(self.app.choose_candidate(self.token,self.session,trace=trace,**data))
        match=re.fullmatch(r"/api/trips/([a-f0-9]{32})/(.+)",path)
        if not match:
            raise NotFound()
        trip,action=match.groups()
        if action=="export":
            return self.exports.create(self.token,trip,trace=trace,**data)
        if action=="recheck":
            return self.recheck.start(self.token,trip,trace=trace,**data)
        if action in {"patch/commit","patch/discard","patch/operation-status","patch/retime","patch/preview"}:
            patch=self.app.get_patch_service()
            if action=="patch/preview":
                if set(data)!={"scope_dates","instruction","base_revision"}:
                    raise ValueError("invalid preview")
                return asyncio.run(patch.preview(self.token,trip,trace=trace,**data))
            method={"patch/commit":patch.commit,"patch/discard":patch.discard,
                    "patch/operation-status":patch.operation_status,"patch/retime":patch.preview_retime}[action]
            if action=="patch/retime":
                return asyncio.run(method(self.token,trip,trace=trace,**data))
            return method(self.token,trip,trace=trace,**data)
        decision=re.fullmatch(r"recheck/([a-f0-9]{32})/(preview|decline)",action)
        if decision and not data:
            key,choice=decision.groups()
            if choice=="preview":
                return asyncio.run(self.recheck.accept(self.token,trip,key,trace=trace))
            return self.recheck.decline(self.token,trip,key,trace=trace)
        raise NotFound()


class WorkbenchError(ValueError):
    def __init__(self,message,status):
        super().__init__(message)
        self.status=status


@contextmanager
def serve(ui, *, browser_checks=None, release_audit=None, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def local(self,*,post=False):
            host=f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host")==host and self.headers.get("Origin","" if post else "http://"+host)=="http://"+host
        def request_token(self):
            cookie=SimpleCookie(self.headers.get("Cookie","")).get("m11")
            return cookie.value if cookie else None
        def auth(self):
            if self.request_token()!=ui.token:
                raise AuthRequired("请打开本机首页")
        def send(self,status,data,mime,*,cookie=False,attachment=None):
            self.send_response(status)
            self.send_header("Content-Type",mime)
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            style="'self' 'unsafe-inline'"
            self.send_header("Content-Security-Policy",f"default-src 'none'; script-src 'self'; connect-src 'self'; style-src {style}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie",f"m11={ui.token}; HttpOnly; SameSite=Strict; Path=/")
            if attachment:
                self.send_header("Content-Disposition",attachment)
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def packet(self,value,status=200):
            self.send(status,json.dumps(value,ensure_ascii=False).encode(),"application/json; charset=utf-8")
        def do_GET(self):
            if not self.local():
                return self.packet({"error":"仅限本机"},403)
            if self.path=="/":
                html=(ROOT/"frontend/travel_workbench.html").read_text(encoding="utf-8")
                if ui.testing:
                    html=html.replace("</head>",'<script defer src="/checks.js"></script></head>')
                return self.send(200,html.encode(),"text/html; charset=utf-8",cookie=True)
            if self.path in ASSETS:
                name,mime=ASSETS[self.path]
                return self.send(200,(ROOT/"frontend"/name).read_bytes(),mime+"; charset=utf-8")
            if ui.testing and self.path=="/checks.js":
                path=browser_checks or TEST/"m11/browser_checks.js"
                return self.send(200,path.read_bytes(),"text/javascript; charset=utf-8")
            try:
                parsed=urlsplit(self.path)
                export=re.fullmatch(r"/api/exports/([a-f0-9]{32})",parsed.path)
                if export:
                    artifact=ui.exports.download(self.request_token(),export[1],trace=trace_for("download"))
                    suffix="pdf" if artifact.content_type=="application/pdf" else "html"
                    disposition="attachment" if parsed.query=="download=1" else "inline"
                    return self.send(200,artifact.content,artifact.content_type,
                                     attachment=f'{disposition}; filename="travel-r{artifact.revision}.{suffix}"')
                self.auth()
                if self.path=="/test/release-state" and ui.testing and release_audit:
                    return self.packet(release_audit())
                if self.path=="/api/setup":
                    return self.packet(dict(ui.setup(),testing=ui.testing))
                match=re.fullmatch(r"/api/trips/([a-f0-9]{32})(?:/recheck/([a-f0-9]{32}))?",self.path)
                if match:
                    trip,run=match.groups()
                    return self.packet(ui.recheck.get(ui.token,trip,run) if run else ui.opened(trip,trace_for("open")))
                raise NotFound()
            except (AuthRequired,NotFound) as exc:
                self.packet({"error":str(exc)},exc.status)
        def do_POST(self):
            if not self.local(post=True):
                return self.packet({"error":"仅限本机"},403)
            try:
                self.auth()
                size=int(self.headers.get("Content-Length",0))
                if not 0<size<=16000 or self.headers.get_content_type()!="application/json":
                    raise ValueError("invalid body")
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):
                    raise ValueError("invalid body type")
                trace=trace_for("request")
                value=ui.action(self.path,data,trace)
                if isinstance(value,dict) and "trace_id" in value:
                    value=dict(value,trace_id=trace.trace_id)
                trace.record("request_finished",status="completed")
                self.packet(dict(value=value,trace_id=trace.trace_id,trace=trace.events))
            except (AuthRequired,NotFound,WorkbenchError,PatchError,RecheckError,ChoiceError,Conflict) as exc:
                self.packet({"error":str(exc),"code":getattr(exc,"code",None)},exc.status)
            except (TypeError,ValueError,KeyError,ExportError,ContractError):
                self.packet({"error":"输入或数据无法校验，请检查行程日期、地点和当前版本。"},422)

    with ThreadingHTTPServer(("127.0.0.1",port),Handler) as server:
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join()


def run(args):
    configure_live(args)
    with TemporaryDirectory(prefix="travel-m11-demo-") as folder:
        work=Path(folder)
        clock=(lambda:datetime.now(BEIJING)) if args.live else (lambda:NOW)
        with isolated_database(work/"travel_m11.sqlite3",work) as db:
            app,token,_,trip=make_case(db,clock=clock)
            ui=Workbench(app,token,trip,live=args.live,testing=bool(args.browser),pdf_browser=args.pdf_browser,
                         model_provider=args.model_provider)
            try:
                with serve(ui,port=args.port) as url:
                    print(url,flush=True)
                    if not args.browser:
                        try:
                            if args.stay_open:
                                Event().wait()
                            else:
                                input("按回车关闭本机联调页。")
                        except (EOFError,KeyboardInterrupt):
                            pass
                        return 0
                    out=args.output.resolve()
                    out.mkdir(parents=True,exist_ok=True)
                    captures=("home,ack,weather,verification,transfer,cost_breakdown,candidates,bundle,itinerary,patch_preview,fallback"
                              if args.width>600 else "home,zoom")
                    command=["node",str(TEST/"m08/browser_runner.mjs"),str(args.browser),url,
                             str(work/"profile"),str(out),str(args.width),captures,"a11y"]
                    result=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",timeout=55)
                    print(result.stdout+result.stderr)
                    data=json.loads(result.stdout) if result.stdout.strip() else {"status":"FAIL","reason":"browser_result_missing"}
                    data.update(data_mode="fixture",width=args.width,sqlite=True)
                    (out/"report.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                    return result.returncode
            finally:
                ui.recheck.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",type=Path)
    parser.add_argument("--pdf-browser",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--width",type=int,choices=(390,1100),default=1100)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--ask-key",action="store_true")
    parser.add_argument("--model-provider", choices=("dashscope","radeon"), default="dashscope")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--stay-open", action="store_true", help="保持本机工作台运行，直至进程停止")
    for name in ("weather","rail","osm"):
        parser.add_argument("--"+name+"-entry",type=Path)
    args=parser.parse_args()
    if args.browser and (args.output is None or args.live):
        parser.error("--browser 需要固定数据和 --output")
    if not 0 <= args.port <= 65535:
        parser.error("--port 必须在 0 到 65535 之间")
    if args.model_provider == "radeon" and not args.live:
        parser.error("--model-provider radeon 需要 --live")
    if args.ask_key:
        key_prompt = "Radeon" if args.model_provider == "radeon" else "百炼"
        if not args.live:
            parser.error("--ask-key 需要 --live")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error",GetPassWarning)
                key=getpass(f"{key_prompt} API Key（输入不显示）: ").strip()
        except (GetPassWarning,EOFError,KeyboardInterrupt):
            parser.exit(2,"请使用本机交互式 PowerShell。\n")
        if not key:
            parser.error("Key 不能为空")
        os.environ[RADEON_KEY_ENV if args.model_provider == "radeon" else "DASHSCOPE_API_KEY"]=key
        del key
    raise SystemExit(run(args))
