import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import closing
import http.client
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from support.guards import (
    ALLOW_NETWORK, ALLOW_PAID_CALLS, BudgetExceeded, CallBudget, FixedClock,
    NetworkBlocked, ToolSpy, Trace, allow_fixture_database, offline_guard,
)
from support.reporting import redact
from support.store import WriteSpy


class GuardTests(unittest.TestCase):
    def test_offline_guard_blocks_external_calls(self):
        import travel_agent

        self.assertFalse(ALLOW_NETWORK)
        self.assertFalse(ALLOW_PAID_CALLS)
        trace = Trace("m01-02")
        now = [0.0]
        budget = CallBudget(3, 1, 2, now=lambda: now[0])
        tools = ToolSpy({"weather": {"data_mode": "fixture", "rain": False}}, budget, trace)
        with offline_guard(trace):
            self.assertFalse(tools.call("weather")["rain"])
            with self.assertRaises(NetworkBlocked):
                with closing(http.client.HTTPConnection("example.invalid", timeout=0.1)) as client:
                    client.request("GET", "/")

            async def confirm_block(operation):
                blocked = False
                try:
                    await operation
                except* NetworkBlocked:
                    blocked = True
                self.assertTrue(blocked, "the real transport must be blocked")

            async def try_model():
                await confirm_block(travel_agent.complete([{"role": "user", "content": "fixture"}]))

            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "FAKE_M1_KEY"}), \
                 patch.object(travel_agent, "endpoint", return_value="https://example.invalid/"):
                asyncio.run(try_model())
            # Exercise the real HTTP transport separately without sending any credentials.
            async def real_http():
                async with travel_agent.httpx2.AsyncClient(trust_env=False) as client:
                    await confirm_block(client.get("https://example.invalid"))
            asyncio.run(real_http())
            with self.assertRaises(NetworkBlocked):
                socket.getaddrinfo("example.invalid", 443)
            with self.assertRaises(NetworkBlocked), socket.socket(type=socket.SOCK_DGRAM) as udp:
                udp.sendto(b"fixture", ("127.0.0.1", 9876))
            with self.assertRaises(NetworkBlocked), socket.socket() as tcp:
                tcp.connect(("127.0.0.1", 9876))
            with self.assertRaises(NetworkBlocked):
                subprocess.run([sys.executable, "-V"], check=True)
            with self.assertRaises(PermissionError):
                sqlite3.connect(":memory:")
        budget.attempt("fixture_retry", category="existing_quota", data_mode="fixture")
        self.assertEqual(budget.provider_attempts, 1)
        with self.assertRaisesRegex(BudgetExceeded, "provider_limit"):
            budget.attempt("fixture_fallback", category="free", data_mode="fixture")
        with self.assertRaisesRegex(BudgetExceeded, "paid_or_unknown"):
            budget.attempt("unknown", category="unknown", data_mode="live")
        now[0] = 3.1
        with self.assertRaisesRegex(BudgetExceeded, "deadline"):
            tools.call("weather")
        self.assertEqual(tools.travel_calls, 1)
        self.assertEqual(budget.tool_calls, 1)
        self.assertTrue(any(event["event"] == "network_blocked" for event in trace.events))

    def test_beijing_clock_is_host_independent(self):
        frozen = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
        for host_zone in (frozen.astimezone().tzinfo, timezone.utc, timezone(timedelta(hours=8))):
            with self.subTest(host_zone=host_zone):
                clock = FixedClock(frozen.astimezone(host_zone))
                self.assertEqual(clock.resolve("明天").isoformat(), "2026-11-27")
                self.assertEqual(clock.resolve("周六").isoformat(), "2026-11-28")
                self.assertEqual(clock.now().isoformat(), "2026-11-26T10:00:00+08:00")
                self.assertEqual(clock.timezone, "Asia/Shanghai")

    def test_test_database_and_logs_are_safe(self):
        with TemporaryDirectory(prefix="travel-m01-") as folder:
            root = Path(folder)
            with self.assertRaises(PermissionError):
                allow_fixture_database("mysql://fixture@localhost/production", root)
            with self.assertRaises(PermissionError):
                allow_fixture_database(root / "../travel_m01.sqlite3", root)
            with self.assertRaises(PermissionError):
                allow_fixture_database(root / "production.sqlite3", root)
        trace = Trace("m01-04")
        spy = WriteSpy(trace)
        with self.assertRaises(PermissionError):
            spy.write("trip.insert", lambda: self.fail("write must not be invoked"))
        allowed = WriteSpy(trace, allowed=True)
        self.assertEqual(allowed.write("fixture.insert", lambda: "ok"), "ok")
        self.assertEqual((spy.count, allowed.count), (0, 1))
        payload = redact({
            "headers": {"Authorization": "Bearer fake-BEARER", "X-Api-Key": "fake-HEADER"},
            "phone": "13812345678", "name": "Test Traveler", "DASHSCOPE_API_KEY": "fake-VAR",
            "exception": "timeout at https://example.invalid/mcp?key=fake-QUERY&sig=fake-SIG "
                         "Bearer fake-TEXT phone=13812345678",
            "trace_id": "trace-m1",
        })
        for secret in ("fake-", "13812345678", "Test Traveler"):
            self.assertNotIn(secret, str(payload))
        self.assertIn("timeout", str(payload))
        self.assertEqual(payload["trace_id"], "trace-m1")

