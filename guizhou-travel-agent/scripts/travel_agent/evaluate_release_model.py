"""M12 intent evaluation. The only possible external call in model mode is the approved main model."""
import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from hashlib import sha256
import json
from math import ceil
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"tests/travel_agent")]
from support.guards import offline_guard
from travel_agent import MODEL, Conversation, classify_intent, endpoint
from travel_contract import ContractError, decode_intent
from travel_radeon import RADEON_ENDPOINT, RADEON_MODEL, radeon_response_format, radeon_schema
from travel_release import Measure, RunBudget, RunLimit, RunTrace, check_policy
from travel_routing import SKILLS

FIX=ROOT/"tests/travel_agent/fixtures"
SAMPLES=json.loads((FIX/"m02/replies.json").read_text(encoding="utf-8"))
DATA=FIX/"model_eval/cases.json"
FROZEN_THRESHOLDS={"intent_delivery":.98,"required_parameters":.99,"first_json":.99,"false_confirmations":0}
MODEL_PROFILES = {"dashscope": (MODEL, "DASHSCOPE_API_KEY"), "radeon": (RADEON_MODEL, "RADEON_API_KEY")}
EPISODE_USAGE = ContextVar('episode_usage', default=None)
INTERNAL={"trip_id","base_revision","locked_ids","scope_date","scenic_area"}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def provider_cost(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and 0 <= amount <= 1_000_000:
            return amount
    except (InvalidOperation, ValueError):
        pass
    return None


def provider_retry_delay(status, retry_after):
    if status in {500, 502, 503, 504}:
        return 3
    if status != 429:
        return None
    try:
        delay = float(retry_after) if retry_after is not None else 6
    except (TypeError, ValueError):
        return 6
    return max(1, delay) if 0 <= delay <= 90 else None


class TransientModelIssue(RuntimeError):
    def __init__(self, delay):
        self.delay = delay
        super().__init__("temporary_model_transport")


class MeteredModel:
    def __init__(self, policy, provider="dashscope", rpm_limit=None):
        self.policy=policy
        self.provider=provider
        self.model,self.env_key=MODEL_PROFILES[provider]
        self.intent_timeout=145 if provider=="radeon" else 15
        self.request_timeout=40 if provider=="radeon" else 14
        self.rpm_limit=rpm_limit
        self.client=None
        self.request_lock=asyncio.Lock()
        self.next_slot=0.0
        self.budget=RunBudget(attempts=policy["max_attempts"],seconds=policy["max_seconds"],cost_micros=policy["cap_micros"])
        self.rows=[]
        self.stop_reason=None

    def price(self,inputs,outputs):
        p=self.policy
        return ceil((inputs*p["input_micros_per_million"]+outputs*p["output_micros_per_million"])/1_000_000)

    async def pace(self):
        async with self.request_lock:
            while (delay := self.next_slot - monotonic()) > 0:
                if delay >= self.budget.remaining():
                    raise RunLimit("deadline")
                await asyncio.sleep(delay)
            if self.rpm_limit:
                self.next_slot = monotonic() + 60 / self.rpm_limit

    async def complete(self,messages,*,structured):
        import httpx2

        body = dict(model=self.model, messages=messages, stream=False)
        if self.provider == "radeon":
            body.update(response_format=radeon_response_format(), temperature=0,
                        max_tokens=self.policy["max_output_tokens"])
        else:
            body.update(enable_thinking=False, response_format={"type":"json_object"},
                        max_completion_tokens=self.policy["max_output_tokens"])

        if self.provider == "radeon":
            for attempt in range(2):
                try:
                    return await self.send(body)
                except (TransientModelIssue, httpx2.TransportError) as error:
                    delay = error.delay if isinstance(error, TransientModelIssue) else 2
                    self.next_slot = max(self.next_slot, monotonic()+delay)
                    if attempt:
                        raise RuntimeError("model_transport_retry_exhausted") from error
        return await self.send(body)

    async def send(self, body):
        import httpx2

        if datetime.now(timezone.utc)>=datetime.fromisoformat(self.policy["expires_at"]):
            self.stop_reason = "expired_policy"
            raise RunLimit(self.stop_reason)
        conservative_inputs=len(json.dumps(body,ensure_ascii=False).encode("utf-8"))+1024
        reserved=self.price(conservative_inputs,self.policy["max_output_tokens"])
        try:
            await self.pace()
            if self.stop_reason:
                raise RunLimit(self.stop_reason)
            ref=self.budget.reserve(self.model,reserved)
        except RunLimit as exc:
            self.stop_reason=str(exc)
            raise
        row=dict(attempt=ref,completion_tokens=None,prompt_tokens=None,cost_micros=None,status="failed")
        self.rows.append(row)
        if (usage := EPISODE_USAGE.get()) is not None:
            usage.append(row)
        start=monotonic()
        try:
            url = RADEON_ENDPOINT if self.provider == "radeon" else endpoint()
            request = dict(headers={"Authorization":"Bearer "+os.environ[self.env_key]}, json=body,
                           timeout=min(self.request_timeout, self.budget.remaining()))
            if self.client is None:
                async with httpx2.AsyncClient(follow_redirects=False) as client:
                    response=await client.post(url, **request)
            else:
                response=await self.client.post(url, **request)
            row["http_status"]=response.status_code
            retry = provider_retry_delay(response.status_code, response.headers.get("Retry-After"))
            if self.provider == "radeon" and retry is not None:
                row["retry_after_seconds"] = retry
                raise TransientModelIssue(retry)
            if response.status_code in {400,401,402,403,404,429}:
                self.stop_reason=f"model_http_{response.status_code}"
            response.raise_for_status()
            payload=response.json()
            usage=payload.get("usage",{})
            inputs,outputs=usage.get("prompt_tokens"),usage.get("completion_tokens")
            if type(inputs) is int and type(outputs) is int and inputs>=0 and outputs>=0:
                row.update(prompt_tokens=inputs,completion_tokens=outputs)
                if self.provider != "radeon":
                    row["cost_micros"] = self.price(inputs,outputs)
            if self.provider == "radeon":
                amount = provider_cost(usage.get("cost"))
                if amount is not None:
                    row.update(provider_reported_cost=str(amount),
                               cost_micros=int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING)))
                details = usage.get("prompt_tokens_details") or {}
                row["cached_tokens"] = details.get("cached_tokens")
                row["reasoning_tokens"] = usage.get("reasoning_tokens")
            choice = payload["choices"][0]
            row["finish_reason"] = choice.get("finish_reason")
            row["status"]="received"
            return choice["message"]
        except (httpx2.HTTPError, TimeoutError, TransientModelIssue, asyncio.CancelledError) as error:
            row["transport_error"] = type(error).__name__
            raise
        finally:
            row["elapsed_ms"]=round((monotonic()-start)*1000)
            self.budget.finish(ref,row["cost_micros"])

    async def intent(self,message,now,known,repair):
        return await asyncio.wait_for(classify_intent(message,now,known,repair,completion=self.complete),
                                      timeout=min(self.intent_timeout,self.budget.remaining()))


def packets(case,now):
    base={name:deepcopy(packet) for name,packet in SAMPLES.items()}
    expected=case["expected_kind"]
    # In the shortlist replay, absent vs reprinted material has the same safe conclusion.
    for kind in ("recheck_report","export_result"):
        extra=(dict(findings=[]) if kind=="recheck_report" else
               dict(revision=7,format="html",status="unavailable",action="未申请真实导出。",download_url=None,reason="unbound"))
        if kind=="recheck_report":
            extra["base_revision"]=7
        base[kind]=dict(schema_version="1.0",trace_id=uuid4().hex,generated_at=now.isoformat(),
           timezone="Asia/Shanghai",run_status="completed",persistence="none",evidence=[],
           result=dict(kind=kind,trip_id="trip-A",data_mode="fixture",evidence_ids=[],**extra))
    if case["negative_reason"]=="reprints":
        base["verification"]["evidence"]=[
            dict(evidence_id=f"m12-reprint-{i}",source_url=f"fixture://m12-reprint-{i}",publisher="模拟同源文章",
                 source_group_id="m12-reprinted-notice",collected_at=now.isoformat(),applies_to=[],
                 source_kind="fixture",data_mode="fixture",excerpt="同源转载，无法确认目标日。")
            for i in (1,2)]
    if expected=="bundle":
        # Force only the weather adapter to return its existing bounded unavailable response.
        base.pop("weather")
    return base


def supplied_decision(case):
    parameters={key:value for key,value in case["resolved_parameters"].items() if key not in INTERNAL}
    return dict(intent=case["expected_intent"],confidence_band="high",
                requested_outputs=["weather","transfer"] if case["expected_kind"]=="bundle" else [case["expected_intent"]],
                parameters=parameters)


async def episode(case,now,trial,model=None):
    record, attempts, parsed, decode_errors = RunTrace(),[],[],[]
    start=monotonic()
    raw_usage=[]
    marker=EPISODE_USAGE.set(raw_usage)
    expected=case["expected_kind"]
    data=packets(case,now)

    def available_handler(kind,tool):
        async def handle(arguments):
            attempts.append((tool,dict(arguments)))
            record.record("fixture_adapter",tool=tool,arguments=arguments,status="success" if kind in data else "timeout")
            if kind not in data:
                raise TimeoutError
            return data[kind]
        return handle

    async def decide(message,instant,known,repair):
        raw=await model.intent(message,instant,known,repair) if model else json.dumps(supplied_decision(case),ensure_ascii=False)
        try:
            parsed.append(decode_intent(raw))
        except ContractError as error:
            parsed.append(None)
            cause = error.__cause__
            fields = list(cause.absolute_path) if hasattr(cause, "absolute_path") else None
            decode_errors.append({"reason":str(error), "field_path":fields,
                                  "rule":getattr(cause, "validator", None)})
        else:
            decode_errors.append(None)
        return raw

    known={k:v for k,v in case["resolved_parameters"].items() if k in INTERNAL}
    if expected in {"patch_preview","itinerary"}:
        # Saved-trip dates and itinerary lock anchors are trusted, not supplied by the model.
        known.update(start_date="2026-11-28")
    if expected=="clarification":
        known=dict(date="2026-11-27")
    handlers={skill.tool:available_handler(kind,skill.tool) for kind,skill in SKILLS.items()}
    conv=Conversation(homepage=known,typed_output=True,intent_model=decide,handlers=handlers,clock=lambda:now,fixture_mode=True,
                      intent_timeout=model.intent_timeout if model else 15)
    error=None
    try:
        packet=await conv.ask(case["message"],record)
        if not parsed and packet["result"]["kind"]=="ack":
            # Router fast path can bypass the model, but the benchmark still measures its decision.
            await decide(case["message"],now,known,False)
    except (ValueError,OSError,RuntimeError,TimeoutError) as issue:
        error=type(issue).__name__
        packet=dict(result={"kind":"unavailable"},persistence="none")
    result=packet["result"]
    delivered=result["kind"]
    intent=next((row["intent"] for row in reversed(parsed) if row),None)
    required=case["expected_tools"]
    seen=[tool for tool,_ in attempts]
    valid_tools=0
    for tool in required:
        actual=next((args for name,args in attempts if name==tool),None)
        if actual is None:
            continue
        kind=next(k for k,skill in SKILLS.items() if skill.tool==tool)
        from travel_routing import CANDIDATE_FIELDS, ROUTE_FIELDS
        allowed=set(SKILLS[kind].required)|set(ROUTE_FIELDS.get(kind,()))
        if kind=="candidates":
            allowed.update(CANDIDATE_FIELDS.get(actual.get("candidate_kind"),()))
        expected_params={k:v for k,v in case["resolved_parameters"].items() if k in allowed}
        if all(actual.get(k)==v for k,v in expected_params.items()):
            valid_tools+=1
    if expected=="verification":
        safe=delivered=="verification" and result.get("status")=="unconfirmed"
    elif expected=="transfer":
        safe=delivered=="transfer" and result.get("last_service",{}).get("state")=="unknown"
    elif expected=="cost_breakdown":
        safe=delivered=="cost_breakdown" and result.get("total_fen") is None
    else:
        safe=True
    required_kinds=["weather","transfer"] if expected=="bundle" else [case["expected_intent"]]
    model_shapes=any(p and p["requested_outputs"]==required_kinds for p in parsed)
    bundle_delivered=(delivered!="bundle" or (result["primary"]["kind"]=="weather_unavailable"
                      and [item["kind"] for item in result["supplements"]]==["transfer"]))
    extra=sum((Counter(seen)-Counter(required)).values())
    no_write=packet["persistence"]=="none" and all(row.get("trip_writes",0)==0 and row.get("write_count",0)==0
                                                   for row in record.events)
    EPISODE_USAGE.reset(marker)
    return dict(case_id=case["case_id"],family=case["family"],trial=trial,expected_kind=expected,intent=intent,
       delivered=delivered,first_json=bool(parsed and parsed[0] is not None),
       repaired=bool(len(parsed)>1 and parsed[-1] is not None),
       joint_ok=delivered==expected and intent==case["expected_intent"] and model_shapes and bundle_delivered,
       required_tools=len(required),correct_tools=valid_tools,extra_tools=extra,
       negative=case["negative"],safe=safe,no_trip_writes=no_write,elapsed_ms=round((monotonic()-start)*1000),
       cost_micros=(sum(row["cost_micros"] for row in raw_usage) if all(row["cost_micros"] is not None for row in raw_usage) else None)
           if model else 0,attempts=len(raw_usage),retries=sum(bool(x) for x in raw_usage[1:]),
       fixture_tool_attempts=len(seen),cache_hits=sum(bool(row.get("cached_tokens")) for row in raw_usage),
       usage=raw_usage,error=error,decode_errors=decode_errors,trace_id=record.trace_id,trace=record.events)


async def run(data,model):
    now=datetime.fromisoformat(data["clock"])
    pending = [(trial, case) for trial in range(1,data["repeats"]+1) for case in data["cases"]]
    if not model:
        return [await episode(case,now,trial) for trial,case in pending]

    rows = [None] * len(pending)
    numbered = iter(enumerate(pending))
    completed = 0

    async def worker():
        nonlocal completed
        while not model.stop_reason and model.budget.remaining() and model.budget.attempts < model.budget.limit:
            item = next(numbered, None)
            if item is None:
                return
            index, (trial, case) = item
            rows[index] = await episode(case,now,trial,model)
            completed += 1
            if completed % 12 == 0:
                print(f"Model progress {completed}/{len(pending)}", flush=True)

    import httpx2
    async with httpx2.AsyncClient(follow_redirects=False, limits=httpx2.Limits(
            max_connections=4, max_keepalive_connections=4, keepalive_expiry=25)) as client:
        model.client = client
        try:
            await asyncio.gather(*(worker() for _ in range(4)))
        finally:
            model.client = None
    return [row for row in rows if row is not None]


def summarize(data,rows):
    planned=len(data["cases"])*data["repeats"]
    required=sum(len(case["expected_tools"]) for case in data["cases"])*data["repeats"]
    negatives=sum(case["negative"] for case in data["cases"])*data["repeats"]
    return dict(planned=planned,executed=len(rows),joint_pass=sum(row["joint_ok"] for row in rows),
                first_json_pass=sum(row["first_json"] for row in rows),repairs_pass=sum(row["repaired"] for row in rows),
                required_tool_parameters=required,correct_tool_parameters=sum(row["correct_tools"] for row in rows),
                extra_tool_calls=sum(row["extra_tools"] for row in rows),negative_planned=negatives,
                negative_executed=sum(row["negative"] for row in rows),
                negative_safe=sum(row["negative"] and row["safe"] for row in rows),
                no_write_pass=sum(row["no_trip_writes"] for row in rows),by_family=Measure.summary(rows))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("fixture","model"),required=True)
    parser.add_argument("--policy",type=Path)
    parser.add_argument("--provider", choices=MODEL_PROFILES, default="dashscope")
    parser.add_argument("--rpm-limit", type=int, help="Pace all actual model attempts, including repairs")
    parser.add_argument("--approved-frozen-labels",action="store_true")
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    if args.rpm_limit is not None and not 1 <= args.rpm_limit <= 120:
        parser.error("--rpm-limit must be between 1 and 120")
    model=None
    data=load(DATA)
    model_name, env_key = MODEL_PROFILES[args.provider]
    report=dict(layer="fixed_no_network" if args.mode=="fixture" else "real_model_fixture_tools",
      dataset_version=data["dataset_version"],label_version=data["label_version"],dataset_sha256=digest(DATA),
      proposed_thresholds=FROZEN_THRESHOLDS,thresholds_approved=args.approved_frozen_labels,model=model_name if args.mode=="model" else None,
      model_provider=args.provider if args.mode=="model" else None,
      intent_schema_sha256=digest(ROOT/"contracts/travel_agent/v1/intent.schema.json"),
      cost_unit=("radeon_provider_units" if args.provider=="radeon" else "CNY") if args.mode=="model" else None)
    failures=[]
    if args.mode=="model":
        if not os.getenv(env_key):
            failures.append("missing_current_model_key")
        if not args.approved_frozen_labels:
            failures.append("frozen_labels_and_thresholds_not_approved")
        if args.policy is None:
            failures.append("missing_approved_model_budget")
        else:
            try:
                policy=check_policy(load(args.policy),datetime.now(timezone.utc),model_name)
            except (OSError,RunLimit,ValueError):
                failures.append("invalid_or_expired_model_policy")
        if not failures:
            model=MeteredModel(policy,args.provider,args.rpm_limit)
    if failures:
        rows=[]
    elif args.mode=="fixture":
        with offline_guard(RunTrace()):
            rows=asyncio.run(run(data,None))
    else:
        rows=asyncio.run(run(data,model))
    summary=summarize(data,rows)
    safe=summary["negative_safe"]==summary["negative_planned"] and summary["no_write_pass"]==summary["planned"] and not summary["extra_tool_calls"]
    metrics=(summary["joint_pass"]>=summary["planned"]*FROZEN_THRESHOLDS["intent_delivery"]
           and summary["first_json_pass"]>=summary["planned"]*FROZEN_THRESHOLDS["first_json"]
           and summary["correct_tool_parameters"]>=summary["required_tool_parameters"]*FROZEN_THRESHOLDS["required_parameters"])
    result="BLOCKED" if failures or summary["executed"]<summary["planned"] else "PASS" if safe and metrics else "FAIL"
    if model and summary["executed"]<summary["planned"]:
        failures.append(model.stop_reason or "approved_call_or_time_budget_exhausted")
    args.report.parent.mkdir(parents=True,exist_ok=True)
    report.update(status=result,summary=summary,blockers=failures,cases=rows,
                  actual_metered_model_attempts=model.budget.attempts if model else 0,
                  model_intent_timeout_seconds=model.intent_timeout if model else None,
                  provider_request_timeout_seconds=model.request_timeout if model else None,
                  requested_pacing_rpm=model.rpm_limit if model else None)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":result,"summary":summary,"blockers":failures},ensure_ascii=False))
    return {"PASS":0,"FAIL":1,"BLOCKED":2}[result]


if __name__=="__main__":
    raise SystemExit(main())
