"""Evidence and result contracts shared by local travel tests."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
import unittest

STATUSES = ("PASS", "FAIL", "BLOCKED", "SKIPPED")
SECRET = re.compile(
    r"authorization|api.?key|(?:access|refresh|session|auth|provider)?.?token|secret|password|passwd|"
    r"cookie|credential|(?:database|mysql).?(?:url|dsn)|ticket.?(?:no|number)|phone|mobile|"
    r"(?:real|full|user|passenger).?name|^name$|^key$|^sig(?:nature)?$",
    re.I,
)
URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.I)
URL_USERINFO = re.compile(r"(?<=://)[^/@\s]+@", re.I)
HEADER = re.compile(r"\b(?:Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*[:=]\s*[^\r\n]+", re.I)
BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[^\s,;\"'\]}]+", re.I)
LABELED = re.compile(
    r"""(?ix)
    \b[A-Z0-9_.-]*(?:key|token|secret|password|passwd|credential|phone|mobile|ticket_no|ticket_number|
    real_name|full_name|username|signature|dsn)['"]?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]]+)
    |(?:手机号|手机号码|姓名|票号)\s*[：:=]\s*[^，,。\r\n;]+
    """
)
CHINA_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


def redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if SECRET.search(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    value = URL.sub(lambda match: URL_USERINFO.sub("", re.split(r"[?#]", match[0], maxsplit=1)[0]), value)
    value = HEADER.sub("[redacted header]", value)
    value = BEARER.sub("[redacted auth]", value)
    value = LABELED.sub("[redacted value]", value)
    return CHINA_PHONE.sub("[redacted phone]", value)


def classify_evidence(records):
    historical = [row for row in records if row["scope"] == "historical"]
    confirmed = [
        row for row in records
        if row["scope"] == "current" and row["status"] == "PASS"
        and row.get("trace_id") and row.get("assertions") and row.get("environment_id")
    ]
    new_agent = sum(row["evidence_level"] == "new_agent" for row in confirmed)
    return {
        "historical_record_count": len(historical),
        "historical_business_data_recorded": any(
            row["evidence_level"] == "business_data" and row["status"] == "PASS" for row in historical
        ),
        "current_new_agent_passes": new_agent,
        "new_agent_verified": bool(new_agent),
        "target_environment": "PASS" if any(
            row["evidence_level"] == "target_environment" for row in confirmed
        ) else "BLOCKED",
    }


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    layer: str
    command: str
    commit: str | None
    environment: str
    data_mode: str
    expected: str
    actual: str
    status: str
    reason: str
    trace_id: str
    artifacts: list[str]
    critical: bool

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError("Unknown test result status")
        if not self.case_id or not self.trace_id:
            raise ValueError("A result requires both case and trace identifiers")
        if self.critical and self.status == "SKIPPED":
            raise ValueError("A critical test cannot be skipped")


def summarize(cases):
    counts = {status: sum(case.status == status for case in cases) for status in STATUSES}
    return {
        "planned": len(cases),
        "executed": counts["PASS"] + counts["FAIL"],
        "pass_denominator": len(cases),
        "executed_pass_denominator": counts["PASS"] + counts["FAIL"],
        "not_executed": counts["BLOCKED"] + counts["SKIPPED"],
        "counts": counts,
        "gate_passed": bool(cases) and all(not case.critical or case.status == "PASS" for case in cases),
    }


def write_report(path, cases, *, scope, **metadata):
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        **metadata, "schema_version": "1.0", "scope": scope,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(cases), "cases": [asdict(case) for case in cases],
    }
    path.write_text(json.dumps(redact(document), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Blocked(Exception):
    """Required test conditions are unavailable."""


class OptionalDisabled(Exception):
    """The owner expressly disabled a noncritical test."""


class GuardedResult(unittest.TestResult):
    # Only diagnostic categories and source locations are kept. unittest's default
    # traceback formatting can include secrets from assertion values.
    def __init__(self, stream=None, descriptions=None, verbosity=None):
        super().__init__()
        self.outcomes = {}

    @property
    def statuses(self):
        return [(case_id.rsplit(".", 1)[-1], outcome[0]) for case_id, outcome in self.outcomes.items()]

    def _record(self, test, status, reason=""):
        test_id = test.id()
        if self.outcomes.get(test_id, ("", ""))[0] != "FAIL":
            self.outcomes[test_id] = (status, redact(reason))

    def addSuccess(self, test):
        self._record(test, "PASS")

    def addFailure(self, test, err):
        self.failures.append((test, self._diagnostic(err)))
        self._record(test, "FAIL", self._diagnostic(err))

    def addError(self, test, err):
        if issubclass(err[0], (Blocked, OptionalDisabled)):
            status = "BLOCKED" if issubclass(err[0], Blocked) else "SKIPPED"
            self._record(test, status, str(err[1]))
        elif issubclass(err[0], ImportError):
            self._record(test, "BLOCKED", self._diagnostic(err))
        else:
            self.errors.append((test, self._diagnostic(err)))
            self._record(test, "FAIL", self._diagnostic(err))

    def addSkip(self, test, reason):
        # A bare unittest skip, including a missing MySQL environment, lacks approval.
        self._record(getattr(test, "test_case", test), "BLOCKED", reason)

    def addSubTest(self, test, subtest, err):
        if err:
            self.addFailure(test, err)

    def addExpectedFailure(self, test, err):
        self.addFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.failures.append((test, "Unexpected success"))
        self._record(test, "FAIL", "Unexpected success")

    @staticmethod
    def _diagnostic(err):
        cursor = err[2]
        while cursor and cursor.tb_next:
            cursor = cursor.tb_next
        where = f" at {cursor.tb_frame.f_code.co_name}:{cursor.tb_lineno}" if cursor else ""
        message = ""
        if issubclass(err[0], ModuleNotFoundError) and err[1].name:
            message = f" ({err[1].name})"
        elif issubclass(err[0], ImportError):
            missing = re.search(r"No module named ['\"]([\w.]+)['\"]", str(err[1]))
            if missing:
                message = f" ({missing[1]})"
        if issubclass(err[0], AssertionError):
            message = f" {err[1]}"
        return redact(err[0].__name__ + message + where)

