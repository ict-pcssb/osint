"""Local M9 chat, browser assertions and SQLite evidence for the two-day fixture."""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests/travel_agent"
sys.path[:0] = [str(ROOT), str(TESTS), str(TESTS / "m09")]
from fixture_m09 import MESSAGE, NOW, count, fields_digest, make_trip, model_factory, planner
from support.guards import Trace, isolated_database
from travel_contract import REPLY
from travel_patch import PatchError, PatchService, day_id
from travel_state_app import AuthRequired, NotFound, TravelApplication

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>修改我的贵阳行程</title><link rel="stylesheet" href="/result.css">
<script defer src="/result.js"></script><script defer src="/patch.js"></script>{checks}</head>
<body><main class="chat patch-workbench"><header><h1>修改我的贵阳行程</h1>
<p>本机测试行程。金额、车次和路段使用固定测试数据。</p></header>
<label for="patch-message">想改哪一天？</label>
<form id="patch-form"><input id="patch-message" name="instruction" value="{instruction}" maxlength="500" required>
<button type="submit" disabled>看修改预览</button></form>
<p id="patch-status" role="status"></p>
<section id="patch-preview" aria-live="polite"></section>
<section id="traveller-timeline" aria-label="已保存的行程"></section></main></body></html>"""
ASSETS = {"/result.js": ("frontend/travel_result.js", "text/javascript"),
          "/result.css": ("frontend/travel_result.css", "text/css"),
          "/patch.js": ("frontend/travel_patch.js", "text/javascript")}


@contextmanager
def local_page(app, token, trip_id, trace, *, testing=False):
    service = app.get_patch_service()
    drop_once = Event()
    prefix = f"/api/travel/trips/{trip_id}/patch/"
    version_conflict = lambda: PatchError("version_conflict", 409)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data, mime, *, cookie=False):
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                             "frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", f"m09={token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def packet(self, value, status=200):
            self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json")

        def local(self):
            host = f"127.0.0.1:{self.server.server_port}"
            origin = "http://" + host
            return self.headers.get("Host") == host and self.headers.get("Origin", origin) == origin

        def signed_in(self):
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("m09")
            return self.local() and cookie and cookie.value == token

        def do_GET(self):
            if not self.local():
                return self.packet({"error": "仅供本机查看"}, 403)
            if self.path == "/":
                checks = '<script defer src="/checks.js"></script>' if testing else ""
                self.send(200, HTML.format(checks=checks, instruction=MESSAGE).encode("utf-8"), "text/html", cookie=True)
            elif self.path in ASSETS:
                file, mime = ASSETS[self.path]
                self.send(200, (ROOT / file).read_bytes(), mime)
            elif self.path == "/schema":
                self.packet(REPLY.schema)
            elif self.path == "/api/trip" and self.signed_in():
                version = app.read_trip(token, trip_id)
                lock = next((x.fields for x in version.locked_items if x.kind == "rail"), None)
                lock_labels = {"rail":"已锁定返程票", "hotel":"已锁定酒店", "booking":"已锁定预约"}
                self.packet(dict(trip_id=trip_id, revision=version.revision, schema_version=version.schema_version,
                                 timeline=version.timeline, return_lock=lock,
                                 locked_labels={x.stable_id:lock_labels.get(x.kind, "已锁定安排")
                                                for x in version.locked_items},
                                 day_ids={day["date"]:day_id(day) for day in version.timeline["result"]["days"]}))
            elif testing and self.path == "/checks.js":
                self.send(200, (TESTS / "m09/browser_checks.js").read_bytes(), "text/javascript")
            elif testing and self.path == "/test/counts" and self.signed_in():
                self.packet(count(app))
            else:
                self.packet({"error": "找不到入口"}, 404)

        def do_POST(self):
            if not self.signed_in():
                return self.packet({"error": "请在本机页面重新打开会话"}, 403)
            if not self.path.startswith(prefix):
                return self.packet({"error": "找不到行程"}, 404)
            action = self.path.removeprefix(prefix)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 4096 or self.headers.get_content_type() != "application/json":
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError()
                if action == "preview" and set(data) == {"instruction", "base_revision"}:
                    if (not isinstance(data["instruction"], str) or not 0 < len(data["instruction"]) <= 500
                            or type(data["base_revision"]) is not int):
                        raise ValueError()
                    if app.read_trip(token, trip_id).revision != data["base_revision"]:
                        raise version_conflict()
                    result = asyncio.run(app.turn(token, data["instruction"], trip_id=trip_id,
                                         conversation_factory=model_factory(app.clock), trace=trace))
                    if result["result"].get("base_revision", data["base_revision"]) != data["base_revision"]:
                        raise version_conflict()
                elif action == "commit":
                    result = service.commit(token, trip_id, trace=trace, **data)
                    if testing and not drop_once.is_set():
                        drop_once.set()
                        trace.record("m09_http_response_lost", route=action, revision=result["revision"])
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                elif action == "discard":
                    result = service.discard(token, trip_id, trace=trace, **data)
                elif action == "operation-status":
                    result = service.operation_status(token, trip_id, trace=trace, **data)
                else:
                    return self.packet({"error": "找不到入口"}, 404)
                self.packet(result)
            except PatchError as exc:
                self.packet(dict(error=str(exc), code=exc.code, save_status=exc.save_status), exc.status)
            except AuthRequired:
                self.packet({"error": "会话已失效"}, 401)
            except NotFound:
                self.packet({"error": "找不到行程或预览"}, 404)
            except (ValueError, TypeError):
                self.packet({"error": "局部修改请求无效"}, 422)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join()


def snapshot(version):
    days = version.timeline["result"]["days"]
    return dict(revision=version.revision, first_day=days[0], second_day=days[1],
                locks=[asdict(item) for item in version.locked_items], lock_digest=fields_digest(version.locked_items))


def run(browser, output, width):
    with TemporaryDirectory(prefix="travel-m09-browser-") as folder:
        work = Path(folder).resolve()
        with isolated_database(work / "travel_m09.sqlite3", work) as database:
            app = TravelApplication(database, clock=lambda: NOW)
            app.create_account("m09_demo", "local-demo-only")
            token = app.login("m09_demo", "local-demo-only")
            before = asyncio.run(make_trip(app, token))
            baseline_counts = count(app)
            app.patch_service = PatchService(app, planner)
            trace = Trace("m09-browser-" + str(width))
            with local_page(app, token, before.trip_id, trace, testing=bool(browser)) as url:
                print(url, flush=True)
                if not browser:
                    try:
                        input("按回车结束本机演示。")
                    except (EOFError, KeyboardInterrupt):
                        pass
                    return 0
                output.mkdir(parents=True, exist_ok=True)
                cmd = ["node", str(TESTS / "m08/browser_runner.mjs"), str(browser), url,
                       str(work / "chrome-profile"), str(output), str(width), "preview,declined,committed"]
                done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=50, check=False)
                print(done.stdout + done.stderr)
                result = json.loads(done.stdout) if done.stdout.strip() else {"status": "FAIL", "failure": done.stderr}
                after = app.read_trip(token, before.trip_id)
                result.update(data_mode="fixture", layer="chrome_local_http_sqlite", width=width,
                              provider_calls=0, before_counts=baseline_counts, after_counts=count(app),
                              before=snapshot(before), after=snapshot(after), trace=trace.events)
                (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return done.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.browser and not args.output:
        parser.error("--browser needs --output")
    if args.browser:
        root = args.output.resolve()
        results = [run(args.browser, root / view, width) for view, width in (("desktop", 1100), ("narrow", 390))]
        return max(results)
    return run(None, None, 1100)


if __name__ == "__main__":
    raise SystemExit(main())

