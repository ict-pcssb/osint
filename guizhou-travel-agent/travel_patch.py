"""Preview and commit a scoped itinerary patch against an owned SQLite trip."""
import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import os
from threading import RLock
from uuid import uuid4

from travel_contract import ContractError
from travel_evidence import digest
from travel_rules import POLICY_VERSION, format_time, instant
from travel_state import BEIJING
from travel_state_app import NotFound
from travel_transport import event
from travel_transport_skills import reply
from travel_patch_plan import ApplyPlan, day_id, validate_patch

PROPOSAL_TTL=timedelta(minutes=10)
KEY_PREFIX="patch:"


class PatchError(ValueError):
    def __init__(self,code,status,write_status="conflict"):
        self.code,self.status,self.save_status=code,status,write_status
        messages={"confirmation_required":"请确认这一次修改预览。",
                  "version_conflict":"行程版本已变化，请打开最新版本后重新预览。",
                  "expired":"本次预览或报价已过期，请重新预览。",
                  "rule_conflict":"这次修改存在规则冲突，请调整后再预览。",
                  "proposal_unavailable":"本次提案不可用，请重新预览。",
                  "commit_disabled":"局部修改保存已暂停，当前行程可继续查看。",
                  "invalid_request":"请提交有效的局部修改请求。",
                  "idempotency_conflict":"本次操作键已绑定其他请求。"}
        super().__init__(messages.get(code,"本次修改无法保存，请重新预览。"))


@dataclass(frozen=True)
class Proposal:
    owner: str
    trip_id: str
    base_revision: int
    proposal_id: str
    dates: tuple
    expires_at: datetime
    checked_at: str
    base_fingerprint: str
    candidate_fingerprint: str
    plan: dict


def fingerprint(value):
    return digest(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False))


def revision_identity(version):
    return fingerprint({"timeline":version.timeline,"fields":version.fields,
                        "provenance":{key:asdict(value) for key,value in version.provenance.items()},
                        "locks":[asdict(item) for item in version.locked_items]})


def check_request(kind, obj):
    try:
        validate_patch(kind,obj)
    except ContractError as exc:
        raise PatchError("invalid_request",422,"draft") from exc


class PatchService:
    def __init__(self,app,planner):
        self.app,self.planner=app,planner
        self.lock=RLock()
        self.proposals={}

    def clock(self):
        return self.app.clock().astimezone(BEIJING)

    def recent_version(self,token,trip_id):
        with self.app.repository.connection() as db:
            owner=self.app._owner(db,token)
            return self.app.repository.version(db,owner,trip_id)

    async def preview(self,token,trip_id,*,scope_dates,instruction,base_revision,trace=None,plan_factory=None):
        check_request("preview",dict(scope_dates=scope_dates,instruction=instruction,base_revision=base_revision))
        current=self.recent_version(token,trip_id)
        if current.revision!=base_revision:
            raise PatchError("version_conflict",409)
        started=self.clock()
        dates=tuple(sorted(scope_dates))
        result=current.timeline["result"] if current.timeline else None
        supported=(result and result.get("return_leg") and len(current.locked_items)>=1
                   and set(dates)<={d["date"] for d in result["days"]})
        mode=result["data_mode"] if supported else "snapshot"
        if not supported:
            return self.preview_failure(started,mode,"unbound",trace,"请打开含时间轴和固定返程的行程，再选择修改日期。")
        try:
            planner = plan_factory or self.planner
            planned=await asyncio.wait_for(planner(deepcopy(current),dates,instruction),timeout=30)
            plan=deepcopy(validate_patch("plan",planned))
            if {op["date"] for op in plan["operations"]}!=set(dates):
                raise ContractError("scope_not_covered")
            now=self.clock()
            checked=format_time(now)
            expiry=now+PROPOSAL_TTL
            if quote:=plan.get("quote_expires_at"):
                expires,_=instant(quote)
                if expires is None or expires<=now:
                    return self.preview_failure(now,mode,"provider_error",trace,"报价已过期，请重新查询并查看差异。")
                expiry=min(expiry,expires)
            candidate,details,new_evidence=ApplyPlan(current,plan,dates,checked).build()
        except TimeoutError:
            return self.preview_failure(self.clock(),mode,"timeout",trace,"候选查询超时，原行程仍可查看。")
        except (ContractError,TypeError,KeyError,ValueError,IndexError):
            return self.preview_failure(self.clock(),mode,"invalid_response",trace,"候选资料无效或改变了指定范围之外的安排。")
        except (OSError,RuntimeError):
            return self.preview_failure(self.clock(),mode,"provider_error",trace,"暂时没有拿到可用的修改候选，原行程仍可查看。")

        identity=uuid4().hex
        proposal=Proposal(current.owner_id,trip_id,base_revision,identity,dates,expiry,checked,
                          revision_identity(current),fingerprint(candidate),plan)
        data=dict(kind="patch_preview",data_mode=mode,proposal_id=identity,trip_id=trip_id,base_revision=base_revision,
                  expires_at=format_time(expiry),scope={"dates":list(dates),"day_ids":[day_id(d) for d in result["days"] if d["date"] in dates]},
                  save_status="awaiting_confirmation",needs_user_choice=True,
                  evidence_ids=[row["evidence_id"] for row in new_evidence],**details)
        known=started if started>now else now
        packet=reply(data,known,candidate["run_status"],new_evidence,fixture=mode=="fixture")
        with self.lock:
            self.proposals={key:value for key,value in self.proposals.items() if value.expires_at>now-timedelta(minutes=10)}
            if len(self.proposals)>=256:
                self.proposals.pop(next(iter(self.proposals)))
            self.proposals[identity]=proposal
        event(trace,"m09_proposal_created",base_revision=base_revision,scope=list(dates),proposal_hash=digest(identity),
              rule_status=details["rule_status"],rule_codes=details["rule_codes"],changed_stop_ids=[
                  row["stop_id"] for row in details["changes"]],trip_writes=0)
        return packet

    async def preview_retime(self,token,trip_id,*,date,stop_id,arrival,departure,base_revision,trace=None):
        async def plan(*_):
            return dict(operations=[dict(op="retime",date=date,stop_id=stop_id,arrive=arrival,leave=departure)],
                        points=[],routes=[],unknown_cost_ids=[])
        return await self.preview(token,trip_id,scope_dates=[date],instruction="调整这一站的时间",
                                  base_revision=base_revision,trace=trace,plan_factory=plan)

    @staticmethod
    def preview_failure(now,mode,reason,trace,help_text):
        event(trace,"m09_preview_unavailable",reason=reason,trip_writes=0)
        return reply(dict(kind="unavailable",requested_kind="patch_preview",reason=reason,action=help_text),
                     now,"partial",fixture=mode=="fixture")

    def proposal_for(self,version,proposal_id,base_revision):
        proposal=self.proposals.get(proposal_id)
        if proposal is None:
            raise PatchError("proposal_unavailable",410,"draft")
        if proposal.owner!=version.owner_id or proposal.trip_id!=version.trip_id:
            raise NotFound()
        if proposal.base_revision!=base_revision or version.revision!=base_revision:
            raise PatchError("version_conflict",409)
        if self.clock()>=proposal.expires_at:
            raise PatchError("expired",410,"draft")
        return proposal

    @staticmethod
    def receipt(record,trip_id):
        return dict(save_status="saved",trip_id=trip_id,revision=record["revision"],
                    proposal_id=record["draft_id"][len(KEY_PREFIX):],base_revision=record["revision"]-1)

    def commit(self,token,trip_id,*,proposal_id,base_revision,idempotency_key,confirmed,trace=None):
        if confirmed is not True:
            raise PatchError("confirmation_required",422,"awaiting_confirmation")
        check_request("commit",dict(proposal_id=proposal_id,base_revision=base_revision,idempotency_key=idempotency_key,
                                    confirmed=confirmed))
        guard=self.app.repository
        with guard.transaction() as db:
            owner=self.app._owner(db,token)
            current=guard.version(db,owner,trip_id)
            record=db.execute("SELECT * FROM save_keys WHERE owner_id=? AND request_key=?",(owner,idempotency_key)).fetchone()
            if record:
                if (record["trip_id"]!=trip_id or record["draft_id"]!=KEY_PREFIX+proposal_id
                        or record["revision"]!=base_revision+1):
                    raise PatchError("idempotency_conflict",409)
                respond=self.receipt(record,trip_id)
                event(trace,"m09_commit_replay",revision=respond["revision"],operation_hash=digest(idempotency_key),trip_writes=0)
                return respond
            if current.revision!=base_revision:
                event(trace,"m09_commit_conflict",reason="version_conflict",base_revision=base_revision,
                      current_revision=current.revision,trip_writes=0)
                raise PatchError("version_conflict",409)
            if (os.getenv("TRAVEL_M09_COMMIT","enabled")!="enabled"
                    or os.getenv("TRAVEL_RULES_POLICY_VERSION",POLICY_VERSION)!=POLICY_VERSION):
                raise PatchError("commit_disabled",503,"awaiting_confirmation")
            with self.lock:
                proposal=self.proposal_for(current,proposal_id,base_revision)
                if revision_identity(current)!=proposal.base_fingerprint:
                    raise PatchError("version_conflict",409)
                try:
                    candidate,details,_=ApplyPlan(current,proposal.plan,proposal.dates,proposal.checked_at).build()
                except (ContractError,ValueError,KeyError,IndexError,TypeError) as exc:
                    raise PatchError("rule_conflict",409) from exc
                if (details["rule_status"]=="conflict" or fingerprint(candidate)!=proposal.candidate_fingerprint
                        or self.clock()>=proposal.expires_at):
                    raise PatchError("expired" if self.clock()>=proposal.expires_at else "rule_conflict",
                                     410 if self.clock()>=proposal.expires_at else 409)
                revision=base_revision+1
                guard.insert_version(db,current,revision,timeline=candidate)
                db.execute("INSERT INTO save_keys VALUES (?,?,?,?,?)",
                           (owner,idempotency_key,KEY_PREFIX+proposal_id,trip_id,revision))
            response=dict(save_status="saved",trip_id=trip_id,proposal_id=proposal_id,revision=revision,
                          base_revision=base_revision)
        event(trace,"m09_commit_complete",base_revision=base_revision,revision=response["revision"],
              proposal_hash=digest(proposal_id),operation_hash=digest(idempotency_key),trip_writes=1)
        return response

    def operation_status(self,token,trip_id,idempotency_key,*,trace=None):
        check_request("operation",dict(idempotency_key=idempotency_key))
        with self.app.repository.connection() as db:
            owner=self.app._owner(db,token)
            self.app.repository.version(db,owner,trip_id)
            saved=db.execute("SELECT * FROM save_keys WHERE owner_id=? AND request_key=? AND trip_id=?",
                             (owner,idempotency_key,trip_id)).fetchone()
        found=saved and saved["draft_id"].startswith(KEY_PREFIX)
        result=self.receipt(saved,trip_id) if found else dict(save_status="unknown_commit",trip_id=trip_id)
        event(trace,"m09_operation_status",status=result["save_status"],operation_hash=digest(idempotency_key),trip_writes=0)
        return result

    def discard(self,token,trip_id,*,proposal_id,base_revision,trace=None):
        check_request("discard",dict(proposal_id=proposal_id,base_revision=base_revision))
        with self.app.repository.transaction() as db:
            owner=self.app._owner(db,token)
            version=self.app.repository.version(db,owner,trip_id)
            with self.lock:
                self.proposal_for(version,proposal_id,base_revision)
                del self.proposals[proposal_id]
        event(trace,"m09_proposal_discarded",base_revision=base_revision,proposal_hash=digest(proposal_id),trip_writes=0)
        return dict(save_status="discarded",trip_id=trip_id,base_revision=base_revision)

