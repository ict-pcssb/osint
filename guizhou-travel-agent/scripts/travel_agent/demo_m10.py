"""Local M10 browser exercise with fixed facts, HTTP and real temporary SQLite."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT/"tests/travel_agent"
sys.path[:0] = [str(ROOT),str(TESTS),str(TESTS/"m10")]
from fixture_m10 import NOW, advance, make_case, observed, previous, set_facts
from support.guards import Trace, isolated_database
from travel_contract import REPLY
from travel_patch import PatchError
from travel_recheck import RecheckError, RecheckService
from travel_state_app import AuthRequired, NotFound

STATIC = {
    "/result.js":("travel_result.js","text/javascript"), "/result.css":("travel_result.css","text/css"),
    "/patch.js":("travel_patch.js","text/javascript"), "/recheck.js":("travel_recheck.js","text/javascript"),
    "/recheck.css":("travel_recheck.css","text/css"),
}


@contextmanager
def serve(app, token, trip_id, service, trace, calls, *, checks=False):
    prefix = f"/api/travel/trips/{trip_id}/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass

        def send(self,status,data,mime="application/json",*,cookie=False):
            self.send_response(status)
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Content-Type",mime+"; charset=utf-8")
            if cookie:
                self.send_header("Set-Cookie",f"m10={token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def packet(self,data,status=200):
            self.send(status,json.dumps(data,ensure_ascii=False).encode("utf-8"))

        def local(self):
            host = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host")==host and self.headers.get("Origin","http://"+host)=="http://"+host

        def signed_in(self):
            cookie = SimpleCookie(self.headers.get("Cookie","")).get("m10")
            return self.local() and cookie and cookie.value==token

        def do_GET(self):
            if not self.local():
                return self.packet({"error":"仅限本机"},403)
            if self.path=="/":
                html=(ROOT/"frontend/travel_recheck.html").read_text(encoding="utf-8")
                if checks:
                    html=html.replace("</head>",'<script defer src="/checks.js"></script></head>')
                return self.send(200,html.encode("utf-8"),"text/html",cookie=True)
            if self.path in STATIC:
                filename,mime=STATIC[self.path]
                return self.send(200,(ROOT/"frontend"/filename).read_bytes(),mime)
            if checks and self.path=="/checks.js":
                return self.send(200,(TESTS/"m10/browser_checks.js").read_bytes(),"text/javascript")
            if not self.signed_in():
                return self.packet({"error":"请重新打开本机页面"},401)
            try:
                if self.path=="/schema":
                    return self.packet(REPLY.schema)
                if self.path=="/api/trip":
                    opened=service.open(token,trip_id,trace=trace)
                    version=app.read_trip(token,trip_id)
                    lock=next((x.fields for x in version.locked_items if x.kind=="rail"),None)
                    labels={x.stable_id:{"rail":"已锁定返程票","hotel":"已锁定酒店"}.get(x.kind,"锁定安排")
                            for x in version.locked_items}
                    return self.packet(dict(opened,return_lock=lock,locked_labels=labels))
                if self.path.startswith(prefix+"recheck/"):
                    run_id=self.path.removeprefix(prefix+"recheck/")
                    if re.fullmatch(r"[a-f0-9]{32}",run_id):
                        return self.packet(service.get(token,trip_id,run_id))
                if checks and self.path=="/test/state":
                    version=app.read_trip(token,trip_id)
                    return self.packet(dict(revision=version.revision,calls=len(calls),
                                            lock_hash=[x.fields_hash for x in version.locked_items]))
            except (AuthRequired,NotFound) as exc:
                return self.packet({"error":str(exc)},exc.status)
            self.packet({"error":"找不到该资源"},404)

        def do_POST(self):
            if not self.signed_in():
                return self.packet({"error":"请重新打开本机页面"},401)
            try:
                if checks and self.path=="/test/advance":
                    current=app.read_trip(token,trip_id)
                    return self.packet(dict(revision=advance(app,token,current).revision))
                size=int(self.headers.get("Content-Length",0))
                if not 0<size<=4096 or self.headers.get_content_type()!="application/json":
                    raise ValueError("invalid body")
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict) or not self.path.startswith(prefix):
                    raise NotFound()
                action=self.path.removeprefix(prefix)
                if action=="recheck":
                    return self.packet(service.start(token,trip_id,trace=trace,**data),202)
                matched=re.fullmatch(r"recheck/([a-f0-9]{32})/(preview|decline)",action)
                if matched and not data:
                    run_id,choice=matched.groups()
                    result=(asyncio.run(service.accept(token,trip_id,run_id,trace=trace)) if choice=="preview"
                            else service.decline(token,trip_id,run_id,trace=trace))
                    return self.packet(result)
                patch=app.get_patch_service()
                if action=="patch/commit":
                    return self.packet(patch.commit(token,trip_id,trace=trace,**data))
                if action=="patch/discard":
                    return self.packet(patch.discard(token,trip_id,trace=trace,**data))
                if action=="patch/operation-status":
                    return self.packet(patch.operation_status(token,trip_id,trace=trace,**data))
                raise NotFound()
            except (AuthRequired,NotFound,RecheckError,PatchError) as exc:
                self.packet({"error":str(exc),"code":getattr(exc,"code",None)},exc.status)
            except (TypeError,ValueError):
                self.packet({"error":"核查请求格式不正确"},422)

    with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
        worker=Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            worker.join()


def run(browser, output, width):
    with TemporaryDirectory(prefix="travel-m10-demo-") as folder:
        work=Path(folder).resolve()
        now=NOW+timedelta(days=2)
        with isolated_database(work/"travel_m10.sqlite3",work) as db:
            app,token,_,trip=make_case(db,clock=lambda:now)
            set_facts(app,token,trip.trip_id,[dict(previous(),value="closed")])
            trace=Trace("m10-browser")
            calls=[]
            async def official(fact,budget):
                budget.take("fixture-notice")
                calls.append(fact["fact_id"])
                value="open" if len(calls)==1 else "closed"
                await asyncio.sleep(1.3 if browser else .65)
                return observed(value)
            service=RecheckService(app,{"opening":official},auto_open=os.getenv("TRAVEL_M10_AUTO_OPEN","1")=="1")
            try:
                with serve(app,token,trip.trip_id,service,trace,calls,checks=bool(browser)) as url:
                    print(url,flush=True)
                    if not browser:
                        try:
                            input("按回车结束本机演示。")
                        except (EOFError,KeyboardInterrupt):
                            pass
                        return 0
                    output.mkdir(parents=True,exist_ok=True)
                    cmd=["node",str(TESTS/"m08/browser_runner.mjs"),str(browser),url,
                         str(work/"chrome-profile"),str(output),str(width),"opened,changes,declined,stale"]
                    done=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",timeout=50)
                    print(done.stdout+done.stderr)
                    report=json.loads(done.stdout) if done.stdout.strip() else {"status":"FAIL","failure":done.stderr}
                    report.update(data_mode="fixture",layer="chrome_local_http_sqlite",width=width,provider_calls=len(calls),
                                  trace=trace.events,final_revision=app.read_trip(token,trip.trip_id).revision)
                    (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                    return done.returncode
            finally:
                service.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--width",type=int,choices=(1100,390),default=1100)
    args=parser.parse_args()
    if args.browser and args.output is None:
        parser.error("--browser 需要 --output")
    raise SystemExit(run(args.browser,args.output,args.width))
