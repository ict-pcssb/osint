"""Local Guizhou chat runtime. The hosting application owns each Conversation."""
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

import httpx2

ROOT = Path(__file__).resolve().parent
OLD = ROOT.parent / "samples-1124"
CHINA = timezone(timedelta(hours=8))
MODEL = "qwen4-max"
PROMPT = (ROOT / "提示词" / "旅游助手系统提示词.md").read_text(encoding="utf-8").strip()
WEATHER = {"type":"function","function":{"name":"get_destination_weather",
    "description":"按贵州景点名称和北京时间日期查询实时来源的天气预报。",
    "parameters":{"type":"object","properties":{"place":{"type":"string","description":"从游客消息或会话历史解析的景点全名"},
    "day":{"type":"string","description":"YYYY-MM-DD"}},"required":["place","day"],"additionalProperties":False}}}


def endpoint():
    base = os.getenv("DASHSCOPE_BASE_URL")
    if not base and (OLD / ".env").is_file():
        for line in (OLD / ".env").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("DASHSCOPE_BASE_URL="):
                base = line.partition("=")[2].strip().strip("\"'")
                break
    base = (base or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    if not re.fullmatch(r"https://(?:dashscope\.aliyuncs\.com|[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com)/compatible-mode/v1", base):
        raise ValueError("请核对百炼北京兼容接口地址")
    return base + "/chat/completions"


async def complete(messages, *, structured=False):
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")
    payload = {"model": MODEL, "messages": messages, "stream": False}
    if structured:
        payload["response_format"] = {"type": "json_object"}
        payload["enable_thinking"] = False
    else:
        payload.update(tools=[WEATHER], tool_choice="auto")
    async with httpx2.AsyncClient(timeout=65) as client:
        response = await client.post(endpoint(), headers={"Authorization": "Bearer " + key}, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


async def weather(place, day):
    target = date.fromisoformat(day)
    now = datetime.now(CHINA)
    if not 0 <= (target-now.date()).days < 16:
        return {"status":"unconfirmed","reason":"日期超出预报窗口"}
    catalog = json.loads((OLD/"static/travel/data/pois.json").read_text(encoding="utf-8"))
    matches = [p for p in catalog if p["name"] == place or p["poi_id"] == place]
    if len(matches) != 1:
        return {"status":"unconfirmed","reason":"请确认景点全名"}
    point = matches[0]["coordinates"]
    metrics = ["temperature_2m_min","temperature_2m_max","precipitation_probability_max"]
    async with httpx2.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast",params={
            "latitude":point["latitude"],"longitude":point["longitude"],"timezone":"Asia/Shanghai",
            "start_date":day,"end_date":day,"daily":",".join(metrics)})
        r.raise_for_status()
        data = r.json()
    if data["daily"]["time"] != [day] or any(data["daily"][k][0] is None for k in metrics):
        return {"status":"unconfirmed","reason":"目标日期预报不完整"}
    return {"status":"ok","place":matches[0]["name"],"county":matches[0]["county"],"date":day,
        "values":{k:data["daily"][k][0] for k in metrics},"units":data["daily_units"],
        "source":"Open-Meteo","checked_at":now.isoformat(timespec="seconds")}


async def classify_intent(message, now, known, repair, *, completion=None):
    from travel_contract import INTENT
    from travel_routing import travel_statement
    instruction = """你在生成意图路由 JSON，由服务端执行后续逻辑。
输出且只输出一个 JSON 对象，四个必填字段 intent、confidence_band、requested_outputs、parameters。
先区分陈述与请求。utterance_form 为 travel_statement 时是已明确识别的出行陈述，没有请求，必须输出 {"intent":"ack","confidence_band":"high","requested_outputs":["ack"],"parameters":{}}。
requested_outputs 列出用户明确要求的结果，按主次顺序排列，第一项等于 intent。缺条件时仍返回该业务意图，把未知参数省略。不要输出 null、解释、自造字段、账户或行程记录 ID。
用途：
- weather 查天气；询问具体时段才写 hourly=true。
- verification 核对景区开放、公告或预约事实。
- transfer 查接驳、交通路径和末班。识别实际出发点和到达点，保留站点与入口的区别。
- cost_breakdown 问费用、门票或全团多少钱。景点写 place_id，成人和儿童各写人数（adults、children）。
- candidates 查候选，candidate_kind 为 taxi、hotel、dining、train、flight 或 intercity；只写明确条件。住宿入住日用 check_in（YYYY-MM-DD），房间数 rooms，连住晚数 nights，住客数 adults。已知上轮为 hotel 且只改住宿条件时，仍写 hotel 和变化字段。
- itinerary 规划行程时间轴；日期写 start_date，城市写 destination，人数写 adults，天数写 days，全团预算用 group_cap_fen（人民币分）。
- patch_preview 预览已保存行程的局部调整；返程、酒店等锁定安排由服务端读取和校验。
- recheck_report 复查已保存行程；export_result 导出路书，format 小写 html 或 pdf，未指定用 html。
参数仅来自明确输入。没有标准地点 ID 时可写用户给出的准确地名，由服务端匹配已登记目录；不要造 ID。贵阳北站为 guiyang-north，青岩北门停车场为 qingyan-north-parking。
日期按给出的北京时间，使用 YYYY-MM-DD，住宿的 check_in 也必须是该格式。多项都明确时保留所有请求，不增添旅游建议。
"""
    if repair:
        instruction += "上轮未通过校验。只返回定义的四个字段，省略一切无关参数。"
    messages = [
        {"role": "system", "content": instruction + "\nJSON Schema: " +
         json.dumps(INTENT.schema, ensure_ascii=False, separators=(",", ":"))},
        {"role": "user", "content": json.dumps({"message": message, "time": now.isoformat(), "known": known,
                                               "utterance_form": "travel_statement" if travel_statement(message) else "undetermined"}, ensure_ascii=False)},
    ]
    try:
        result = await (completion or complete)(messages, structured=True)
        return result.get("content")
    except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("intent_model_unavailable") from exc


class Conversation:
    def __init__(self, homepage=None, preferences=None, *, typed_output=False, intent_model=None,
                 handlers=None, clock=None, fixture_mode=False, timeline_handler=None, verification_handler=None,
                 transport_skills=None, intent_timeout=15):
        self.homepage = dict(homepage or {})
        self.preferences = dict(preferences or {})
        self.turns = []
        self.clock = clock or (lambda: datetime.now(CHINA))
        self.typed = None
        if typed_output:
            from travel_routing import TypedTurn
            if transport_skills is None and not fixture_mode and os.getenv("TRAVEL_M07_NETWORK")=="1":
                from travel_transport_live import configured_skills
                transport_skills = configured_skills(self.clock)
            self.typed = TypedTurn(intent_model or classify_intent, handlers, fixture_mode, timeline_handler=timeline_handler,
                                   verification_handler=verification_handler, transport_skills=transport_skills,
                                   intent_timeout=intent_timeout)

    async def ask(self, text, trace=None, *, candidate_skills=None, candidate_scope=None, patch_handler=None, recheck_handler=None, export_handler=None):
        now = self.clock().astimezone(CHINA)
        if self.typed is not None:
            return await self.typed.run(text, self.homepage, now, trace, candidate_skills=candidate_skills,
                                        candidate_scope=candidate_scope,patch_handler=patch_handler,recheck_handler=recheck_handler,export_handler=export_handler)
        context = {"today":now.date().isoformat(),"weekday":now.isoweekday(),
                   "timezone":"Asia/Shanghai","homepage":self.homepage,"preferences":self.preferences}
        messages = [{"role":"system","content":PROMPT},
                    {"role":"user","content":"应用提供的上下文数据："+json.dumps(context,ensure_ascii=False)}]
        for turn in self.turns[-8:]:
            messages.extend(turn)
        turn = [{"role":"user","content":text}]
        for _ in range(4):
            if trace is not None:
                trace.append({"model":MODEL,"messages":list(messages+turn),"tools":[WEATHER],"tool_choice":"auto"})
            reply = await complete(messages+turn)
            calls = reply.get("tool_calls") or []
            entry = {"role":"assistant","content":reply.get("content")}
            if calls:
                entry["tool_calls"] = calls
            turn.append(entry)
            if not calls:
                if not entry["content"]:
                    raise RuntimeError("模型没有返回答复")
                self.turns = (self.turns+[turn])[-8:]
                return entry["content"]
            for call in calls:
                fn = call["function"]
                try:
                    if fn["name"] != "get_destination_weather":
                        raise ValueError("unknown tool")
                    result = await weather(**json.loads(fn["arguments"]))
                except (ValueError, TypeError, KeyError, IndexError, httpx2.HTTPError):
                    result = {"status":"unconfirmed","reason":"天气参数无效或供应商暂时不可用"}
                turn.append({"role":"tool","tool_call_id":call["id"],"content":json.dumps(result,ensure_ascii=False)})
        raise RuntimeError("工具调用超过本轮限制")


async def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--qingyan-test", action="store_true")
    mode.add_argument("--typed-output", action="store_true", help="结构化输出预览，旅行供应商未启用")
    mode.add_argument("--state-db", type=Path, help="启用 M3 本地账号和行程，显式选择 SQLite 文件")
    parser.add_argument("--create-account", action="store_true", help="在指定本地库创建账号")
    args = parser.parse_args()
    if args.create_account and args.state_db is None:
        parser.error("--create-account requires --state-db")
    if args.state_db is not None:
        from travel_state_cli import run_console
        try:
            await run_console(args.state_db, create_account=args.create_account)
        except (PermissionError, ValueError, RuntimeError) as exc:
            parser.exit(1, f"{exc}\n")
        return
    agent = Conversation(typed_output=args.typed_output)
    if args.qingyan_test:
        trace = []
        for question in ["我们两个人，周六去青岩古镇。","那天会下雨吗？"]:
            print(question, await agent.ask(question,trace))
        print(json.dumps(trace,ensure_ascii=False,indent=2))
        last = agent.turns[-1]
        assert any(c["function"]["name"]=="get_destination_weather" for m in last for c in m.get("tool_calls",[]))
        today = datetime.now(CHINA).date()
        saturday = str(today+timedelta(days=(5-today.weekday())%7))
        assert any(d.get("status")=="ok" and d.get("date")==saturday and d.get("place")=="青岩古镇"
                   for m in last if m["role"]=="tool" for d in [json.loads(m["content"])])
    else:
        while text := input("游客（回车结束）> ").strip():
            reply = await agent.ask(text)
            print(json.dumps(reply, ensure_ascii=False) if args.typed_output else reply)

if __name__ == "__main__":
    asyncio.run(main())
