import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from support.guards import isolated_database
from travel_release import RunTrace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "travel_agent"))
from demo_m11 import Workbench, make_case, NOW


class RadeonWorkbench(TestCase):
    def setUp(self):
        work = Path(self.enterContext(TemporaryDirectory(prefix="travel-m12-")))
        db = self.enterContext(isolated_database(work / "travel_m12.sqlite3", work))
        self.app, self.token, _, self.trip = make_case(db, clock=lambda: NOW)

    def ui(self, **options):
        ui = Workbench(self.app, self.token, self.trip, live=True, testing=False,
                       pdf_browser=None, model_provider="radeon", **options)
        self.addCleanup(ui.recheck.close)
        return ui

    def test_selects_radeon_and_only_shows_provider_name(self):
        with patch.dict(os.environ, {"RADEON_API_KEY":"fake-radeon", "DASHSCOPE_API_KEY":""}):
            ui = self.ui()
        self.assertTrue(ui.setup()["model"])
        self.assertEqual(ui.setup()["model_provider"], "radeon")
        self.assertNotIn("fake-radeon", json.dumps(ui.setup()))

    def test_radeon_does_not_fall_back_to_dashscope_key(self):
        with patch.dict(os.environ, {"RADEON_API_KEY":"", "DASHSCOPE_API_KEY":"fake-dashscope"}):
            ui = self.ui()
        self.assertFalse(ui.setup()["model"])

    def test_model_uses_workbench_context_and_radeon_adapter(self):
        with patch.dict(os.environ, {"RADEON_API_KEY":"fake-radeon", "DASHSCOPE_API_KEY":""}):
            ui = self.ui()
        trace = RunTrace()
        ui.current_mode, ui.current_trace = "model", trace
        raw = '{"intent":"weather","confidence_band":"high","requested_outputs":["weather"],"parameters":{}}'
        with patch.object(ui.radeon, "intent", new=AsyncMock(return_value=raw)) as mock:
            self.assertEqual(asyncio.run(ui.intent("那天呢？", NOW, {"place_id":"qingyan"}, False)), raw)
        mock.assert_awaited_once_with("那天呢？", NOW, {"place_id":"qingyan"}, False, trace=trace)


    def test_explicit_transfer_names_recover_unregistered_model_ids(self):
        from travel_routing import resolve_parameters

        supplied = {"from_id":"tongren-station", "to_id":"fanjingshan-east-gate"}
        resolve_parameters(supplied, ["transfer"], "请查明天从铜仁站到梵净山东门的接驳路线。", NOW)
        self.assertEqual(supplied, {"from_id":"tongren", "to_id":"fanjing-east"})
        ambiguous = {"from_id":"guess", "to_id":"guess"}
        resolve_parameters(ambiguous, ["transfer"], "铜仁站到梵净山东门，或铜仁站到梵净山西门。", NOW)
        self.assertEqual(ambiguous, {"from_id":"guess", "to_id":"guess"})

