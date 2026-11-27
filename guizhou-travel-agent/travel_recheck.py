"""Read-only rechecks against an owned, immutable trip revision."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from travel_contract import ContractError, check, validate_reply, _validator
from travel_patch import fingerprint
from travel_recheck_facts import find_facts, freshness, review_item, normalize_observation
from travel_rules import format_time, instant
from travel_state import BEIJING
from travel_state_app import NotFound
from travel_transport import event
from travel_transport_skills import reply

CONTRACT = _validator("recheck")
MAX_CALLS = 6
MAX_SECONDS = 45
REUSE_MINUTES = 15


def validate(kind, data):
    return check(CONTRACT.evolve(schema={"$defs":CONTRACT.schema["$defs"],"$ref":"#/$defs/"+kind}), data)


class RecheckError(ValueError):
    def __init__(self, code, status):
        self.code = code
        self.status = status
        super().__init__({
            "version_conflict":"原行程版本已变化，请重新核验并预览。",
            "no_suggestion":"当前没有可预览的替换方案。",
            "disabled":"打开时自动核查已关闭，可手动刷新。",
        }.get(code,"核查请求无效。"))


class CallLimit(Exception):
    pass


class Budget:
    def __init__(self, stopped, deadline):
        self.stopped = stopped
        self.deadline = deadline
        self.calls = []
        self.lock = Lock()

    def take(self, provider):
        with self.lock:
            if self.stopped.is_set() or monotonic()>=self.deadline or len(self.calls)>=MAX_CALLS:
                raise CallLimit()
            self.calls.append(provider)


class RecheckService:
    def __init__(self, app, providers, *, auto_open=True, timeout=MAX_SECONDS, item_timeout=25):
        self.app, self.providers, self.auto_open = app, dict(providers), auto_open
        self.timeout, self.item_timeout = min(timeout, MAX_SECONDS), min(item_timeout, 25)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="travel-recheck")
        self.stopped = Event()
        self.lock = Lock()
        self.jobs = {}

    def clock(self):
        return self.app.clock().astimezone(BEIJING)

    def close(self):
        self.stopped.set()
        self.executor.shutdown(wait=True)

    def open(self, token, trip_id, *, trace=None):
        version = self.app.read_trip(token, trip_id)
        now = self.clock()
        facts = find_facts(version, now)
        opened = dict(trip_id=trip_id, revision=version.revision, schema_version=version.schema_version,
                      timeline=version.timeline, auto_recheck=self.auto_open,
                      freshness=[dict(fact_id=f["fact_id"], stop_id=f["stop_id"], kind=f["kind"],
                                      target_date=f["target_date"], data_freshness=freshness(f,now),
                                      collected_at=f["previous"]["collected_at"],
                                      valid_until=f["previous"]["valid_until"]) for f in facts])
        event(trace,"m10_opened",revision=version.revision,fact_count=len(facts),provider_calls=0,trip_writes=0)
        return opened

    def start(self, token, trip_id, *, base_revision, trigger, trace=None):
        validate("start",{"base_revision":base_revision,"trigger":trigger})
        if self.stopped.is_set() or (trigger=="open" and not self.auto_open):
            raise RecheckError("disabled",503)
        repo = self.app.repository
        now = self.clock()
        checked = format_time(now)
        created = False
        deadline=monotonic()+self.timeout
        with self.lock:
            with repo.transaction() as db:
                owner = self.app._owner(db,token)
                version = repo.version(db,owner,trip_id)
                if version.revision!=base_revision:
                    raise RecheckError("version_conflict",409)
                facts = find_facts(version,now)
                key = fingerprint([fact["fact_id"] for fact in facts])
                old = db.execute("""SELECT * FROM recheck_runs WHERE owner_id=? AND trip_id=? AND base_revision=?
                                    AND fact_key=? AND reusable_until>? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                                 (owner,trip_id,base_revision,key,checked)).fetchone()
                if old and (old["status"]=="running" or trigger!="manual"):
                    run_id = old["run_id"]
                else:
                    history = db.execute("""SELECT result_json FROM recheck_runs WHERE owner_id=? AND trip_id=?
                                            AND base_revision=? AND status='done' ORDER BY rowid DESC LIMIT 8""",
                                         (owner,trip_id,base_revision)).fetchall() if trigger!="manual" else []
                    recent = {}
                    for stored in history:
                        for item in json.loads(stored["result_json"])["station_changes"]:
                            if item["latest"]:
                                recent.setdefault(item["fact_id"],item["latest"])
                    active = db.execute("""SELECT count(*) FROM recheck_runs
                                           WHERE status='running' AND reusable_until>?""",(checked,)).fetchone()[0]
                    if active>=8:
                        raise RecheckError("busy",429)
                    run_id = uuid4().hex
                    db.execute("""INSERT INTO recheck_runs
                                  (run_id,owner_id,trip_id,base_revision,fact_key,status,created_at,reusable_until,trigger)
                                  VALUES (?,?,?,?,?,'running',?,?,?)""",
                               (run_id,owner,trip_id,base_revision,key,checked,
                                format_time(now+timedelta(seconds=MAX_SECONDS+5)),trigger))
                    created = True
            if created:
                self.jobs = {key:job for key,job in self.jobs.items() if not job.done()}
                self.jobs[run_id] = self.executor.submit(self.run,run_id,version,facts,recent,deadline,trace)
        event(trace,"m10_task_started" if created else "m10_task_reused",run_id=run_id,
              base_revision=base_revision,fact_count=len(facts),trip_writes=0)
        return self.get(token,trip_id,run_id)

    def wait(self, run_id, *, timeout=MAX_SECONDS+5):
        with self.lock:
            future = self.jobs.get(run_id)
        if future:
            future.result(timeout=timeout)

    async def collect(self, facts, recent, budget, deadline):
        semaphore = asyncio.Semaphore(3)
        obtained = {}
        started = self.clock()

        async def one(fact):
            key = fact["fact_id"]
            if freshness(fact,started)=="current":
                obtained[key] = review_item(fact,started,None,"reused")
                return
            if key in recent:
                cached,_ = normalize_observation(recent[key],fact,self.clock())
                if cached:
                    obtained[key] = review_item(fact,self.clock(),cached,"reused")
                    return
            provider = self.providers.get(fact["kind"])
            if provider is None or self.stopped.is_set():
                obtained[key] = review_item(fact,self.clock(),None,"unbound" if provider is None else "cancelled")
                return
            async with semaphore:
                latest = None
                try:
                    outcome = await asyncio.wait_for(provider(deepcopy(fact),budget),timeout=self.item_timeout)
                    if outcome["status"]=="success":
                        latest, reason = normalize_observation(outcome["observation"],fact,self.clock())
                    else:
                        reason = outcome["status"]
                except TimeoutError:
                    reason = "timeout"
                except CallLimit:
                    reason = "cancelled" if self.stopped.is_set() else "timeout" if monotonic()>=deadline else "budget_exhausted"
                except (ContractError,ValueError,KeyError,TypeError):
                    reason = "invalid_response"
                except (OSError,RuntimeError):
                    reason = "provider_error"
                obtained[key] = review_item(fact,self.clock(),latest,reason)

        try:
            await asyncio.wait_for(asyncio.gather(*(one(fact) for fact in facts)),timeout=max(0,deadline-monotonic()))
        except TimeoutError:
            pass
        return [obtained.get(fact["fact_id"]) or review_item(fact,self.clock(),None,"timeout") for fact in facts]

    def run(self, run_id, version, facts, recent, deadline, trace):
        budget = Budget(self.stopped,deadline)
        items = asyncio.run(self.collect(facts,recent,budget,deadline))
        pending = [dict(fact_id=row["fact_id"],stop_id=row["stop_id"],reason=row["reason"])
                   for row in items if row["status"]=="pending"]
        changed = [row for row in items if row["status"] in {"changed","conflict"}]
        successes = len(items)-len(pending)
        state = ("partial" if successes else "unavailable") if pending else "changed" if changed else "unchanged"
        now = self.clock()
        mode = version.timeline["result"]["data_mode"] if version.timeline else "snapshot"
        # Preserve safe suggestions as decisions only. M9 owns all schedule validation and saves.
        valid_stops = {s["stop_id"]:s for day in version.timeline["result"]["days"] for s in day["stops"]} if version.timeline else {}
        candidate = next((row for row in changed if row["kind"]=="opening" and row["stop_id"]=="d2-park"
                          and row["latest"]["value"] in {"open","closed"} and row["stop_id"] in valid_stops
                          and not any(valid_stops[row["stop_id"]].get(key) for key in ("completed","locked","required"))),None)
        decision_key = fingerprint([{"id":row["fact_id"], "value":row["latest"]["value"],
                                     "effective":row["effective_date"]} for row in changed]) if changed else None
        data = dict(run_id=run_id,trip_id=version.trip_id,base_revision=version.revision,current_revision=version.revision,
                    data_mode=mode,run_status="partial" if pending else "completed",check_state=state,
                    checked_at=format_time(now),station_changes=items,pending_checks=pending,
                    feasibility="conflict" if any(row["status"]=="conflict" for row in items)
                    else "needs_verification" if pending or changed else version.timeline["result"]["feasibility"]
                    if version.timeline else "needs_verification",
                    requires_user_choice=bool(candidate or any(row["status"]=="conflict" for row in items)),
                    suggestion_date=candidate["target_date"] if candidate else None,
                    declined=False,provider_calls=len(budget.calls))
        repo = self.app.repository
        expiry = now + timedelta(minutes=REUSE_MINUTES)
        # A fresh observation can make the open dedup window shorter, never longer.
        for item in items:
            if item["status"]=="pending":
                continue
            until,_ = instant((item["latest"] or item["previous"])["valid_until"])
            if until:
                expiry = min(expiry,until)
        with repo.transaction() as db:
            if decision_key:
                prior = db.execute("""SELECT 1 FROM recheck_runs WHERE owner_id=? AND trip_id=? AND base_revision=?
                                      AND decision_key=? AND declined=1 LIMIT 1""",
                                   (version.owner_id,version.trip_id,version.revision,decision_key)).fetchone()
                if prior:
                    data.update(requires_user_choice=False,declined=True)
            validate("report",data)
            db.execute("""UPDATE recheck_runs SET status='done', reusable_until=?, decision_key=?, declined=?, result_json=?
                          WHERE run_id=? AND status='running'""",
                       (format_time(expiry),decision_key,int(data["declined"]),json.dumps(data,ensure_ascii=False),run_id))
        event(trace,"m10_finished",run_id=run_id,base_revision=version.revision,check_state=state,
              provider_calls=len(budget.calls),providers=budget.calls,station_changes=items,trip_writes=0)

    def scoped_run(self, db, token, trip_id, run_id):
        owner = self.app._owner(db,token)
        version = self.app.repository.version(db,owner,trip_id)
        row = db.execute("SELECT * FROM recheck_runs WHERE run_id=? AND trip_id=? AND owner_id=?",
                         (run_id,trip_id,owner)).fetchone()
        if row is None:
            raise NotFound()
        return version,row

    def summary(self, version, row):
        if row["result_json"]:
            result = json.loads(row["result_json"])
            result.update(current_revision=version.revision,declined=bool(row["declined"]))
            if result["declined"] or version.revision!=result["base_revision"]:
                result["requires_user_choice"] = False
            return result
        expired = format_time(self.clock())>=row["reusable_until"]
        return dict(run_id=row["run_id"],trip_id=row["trip_id"],base_revision=row["base_revision"],
                    current_revision=version.revision,check_state="unavailable" if expired else "running",
                    run_status="partial" if expired else "running",station_changes=[],pending_checks=[],
                    requires_user_choice=False,checked_at=row["created_at"],declined=False,provider_calls=0)

    def get(self, token, trip_id, run_id):
        with self.app.repository.connection() as db:
            version,row = self.scoped_run(db,token,trip_id,run_id)
        return self.summary(version,row)

    def decline(self, token, trip_id, run_id, *, trace=None):
        with self.app.repository.transaction() as db:
            version,row = self.scoped_run(db,token,trip_id,run_id)
            if row["status"]!="done" or not row["decision_key"]:
                raise RecheckError("no_suggestion",409)
            db.execute("""UPDATE recheck_runs SET declined=1 WHERE owner_id=? AND trip_id=?
                          AND base_revision=? AND decision_key=?""",
                       (version.owner_id,trip_id,row["base_revision"],row["decision_key"]))
        event(trace,"m10_suggestion_declined",run_id=run_id,base_revision=row["base_revision"],trip_writes=0)
        return self.get(token,trip_id,run_id)

    async def accept(self, token, trip_id, run_id, *, trace=None):
        report = self.get(token,trip_id,run_id)
        if report["current_revision"]!=report["base_revision"]:
            raise RecheckError("version_conflict",409)
        day = report.get("suggestion_date")
        if not day or not report["requires_user_choice"]:
            raise RecheckError("no_suggestion",409)
        with self.app.repository.connection() as db:
            _,row = self.scoped_run(db,token,trip_id,run_id)
            if format_time(self.clock())>=row["reusable_until"]:
                raise RecheckError("version_conflict",409)
        event(trace,"m10_suggestion_preview",run_id=run_id,base_revision=report["base_revision"],trip_writes=0)
        return await self.app.get_patch_service().preview(token,trip_id,scope_dates=[day],
                      instruction="改成室内，返程票不变",base_revision=report["base_revision"],trace=trace)

    async def agent_check(self, token, trip_id, revision, *, trace=None):
        run = self.start(token,trip_id,base_revision=revision,trigger="chat",trace=trace)
        report = run
        mode = report.get("data_mode","snapshot")
        findings = [dict(fact_id=row["fact_id"],before=observation_text(row["previous"]),
                         after=observation_text(row["latest"]) if row["latest"] else None,
                         checked_at=row["checked_at"],status="unconfirmed" if row["status"]=="pending" else "known",
                         evidence_ids=[]) for row in report["station_changes"]]
        result = dict(kind="recheck_report",trip_id=trip_id,base_revision=revision,data_mode=mode,
                      run_id=report["run_id"],check_state=report["check_state"],findings=findings,evidence_ids=[])
        return reply(result,self.clock(),"partial" if report["run_status"]!="completed" else "completed",
                     fixture=mode=="fixture")


def observation_text(observation):
    value = observation["value"]
    label = {"open":"开放","closed":"停运或关闭"}.get(value,value) if isinstance(value,str) else value
    return ("待查" if label is None else str(label)) + (" · " + observation["collected_at"] if observation["collected_at"] else "")
