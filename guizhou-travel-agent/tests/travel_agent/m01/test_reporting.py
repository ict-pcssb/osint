import unittest

from support.reporting import (
    Blocked, CaseResult, write_report, summarize, GuardedResult, OptionalDisabled
)
from tempfile import TemporaryDirectory
from pathlib import Path
import io
import json


class ReportTests(unittest.TestCase):
    def test_result_statuses_are_honest(self):
        class Sample(unittest.TestCase):
            def test_pass(self):
                self.assertTrue(True)

            def test_fail(self):
                self.fail("failure")

            def test_missing(self):
                raise Blocked("缺 Key 或测试库")

            def test_opt_in(self):
                raise OptionalDisabled("所有者明确关闭的选配夹具")

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=GuardedResult).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Sample))
        self.assertEqual(dict(result.statuses),
                         {"test_pass": "PASS", "test_fail": "FAIL",
                          "test_missing": "BLOCKED", "test_opt_in": "SKIPPED"})
        cases = [CaseResult(case_id=case_id, layer="unit", command="python -B", commit=None,
                            environment="fixture-runtime", data_mode="fixture", expected="guarded",
                            actual="Bearer fake-result", status=status, reason="fixture",
                            trace_id=f"trace-{case_id}", artifacts=[], critical=case_id != "test_opt_in")
                 for case_id, status in result.statuses]
        totals = summarize(cases)
        self.assertEqual((totals["planned"], totals["executed"], totals["pass_denominator"],
                          totals["not_executed"]), (4, 2, 4, 2))
        self.assertEqual(totals["counts"], {"PASS": 1, "FAIL": 1, "BLOCKED": 1, "SKIPPED": 1})
        self.assertFalse(totals["gate_passed"])
        with TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            write_report(path, cases, scope="m01-test")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("fake-result", raw)
            self.assertEqual(json.loads(raw)["cases"][0]["trace_id"], cases[0].trace_id)

