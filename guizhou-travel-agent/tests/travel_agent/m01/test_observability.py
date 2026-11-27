import unittest

from support.guards import Trace


class ObservabilityTests(unittest.TestCase):
    def test_trace_relates_steps_without_preserving_prompt_or_credentials(self):
        for outcome, last_step in (("completed", "reply"), ("partial", "supplier"),
                                   ("failed", "rule"), ("unknown_commit", "response")):
            with self.subTest(outcome=outcome):
                trace = Trace("fixture-trace")
                trace.record("message_received", intent="fixture", message="sensitive mock prompt")
                trace.record("tool_attempt", tool="fixture-tool", provider="fixture",
                             attempt_id="att-1", duration_ms=4, retry=0,
                             parameters={"place_id": "fixture", "authorization": "Bearer fake-token"})
                trace.record("pipeline_result", result=outcome, step=last_step,
                             skill="fixture-skill", skill_version="1", evidence_ids=["fixture-1"],
                             writes=0, error="fixture-cause" if last_step != "reply" else None)
                self.assertTrue(all(row["trace_id"] == "fixture-trace" for row in trace.events))
                self.assertEqual(trace.events[-1]["step"], last_step)
                self.assertNotIn("sensitive mock prompt", str(trace.events))
                self.assertNotIn("fake-token", str(trace.events))

