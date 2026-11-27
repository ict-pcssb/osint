"""Isolation and clock controls for the reviewed Python test process."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import socket
import sys
from threading import local
from time import monotonic
from zoneinfo import ZoneInfo

from .reporting import redact

ALLOW_NETWORK = False
ALLOW_PAID_CALLS = False
CHARGE_CATEGORIES = {"free", "existing_quota", "paid", "unknown"}


class NetworkBlocked(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Trace:
    raw_fields = {"message", "messages", "prompt", "content", "body", "request_body", "response_body"}

    def __init__(self, trace_id):
        self.trace_id = trace_id
        self.events = []

    def record(self, event, **fields):
        sanitized = {key: value for key, value in fields.items() if key not in self.raw_fields}
        record = redact({**sanitized, "event": event, "trace_id": self.trace_id})
        self.events.append(record)
        if _active and _active[0] is not self:
            _active[0].events.append(record)


class FixedClock:
    timezone = "Asia/Shanghai"

    def __init__(self, instant):
        if instant.utcoffset() is None:
            raise ValueError("Fixed clock requires a timezone-aware instant")
        self.instant = instant.astimezone(ZoneInfo(self.timezone))

    def now(self):
        return self.instant

    def resolve(self, relative):
        today = self.instant.date()
        if relative == "明天":
            return today + timedelta(days=1)
        if relative == "周六":
            return today + timedelta(days=(5 - today.weekday()) % 7)
        return datetime.strptime(relative, "%Y-%m-%d").date()


class CallBudget:
    """A fixture budget. Provider retries and fallback share the attempt counter."""
    def __init__(self, tool_limit, provider_limit, seconds, *, now=monotonic):
        self.tool_limit = tool_limit
        self.provider_limit = provider_limit
        self.now = now
        self.deadline = now() + seconds
        self.tool_calls = 0
        self.provider_attempts = 0

    def _check_time(self):
        if self.now() >= self.deadline:
            raise BudgetExceeded("deadline")

    def tool(self):
        self._check_time()
        if self.tool_calls >= self.tool_limit:
            raise BudgetExceeded("tool_limit")
        self.tool_calls += 1

    def attempt(self, provider, *, category, data_mode):
        if category not in CHARGE_CATEGORIES:
            raise BudgetExceeded("unclassified_provider")
        if data_mode != "fixture":
            if category in {"paid", "unknown"}:
                raise BudgetExceeded("paid_or_unknown")
            raise BudgetExceeded("network_disabled")
        self._check_time()
        if self.provider_attempts >= self.provider_limit:
            raise BudgetExceeded("provider_limit")
        self.provider_attempts += 1


class ToolSpy:
    def __init__(self, fixtures, budget, trace):
        self.fixtures = fixtures
        self.budget = budget
        self.trace = trace
        self.travel_calls = 0

    def call(self, name):
        if name not in self.fixtures:
            raise PermissionError("No declared fixture for tool")
        self.budget.tool()
        self.travel_calls += 1
        self.trace.record("tool_call", tool=name, data_mode="fixture")
        return json.loads(json.dumps(self.fixtures[name]))


_NETWORK_EVENTS = {
    "socket.bind", "socket.connect", "socket.getaddrinfo", "socket.gethostbyaddr",
    "socket.gethostbyname", "socket.getnameinfo", "socket.sendto",
    "socket.sendmsg", "subprocess.Popen", "os.system", "os.startfile", "os.exec",
    "os.posix_spawn", "os.spawn", "os.fork",
}
_active = []
_isolated_paths = set()
_thread = local()
_hook_installed = False


def _audit(event, args):
    if not _active:
        return
    trace = _active[-1]
    if (event == "sqlite3.connect" and args[0] != getattr(_thread, "fixture_path", None)
            and args[0] not in _isolated_paths):
        trace.record("storage_connection_blocked", error="undeclared_sqlite_database")
        raise PermissionError("Offline tests require an explicitly scoped fixture database")
    if event not in _NETWORK_EVENTS:
        return
    if (getattr(_thread, "socketpair", False) and event in {"socket.bind", "socket.connect"}
            and isinstance(args[1], tuple) and args[1][0] in {"127.0.0.1", "::1"}):
        return
    trace.record("network_blocked", error=event)
    raise NetworkBlocked(event)


@contextmanager
def offline_guard(trace):
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    original_pair = socket.socketpair

    def socketpair(*args, **kwargs):
        # Windows asyncio needs a self-pipe. Only the stdlib socketpair's own
        # loopback bind/connect are permitted during this short operation.
        previous = getattr(_thread, "socketpair", False)
        _thread.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            _thread.socketpair = previous

    proactor = None
    if sys.platform == "win32":
        from asyncio.windows_events import IocpProactor
        proactor = IocpProactor
        original_connect = proactor.connect

        def connect(self, conn, address):
            # ConnectEx can bypass socket.socket.connect on Windows.
            _audit("socket.connect", (conn, address))
            return original_connect(self, conn, address)

        proactor.connect = connect
    socket.socketpair = socketpair
    _active.append(trace)
    try:
        yield
    finally:
        _active.pop()
        socket.socketpair = original_pair
        if proactor is not None:
            proactor.connect = original_connect


def allow_fixture_database(path, root):
    if not isinstance(path, Path):
        raise PermissionError("The fixture store accepts only a local test path")
    if path.name != "travel_m01.sqlite3" or path.resolve().parent != root.resolve():
        raise PermissionError("The database must be in the isolated fixture directory")
    if not root.is_dir() or not root.name.startswith("travel-m01-") or path.is_symlink():
        raise PermissionError("Invalid fixture location")
    return path.resolve()


@contextmanager
def fixture_database_connection(path, root):
    import sqlite3

    target = str(allow_fixture_database(path, root))
    previous = getattr(_thread, "fixture_path", None)
    _thread.fixture_path = target
    try:
        connection = sqlite3.connect(target, isolation_level=None)
    finally:
        _thread.fixture_path = previous
    try:
        # Disallow attaching another database or loading SQLite extensions.
        denied = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
        connection.set_authorizer(
            lambda action, *args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        yield connection
    finally:
        connection.close()



@contextmanager
def isolated_database(path, root):
    from tempfile import gettempdir

    if (not any(path.name == f"travel_{module}.sqlite3" and root.name.startswith(f"travel-{module}-")
                for module in ("m03", "m04", "m06", "m07", "m08", "m09", "m10", "m11", "m12")) or path.is_symlink() or path.exists()
            or not root.is_dir()
            or root.resolve().parent != Path(gettempdir()).resolve()
            or path.resolve().parent != root.resolve()):
        raise PermissionError("The application suite requires a new local SQLite file in its dedicated temporary directory")
    target = str(path.resolve())
    _isolated_paths.add(target)
    try:
        yield path
    finally:
        _isolated_paths.remove(target)
