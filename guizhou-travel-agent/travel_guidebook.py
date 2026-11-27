"""Authenticated, revision-pinned HTML and optional offline Chrome PDF."""
from dataclasses import dataclass
from datetime import timedelta
from html import escape
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, RLock
from urllib.parse import urlsplit
from uuid import uuid4

from travel_contract import _validator, check
from travel_evidence import public_url
from travel_rules import format_time
from travel_state import BEIJING
from travel_state_app import NotFound
from travel_transport import event
from travel_transport_skills import reply

REQUEST = _validator("export")
MAX_BYTES = 8_000_000
HTML = "text/html; charset=utf-8"
PDF = "application/pdf"
CSS = """
@page{size:A4;margin:15mm 16mm}*{box-sizing:border-box}
body{margin:0;color:#1a2b29;font:14px/1.55 "Microsoft YaHei","Noto Sans CJK SC",sans-serif;background:#fff}
main{max-width:880px;margin:24px auto;padding:0 18px}header{border-top:5px solid #286050;padding:16px 0}
h1{font-size:30px;margin:0}h2{color:#28574b;border-bottom:1px solid #b5c9c2;padding-bottom:5px;margin:16px 0 8px}
h3,p{margin:4px 0}article{padding:8px 0;border-bottom:1px solid #e2ebe7;break-inside:avoid}
small,.muted{color:#465b55}section a{color:#214f43}p,a{overflow-wrap:anywhere}
@media print{main{max-width:none;padding:0;margin:0}.day,.sources{break-before:page}.day.first{break-before:auto}}
"""


class ExportError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code=code


def safe_source(value):
    url = public_url(value)
    if not url:
        return None
    hostname = urlsplit(url).hostname
    if not hostname or hostname.endswith((".local",".internal",".localhost")) or "." not in hostname:
        return None
    return url


def amount(value):
    return "待查" if value is None else f"¥{value/100:,.2f}"


def clock(value):
    return value[11:16] if value else "待查"


def render_book(version):
    if not version.timeline:
        raise ExportError("timeline_unavailable")
    snapshot = version.timeline
    result = snapshot["result"]
    if result["kind"]!="itinerary":
        raise ExportError("timeline_unavailable")
    h = lambda value:escape(str(value),quote=True)
    budget = result["budget"]
    scenario = "模拟数据，仅用于测试" if result["data_mode"]=="fixture" else "已保存行程快照"
    body = [f"<header><small>{h(scenario)}</small><h1>{h(version.fields.get('destination','贵州'))}旅行路书</h1>",
            f"<p>行程版本 r{version.revision} · {h(result['days'][0]['date'])} 至 {h(result['days'][-1]['date'])}</p>",
            f"<p>全团预算 {h(amount(budget['cap_fen']))} · 已知至少 {h(amount(budget['known_lower_fen']))} · 全团合计 {h(amount(budget['total_fen']))}</p></header>"]
    evidence = snapshot["evidence"]
    refs = {row["evidence_id"]:i for i,row in enumerate(evidence,1)}
    current_refs = set(result.get("evidence_ids", []))
    current_refs.update(ref for day in result["days"] for stop in day["stops"] for ref in stop["evidence_ids"])
    current_refs.update(ref for line in budget["items"] for ref in line["evidence_ids"])
    current_refs.update(result.get("return_leg", {}).get("evidence_ids", []))
    points = {point["id"]:point for point in result["map_points"]}
    for index,day in enumerate(result["days"]):
        stops = day["stops"]
        by_id = {stop["stop_id"]:stop["label"] for stop in stops}
        body.append(f'<section class="day{" first" if index==0 else ""}"><h2>第 {index+1} 天 · {h(day["date"])}</h2>')
        for i,stop in enumerate(stops):
            leg = stop["incoming_leg"]
            next_leg=stops[i+1]["incoming_leg"] if i+1<len(stops) else (
                result.get("return_leg") if index==len(result["days"])-1 else None)
            point = points.get(stop["entrance_id"])
            coords = f" · WGS84 {point['latitude']:.6f}, {point['longitude']:.6f}" if point else ""
            refs_label = "、".join(str(refs[k]) for k in stop["evidence_ids"] if k in refs)
            slot = "待查" if leg["travel_min"] is None else f"{leg['travel_min']} 分钟"
            next_time = "待查" if not next_leg or next_leg["travel_min"] is None else f"{next_leg['travel_min']} 分钟"
            role = "计划站" if stop["plan_role"]=="primary" else "备选站 · 开放与预约待核实"
            body.append(f"<article><h3>{h(stop['label'])} · {h(role)}</h3>"
                        f"<p>{h(clock(stop['arrival_local']))} – {h(clock(stop['departure_local']))}"
                        f" · 入口 {h(stop['entrance_id']+coords)}</p>"
                        f"<p>上一段 {h(slot)} · 下一段 {h(next_time)} · 下一站 {h(by_id.get(stop['next_stop_id'],'待核实'))}</p>"
                        f"<p>本站费用 {h(amount(stop['cost']['total_fen']))} · 证据 {h(refs_label or '待查')}</p></article>")
        body.append("</section>")
    if result.get("return_leg"):
        returning = result["return_leg"]
        body.append(f"<section><h2>返程</h2><p>预计到站 {h(clock(returning['arrival_at']))}"
                    f" · 预留进站 {h(returning['buffer_min'])} 分钟</p>")
        for lock in result["locked_items"]:
            body.append(f"<p>已锁定安排 · {h(lock['departure_at'])}</p>")
        body.append("</section>")
    body.append("<section><h2>全团费用明细</h2>")
    for line in budget["items"]:
        categories={"hotel":"住宿","meal":"餐饮","transport":"交通","ticket":"门票","shuttle":"接驳"}
        reason = " · 待核实" if line["subtotal_fen"] is None else ""
        units = {"room_night":"房晚","group":"团","person":"人","vehicle":"车"}
        body.append(f"<p>{h(categories.get(line['category'],line['category']))} · {h(line['quantity'])} "
                    f"{h(units.get(line['unit'],line['unit']))} · {h(amount(line['subtotal_fen']))}{reason}</p>")
    body.append("</section>")
    body.append("<section><h2>待核实事项</h2>"+ "".join(f"<p>{h(row)}</p>" for row in result["pending"])+"</section>")
    body.append('<section class="sources"><h2>来源与证据</h2>')
    for row in evidence:
        sample = "模拟资料" if row["data_mode"]=="fixture" else row["publisher"]
        if row["evidence_id"] not in current_refs:
            sample += " · 历史资料"
        link = safe_source(row["source_url"]) if row["data_mode"]!="fixture" else None
        original = f'<p><a href="{h(link)}" rel="noopener noreferrer">打开原来源</a></p>' if link else ""
        days = "、".join(row["applies_to"]) or "适用日期待查"
        body.append(f"<article><h3>{refs[row['evidence_id']]} · {h(sample)}</h3><p>{h(row['excerpt'])}</p>"
                    f"<p>采集 {h(row['collected_at'])} · 适用 {h(days)}</p>{original}</article>")
    body.append("</section>")
    policy = "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'"
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{policy}"><title>贵州旅行路书 r{version.revision}</title>'
            f'<style>{CSS}</style></head><body><main>{"".join(body)}</main></body></html>').encode("utf-8")


class ChromePdf:
    def __init__(self, browser, *, temp_root=None):
        self.browser=Path(browser).resolve()
        self.temp_root=temp_root
        self.slot=BoundedSemaphore(1)

    def __call__(self, html):
        if not self.browser.is_file():
            raise ExportError("browser_unavailable")
        try:
            path=self.temp_root or next((os.getenv(k) for k in ("TEMP","TMP","TMPDIR") if os.getenv(k)),None)
            folder=TemporaryDirectory(prefix="travel-m11-print-",dir=path)
        except OSError:
            raise ExportError("temp_unavailable") from None
        with folder, self.slot:
            work=Path(folder.name)
            inp,out=work/"guidebook.html",work/"guidebook.pdf"
            try:
                inp.write_bytes(html)
            except OSError:
                raise ExportError("temp_unavailable") from None
            env={key:value for key,value in os.environ.items() if key.upper() in
                 {"PATH","SYSTEMROOT","WINDIR","USERPROFILE","LOCALAPPDATA","HOME","LANG"}}
            env.update(TEMP=str(work),TMP=str(work),TMPDIR=str(work))
            try:
                subprocess.run([str(self.browser),"--headless=new","--disable-background-networking",
                    "--disable-component-update","--disable-sync","--no-first-run","--no-default-browser-check",
                    "--host-resolver-rules=MAP * ~NOTFOUND","--proxy-server=http://0.0.0.0:9",
                    "--proxy-bypass-list=<-loopback>","--no-pdf-header-footer",
                    f"--user-data-dir={work/'profile'}",f"--print-to-pdf={out}",inp.as_uri()],
                    check=True,capture_output=True,timeout=32,env=env,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                if out.stat().st_size>MAX_BYTES:
                    raise ExportError("output_too_large")
                data=out.read_bytes()
                if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
                    raise ExportError("invalid_pdf")
                return data
            except (OSError,subprocess.SubprocessError):
                raise ExportError("pdf_renderer_failed") from None


@dataclass(frozen=True)
class Artifact:
    owner: str
    trip_id: str
    revision: int
    expires_at: object
    content_type: str
    content: bytes


class GuidebookService:
    def __init__(self, app, *, pdf=None):
        self.app=app
        self.pdf=pdf
        self.artifacts={}
        self.lock=RLock()

    def clock(self):
        return self.app.clock().astimezone(BEIJING)

    def create(self, token, trip_id, *, revision, format, trace=None):
        check(REQUEST,dict(revision=revision,format=format))
        repo=self.app.repository
        with repo.connection() as db:
            owner=self.app._owner(db,token)
            version=repo.version(db,owner,trip_id,revision)
        html=render_book(version)
        body,kind,reason=html,HTML,None
        if format=="pdf":
            try:
                if not self.pdf:
                    raise ExportError("pdf_disabled")
                body,kind=self.pdf(html),PDF
            except ExportError as exc:
                reason=exc.code
        if len(body)>MAX_BYTES:
            raise ExportError("output_too_large")
        now=self.clock()
        item=Artifact(owner,trip_id,revision,now+timedelta(minutes=10),kind,body)
        ref=uuid4().hex
        with self.lock:
            self.artifacts={k:v for k,v in self.artifacts.items() if v.expires_at>now}
            own=[k for k,v in self.artifacts.items() if v.owner==owner]
            if len(own)>=6:
                self.artifacts.pop(own[0])
            if len(self.artifacts)>=20:
                self.artifacts.pop(next(iter(self.artifacts)))
            self.artifacts[ref]=item
        mode=version.timeline["result"]["data_mode"]
        result=dict(kind="export_result",data_mode=mode,evidence_ids=[],trip_id=trip_id,revision=revision,
                    format=format,status="fallback_available" if reason else "ready",reason=reason,download_url=None,
                    artifact_id=ref,expires_at=format_time(item.expires_at),content_type=kind,
                    pdf_status="failed" if reason else "ready" if format=="pdf" else None,
                    action="PDF 生成失败，可以下载同版本 HTML，或在浏览器打开后打印。" if reason else f"路书 r{revision} 已生成。")
        event(trace,"m11_export_created",requested=format,revision=revision,status=result["status"],reason=reason,trip_writes=0)
        return reply(result,now,"partial" if reason else "completed",fixture=mode=="fixture")

    def download(self,token,ref,*,trace=None):
        repo=self.app.repository
        with repo.connection() as db, self.lock:
            owner=self.app._owner(db,token)
            item=self.artifacts.get(ref)
            if item is not None and item.expires_at<=self.clock():
                self.artifacts.pop(ref)
                item=None
            if item is None or item.owner!=owner:
                event(trace,"m11_export_denied",reason="missing_or_not_owned")
                raise NotFound()
            repo.version(db,owner,item.trip_id,item.revision)
        return item
