"""Local manual check of existing read-only Skills and their live providers."""
import argparse
import asyncio
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from getpass import GetPassWarning, getpass
import json
import os
from pathlib import Path
import sys
import traceback
import warnings
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from travel_agent import Conversation
from travel_candidates_live import configured_skills, setting
from travel_candidate_context import ChoiceError
from travel_contract import REPLY
from travel_state import BEIJING
from travel_state_app import TravelApplication

FILES = {
    "/": ("demo_skills_live.html", "text/html"),
    "/live.js": ("demo_skills_live.js", "text/javascript"),
    "/result.js": ("travel_result.js", "text/javascript"),
    "/result.css": ("travel_result.css", "text/css"),
}
TRACE_KEYS = ("event", "intent", "skill", "tool", "provider", "status", "reason", "allowed_tools", "write_policy")


def serve(path):
    for name in ("NETWORK", "AMAP", "HOTEL", "FOOD"):
        os.environ["TRAVEL_M08_" + name] = "1"
    clock = lambda: datetime.now(BEIJING)
    app = TravelApplication(path, clock=clock, candidate_skills=configured_skills(clock))
    app.create_account("live_demo", "loopback-demo-only")
    token = app.login("live_demo", "loopback-demo-only")
    session = app.open_session(token)
    day = (clock().date() + timedelta(days=2)).isoformat()
    presets = {
        "taxi": ("贵阳北站到青岩北门停车场，经济型打车估价", {
            "candidate_kind":"taxi", "from_id":"guiyang-north", "to_id":"qingyan-north-parking", "vehicle_class":"经济",
        }),
        "hotel": (f"{day} 贵阳，两位成人一间房住两晚", {
            "candidate_kind":"hotel", "place_id":"guiyang", "check_in":day, "nights":2, "adults":2, "rooms":1,
        }),
        "dining": (f"{day} 晚上七点，青岩古镇附近找餐馆", {
            "candidate_kind":"dining", "place_id":"qingyan", "meal_at":day+"T19:00:00+08:00",
        }),
    }
    conversations = {}
    for name, (_, parameters) in presets.items():
        async def intent(*_, parameters=parameters):
            return json.dumps(dict(intent="candidates", confidence_band="high",
                                   requested_outputs=["candidates"], parameters=parameters))
        conversations[name] = Conversation(typed_output=True, clock=clock, intent_model=intent)
    conversations["model"] = Conversation(typed_output=True, clock=clock)
    model_ready = bool(os.getenv("DASHSCOPE_API_KEY"))

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, kind):
            self.send_response(status)
            self.send_header("Content-Type", kind+"; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def packet(self, obj, status=200):
            self.send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        def local(self, post=False):
            host = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host")==host and (not post or self.headers.get("Origin")=="http://"+host)

        def do_GET(self):
            if not self.local():
                return self.packet({"error":"仅限本机"}, 403)
            if self.path in FILES:
                name, kind = FILES[self.path]
                return self.send(200, (ROOT/"frontend"/name).read_bytes(), kind)
            if self.path=="/setup":
                return self.packet(dict(schema=REPLY.schema, model=model_ready,
                                        available=dict(taxi=bool(setting("AMAP_KEY")),
                                                       hotel=bool(setting("ROLLINGGO_HOTEL_API_KEY")), dining=True),
                                        examples={key:value[0] for key,value in presets.items()}))
            self.packet({"error":"入口不存在"}, 404)

        def do_POST(self):
            if not self.local(post=True):
                return self.packet({"error":"仅限本机页面"}, 403)
            trace = []
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0<size<=4096 or self.headers.get_content_type()!="application/json":
                    raise ValueError
                payload = json.loads(self.rfile.read(size))
                if self.path=="/pick":
                    if set(payload)!={"context_id","choice_id"}:
                        raise ValueError
                    packet = asyncio.run(app.choose_candidate(token, session, trace=trace, **payload))
                elif self.path=="/ask":
                    mode = payload["mode"]
                    if mode=="model" and not model_ready:
                        return self.packet({"error":"缺 DASHSCOPE_API_KEY，仍可使用只读预设"}, 503)
                    message = payload["message"] if mode=="model" else presets[mode][0]
                    if not isinstance(message,str) or not 0<len(message.strip())<=600:
                        raise ValueError
                    packet = asyncio.run(app.turn(token, message, session_id=session, trace=trace,
                                                  conversation_factory=lambda: conversations[mode]))
                else:
                    return self.packet({"error":"入口不存在"}, 404)
                self.packet(dict(packet=packet, trace=[{k:e[k] for k in TRACE_KEYS if k in e} for e in trace]))
            except ChoiceError as exc:
                self.packet({"error":str(exc)}, exc.status)
            except (ValueError, TypeError, KeyError) as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                print(type(exc).__name__, [(Path(frame.filename).name, frame.lineno) for frame in frames], flush=True)
                self.packet({"error":"查询未完成，请查看服务端诊断"}, 422)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask-key", action="store_true", help="Read the model key privately in the console")
    if parser.parse_args().ask_key:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                key = getpass("百炼 API Key（输入不显示）: ").strip()
        except (GetPassWarning, EOFError, KeyboardInterrupt):
            parser.exit(2, "请在本机交互式 PowerShell 中输入。\n")
        if not key:
            parser.error("API Key 不能为空")
        os.environ["DASHSCOPE_API_KEY"] = key
        del key
    with TemporaryDirectory(prefix="travel-skills-demo-") as folder:
        try:
            serve(Path(folder)/"skills-demo.sqlite3")
        except KeyboardInterrupt:
            pass

