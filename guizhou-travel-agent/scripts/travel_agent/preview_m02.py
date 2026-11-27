"""Build a local read-only M02 fixture page and optionally test it in installed Chrome."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from travel_contract import REPLY, validate_reply

CORE = ("ack", "weather", "verification", "transfer", "cost_breakdown", "candidates", "itinerary", "patch_preview")


def page(path, *, check_browser=False, selected=None):
    samples = json.loads((ROOT / "tests/travel_agent/fixtures/m02/replies.json").read_text(encoding="utf-8"))
    for packet in samples.values():
        validate_reply(packet, fixture_mode=True)
    injection = deepcopy(samples["verification"])
    injection["result"]["finding"] = '<img src=x onerror="window.m2Injected=true">外部材料要求下单'
    unknown = {**samples["ack"], "result": {"kind": "unknown", "text": "unknown"}}
    version = {**samples["ack"], "schema_version": "9.0"}
    core = [samples[kind] for kind in CORE if selected in {None, kind}]
    edges = [unknown, version, injection, samples["bundle"]] if check_browser else []
    data = json.dumps({"schema": REPLY.schema, "core": core, "edge": edges}, ensure_ascii=False).replace("<", "\\u003c")
    js = (ROOT / "frontend/travel_result.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend/travel_result.css").read_text(encoding="utf-8")
    assertions = (ROOT / "tests/travel_agent/m02/browser_assertions.js").read_text(encoding="utf-8") if check_browser else ""
    monitor = """
window.m2AttemptedRequests = 0;
const rejectRequest = () => { window.m2AttemptedRequests += 1; throw new Error("Preview is offline"); };
window.fetch = rejectRequest;
XMLHttpRequest.prototype.open = rejectRequest;
navigator.sendBeacon = rejectRequest;
window.WebSocket = rejectRequest;
Storage.prototype.setItem = rejectRequest;
Storage.prototype.removeItem = rejectRequest;
Storage.prototype.clear = rejectRequest;
""" if check_browser else ""
    html = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; base-uri 'none'">
<title>贵州旅行 · 结果组件</title><style>{css}</style></head>
<body><main><header><h1>贵州旅行 · 结果组件</h1><p>固定样例 · 预报、费用和路线均为模拟数据</p></header>
<section id="samples" aria-label="八类结果"></section><section id="edge-cases" aria-label="浏览器验收样例"></section></main>
<script>{monitor}
{js}
const m2Data = {data};
for (const packet of m2Data.core) document.querySelector("#samples").append(renderResult(packet, m2Data.schema));
for (const packet of m2Data.edge) document.querySelector("#edge-cases").append(renderResult(packet, m2Data.schema));
{assertions}
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def chat_page(path, *, test=False):
    page(path)
    html = path.read_text(encoding="utf-8")
    start = html.index("<body>")
    end = html.index("<script>", start)
    contents = """<body><main class="chat"><header>
<h1>贵州旅行聊天演示</h1><p>仅用固定样例回复，不查询实时天气或价格。</p></header>
<section id="chat-prompts" aria-label="示例问题"></section>
<section id="chat-log" role="log" aria-label="聊天记录"></section>
<form id="chat-form"><input id="chat-input" aria-label="输入消息" placeholder="输入消息或点击示例问题" maxlength="300">
<button type="submit">发送</button></form></main>
"""
    html = html[:start] + contents + html[end:]
    html = html[:html.index('for (const packet of m2Data.core)')]
    demo = (ROOT / "frontend/travel_chat_demo.js").read_text(encoding="utf-8-sig")
    assertions = (ROOT / "tests/travel_agent/m02/chat_assertions.js").read_text(encoding="utf-8-sig") if test else ""
    path.write_text(html + demo + "\n" + assertions + "\n</script></body></html>", encoding="utf-8")


def test_chat(browser, output):
    results = {}
    with TemporaryDirectory(prefix="travel-m02-chat-") as folder:
        work = Path(folder)
        fixture = work / "chat.html"
        chat_page(fixture, test=True)
        for viewport in (1280, 500):
            result = chrome_run(browser, work / "profile", fixture.as_uri(), "--dump-dom",
                                f"--window-size={viewport},900")
            dom = result.stdout.decode("utf-8", errors="replace")
            match = re.search(r'data-m02-browser="(PASS|FAIL)"[^>]*data-m02-checks="([^"]+)"', dom)
            results[viewport] = {"status": match[1] if match else "FAIL", "checks": match[2] if match else ""}
    (output.parent / "chat-browser-report.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results))
    return all(result["status"] == "PASS" for result in results.values())


def chrome_flags(profile):
    return [
        "--headless=new", "--disable-gpu", "--disable-background-networking", "--disable-component-update",
        "--disable-sync", "--no-first-run", "--no-default-browser-check",
        "--host-resolver-rules=MAP * ~NOTFOUND", f"--user-data-dir={profile}",
    ]


def chrome_run(browser, profile, url, *args):
    return subprocess.run([str(browser), *chrome_flags(profile), *args, url],
                          capture_output=True, timeout=50, check=True)


def run_browser(browser, preview, artifacts):
    with TemporaryDirectory(prefix="travel-m02-browser-") as folder:
        work = Path(folder)
        profile = work / "profile"
        tests = work / "assertions.html"
        page(tests, check_browser=True)
        results = {}
        for name, width in (("desktop", 1280), ("mobile", 430)):
            process = chrome_run(browser, profile, tests.as_uri(), "--dump-dom", f"--window-size={width},1000")
            dom = process.stdout.decode("utf-8", errors="replace")
            match = re.search(r'<body[^>]*data-m02-browser="(PASS|FAIL)"[^>]*data-m02-checks="([^"]+)"', dom)
            viewport = re.search(r'data-m02-width="([0-9]+)"', dom)
            actual_width = int(viewport[1]) if viewport else None
            correct_layout = actual_width is not None and ((actual_width <= 710) == (name == "mobile"))
            results[name] = {"status": match[1] if match and correct_layout else "FAIL",
                             "viewport_width": actual_width, "assertions": match[2].split(",") if match else []}
        for kind in CORE:
            card = work / f"{kind}.html"
            screenshot = (artifacts / f"{kind}.png").resolve()
            page(card, selected=kind)
            chrome_run(browser, profile, card.as_uri(), "--window-size=840,830", f"--screenshot={screenshot}")
            if not screenshot.is_file() or screenshot.stat().st_size < 1000:
                raise RuntimeError(f"Screenshot not produced for {kind}")
        browser_cmd = [str(browser), *chrome_flags("<isolated profile>"), "--dump-dom",
                       "--window-size=<1280 or 430>,1000", str(tests)]
        report = {"case_id": "M2-08", "layer": "browser-component", "browser_command": browser_cmd,
                  "fixture": str(preview), "results": results, "screenshots": [f"{kind}.png" for kind in CORE]}
        (artifacts / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name, outcome in results.items():
            print(name, outcome["status"], f"assertions={len(outcome['assertions'])}", ",".join(outcome["assertions"]))
        return all(result["status"] == "PASS" for result in results.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--chat", action="store_true", help="build the offline chat demonstration")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.chat:
        chat_page(output)
        print(f"Offline chat demo written to {output.name}")
        return 0 if not args.browser or test_chat(args.browser, output) else 1
    page(output)
    print(f"Fixture preview written to {output.name}")
    return 0 if not args.browser or run_browser(args.browser, output, output.parent) else 1


if __name__ == "__main__":
    raise SystemExit(main())

