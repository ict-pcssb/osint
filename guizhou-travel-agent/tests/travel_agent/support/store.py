"""A transactional test fixture. The application has no storage adapter yet."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .guards import fixture_database_connection


class WriteSpy:
    def __init__(self, trace, *, allowed=False):
        self.trace = trace
        self.allowed = allowed
        self.count = 0

    def write(self, operation, action):
        if not self.allowed:
            self.trace.record("storage_write_rejected", operation=operation, error="write_not_declared")
            raise PermissionError("Storage writes are disabled by default")
        result = action()
        self.count += 1
        self.trace.record("storage_write", operation=operation, writes=self.count)
        return result


class FixtureStore:
    def __init__(self, trace):
        self.trace = trace
        self.writes = WriteSpy(trace, allowed=True)
        self._resources = ExitStack()

    def __enter__(self):
        with ExitStack() as setup:
            root = Path(setup.enter_context(TemporaryDirectory(prefix="travel-m01-")))
            self._db = setup.enter_context(fixture_database_connection(root / "travel_m01.sqlite3", root))
            self._db.execute(
                "CREATE TABLE trip (owner TEXT, id TEXT, payload TEXT, PRIMARY KEY (owner, id))")
            self._resources = setup.pop_all()
        return self

    def __exit__(self, *exc):
        return self._resources.__exit__(*exc)

    @contextmanager
    def transaction(self):
        self._db.execute("BEGIN")
        try:
            yield
        finally:
            self._db.rollback()
            self.trace.record("storage_rollback", scope="fixture")

    def _owner(self, account):
        if account not in {"fixture-A", "fixture-B"}:
            raise PermissionError("Use a declared fixture identity")
        return account

    def read(self, account, trip):
        row = self._db.execute(
            "SELECT payload FROM trip WHERE owner=? AND id=?", (self._owner(account), trip)).fetchone()
        return row[0] if row else None

    def create(self, account, trip, payload):
        owner = self._owner(account)
        self.writes.write(
            "fixture.insert", lambda: self._db.execute("INSERT INTO trip VALUES (?, ?, ?)", (owner, trip, payload)))

    def update(self, account, trip, payload):
        owner = self._owner(account)
        if self.read(owner, trip) is None:
            self.trace.record("storage_write_rejected", operation="fixture.update", error="not_found")
            return False
        self.writes.write("fixture.update", lambda: self._db.execute(
            "UPDATE trip SET payload=? WHERE owner=? AND id=?", (payload, owner, trip)))
        return True

    def row_count(self):
        return self._db.execute("SELECT count(*) FROM trip").fetchone()[0]

