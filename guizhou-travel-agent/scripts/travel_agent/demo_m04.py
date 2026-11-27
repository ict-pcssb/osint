"""Local M4 fixture UI, using the same authenticated M3 SQLite application."""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from travel_contract import REPLY
from travel_state_app import AuthRequired, Conflict, NotFound, TravelApplication
from travel_timeline import TimelineWorkflow, load_fixture
from travel_timeline_route import RouteAdapter

NOW = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
ASSETS = {
    "/result.js":("travel_result.js","text/javascript"),
    "/timeline.js":("timeline_m04.js","text/javascript"),
    "/result.css":("travel_result.css","text/css"),
    "/timeline.css":("timeline_m04.css","text/css"),
}


def counts(app):
    with app.repository.connection() as db:
        return [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("trips","trip_versions","save_keys")]


@contextmanager
def local_server(app, flow, token, *, test):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass

        def send(self,status,body,mime="application/json",cookie=False):
            self.send_response(status)
            self.send_header("Content-Type",mime+"; charset=utf-8")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie",f"m04={token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def packet(self,value,status=200):
            self.send(status,json.dumps(value,ensure_ascii=False).encode("utf-8"))

        def is_local(self):
            host=f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host")==host and self.headers.get("Origin",f"http://{host}")==f"http://{host}"

        def auth(self):
            cookie = SimpleCookie(self.headers.get("Cookie",""))
            return cookie["m04"].value if "m04" in cookie else None

        def do_GET(self):
            if not self.is_local():
                self.packet({"error":"仅允许同源本机访问"},403)
                return
            if self.path=="/":
                html=(ROOT/"frontend/timeline_m04.html").read_text(encoding="utf-8")
                if test:
                    html=html.replace("</head>",'<script src="/checks.js" defer></script></head>')
                self.send(200,html.encode("utf-8"),"text/html",cookie=True)
            elif self.path in ASSETS:
                filename,mime=ASSETS[self.path]
                self.send(200,(ROOT/"frontend"/filename).read_bytes(),mime)
            elif self.path=="/schema":
                self.packet(REPLY.schema)
            elif test and self.path=="/checks.js":
                self.send(200,(ROOT/"tests/travel_agent/m04/browser_assertions.js").read_bytes(),"text/javascript")
            elif test and self.path=="/api/test-counts" and self.auth()==token:
                self.packet(counts(app))
            else:
                self.packet({"error":"记录不存在"},404)

        def do_POST(self):
            if not self.is_local():
                self.packet({"error":"仅允许同源本机访问"},403)
                return
            actual=self.auth()
            if not actual:
                self.packet({"error":"请先打开本机首页"},401)
                return
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 1<=length<=16000 or self.headers.get_content_type()!="application/json":
                    raise ValueError("请求格式无效")
                form=json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(form,dict):
                    raise ValueError("请求必须是对象")
                if self.path=="/api/preview":
                    result=asyncio.run(flow.preview(actual,form))
                elif self.path=="/api/edit":
                    result=flow.edit(actual,**form)
                elif self.path=="/api/cancel":
                    flow.cancel(actual,**form)
                    result={"cancelled":True}
                elif self.path=="/api/save":
                    result=asdict(flow.save(actual,**form))
                elif self.path=="/api/trip":
                    result=asdict(app.read_trip(actual,**form))
                else:
                    raise NotFound()
                self.packet(result)
            except (AuthRequired, NotFound, Conflict) as exc:
                self.packet({"error":str(exc)},exc.status)
            except (KeyError, TypeError, ValueError):
                self.packet({"error":"输入无效，或草稿存在冲突；请核对日期、时段与返程确认。"},400)

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
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve",action="store_true")
    mode.add_argument("--browser",type=Path)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.browser and args.output is None:
        parser.error("--browser requires --output")
    with TemporaryDirectory(prefix="travel-m04-demo-") as working:
        root=Path(working)
        app=TravelApplication(root/"travel_m04.sqlite3",clock=lambda:NOW)
        app.create_account("m04_local","local-fixture-only")
        token=app.login("m04_local","local-fixture-only")
        data=load_fixture()
        flow=TimelineWorkflow(app,data.catalog,RouteAdapter(data.route,app.clock,fixture=True),data.costs)
        with local_server(app,flow,token,test=bool(args.browser)) as url:
            print(url,flush=True)
            if args.serve:
                try:
                    input("回车结束，关闭并清理本次演示库。")
                except (EOFError,KeyboardInterrupt):
                    pass
                return 0
            output=args.output.resolve()
            output.mkdir(parents=True,exist_ok=True)
            command=["node",str(ROOT/"tests/travel_agent/m04/browser_runner.mjs"),str(args.browser),
                     url,str(root/"chrome-profile"),str(output)]
            result=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",timeout=75,check=False)
            if result.returncode:
                print(result.stdout+result.stderr)
                return 1
            observed=json.loads(result.stdout)
            observed.update(sql_rows=counts(app),data_mode="fixture_routes_real_isolated_sqlite")
            assert observed["sql_rows"]==[1,1,1]
            (output/"report.json").write_text(json.dumps(observed,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            print(json.dumps(observed,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

