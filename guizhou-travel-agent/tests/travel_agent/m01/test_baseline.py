from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from support.audit import audit_dependencies, audit_mcp, required_argument_gaps
from support.reporting import classify_evidence


class BaselineTests(unittest.TestCase):
    def test_evidence_level_is_not_promoted(self):
        records = [
            {"evidence_level": "handshake", "scope": "historical", "status": "PASS"},
            {"evidence_level": "business_data", "scope": "historical", "status": "PASS"},
            {"evidence_level": "legacy_project", "scope": "historical", "status": "PASS",
             "passed": 48, "total": 48},
            {"evidence_level": "registration", "scope": "current", "status": "PASS",
             "actual_tool_name": "example"},
            {"evidence_level": "legacy_project", "scope": "historical", "status": "PASS",
             "passed": 32, "total": 35},
            {"evidence_level": "new_agent", "scope": "current", "status": "PASS"},
        ]
        summary = classify_evidence(records)
        self.assertEqual(summary["historical_record_count"], 4)
        self.assertEqual(summary["current_new_agent_passes"], 0)
        self.assertFalse(summary["new_agent_verified"])
        self.assertEqual(summary["target_environment"], "BLOCKED")
        self.assertFalse(classify_evidence(records[:1])["historical_business_data_recorded"])

    def test_lock_and_mcp_config_audit(self):
        deps = audit_dependencies("mcp==2.2.0\nhttpx2==2.16.2", {"mcp": "1.30.0"},
                                  {"mcp": "2.0.0"}, "wrong-runtime")
        self.assertEqual([row["status"] for row in deps], ["BLOCKED", "BLOCKED"])
        self.assertEqual(deps[0]["runtime"], "1.30.0")
        self.assertEqual(deps[0]["lock"], "2.0.0")
        self.assertEqual(deps[0]["environment"], "wrong-runtime")
        self.assertEqual(deps[1]["reason"], "dependency_missing")
        with TemporaryDirectory() as folder:
            result = audit_mcp({
                "mcpServers": {
                    "stdio": {"command": r"Z:\missing\python.exe", "args": ["--token=FAKE"],
                              "env": {"SERVICE_TOKEN": "FAKE"}},
                    "remote": {"type": "http", "url": "https://example.invalid/mcp?token=FAKE"},
                },
            }, Path(folder))
        self.assertEqual(result["stdio"]["status"], "BLOCKED")
        self.assertEqual(result["stdio"]["reason"], "command_missing")
        self.assertEqual(result["stdio"]["env_names"], ["SERVICE_TOKEN"])
        self.assertEqual(result["remote"]["status"], "BLOCKED")
        self.assertNotIn("FAKE", repr(result))
        self.assertEqual(required_argument_gaps(
            {"required": ["query", "subject"]}, {"query": "query"}), ["subject"])
        self.assertEqual(required_argument_gaps({"required": ["query"]}, {"query": ""}), ["query"])

