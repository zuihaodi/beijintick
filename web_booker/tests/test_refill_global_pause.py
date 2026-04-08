# -*- coding: utf-8 -*-
"""全局独立 Refill 暂停 API 与状态。"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, task_manager, CONFIG  # noqa: E402


class TestRefillGlobalPauseAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_web_ui = copy.deepcopy(CONFIG.get("web_ui_auth") or {})
        CONFIG["web_ui_auth"] = {"enabled": False}

    @classmethod
    def tearDownClass(cls):
        CONFIG["web_ui_auth"] = cls._orig_web_ui

    def setUp(self):
        self._was_ms = int(getattr(task_manager, "_refill_global_pause_until_ms", 0) or 0)
        task_manager.clear_refill_global_pause()

    def tearDown(self):
        task_manager._refill_global_pause_until_ms = self._was_ms
        task_manager.save_refill_scheduler_state()

    def test_get_pause_status(self):
        c = app.test_client()
        r = c.get("/api/refill-scheduler/pause")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("paused", j)
        self.assertIn("remaining_seconds", j)
        self.assertFalse(j.get("paused"))

    def test_post_pause_then_resume(self):
        c = app.test_client()
        r = c.post("/api/refill-scheduler/pause", json={"action": "pause"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual(j.get("status"), "success")
        self.assertTrue(j.get("paused"))
        r2 = c.post("/api/refill-scheduler/pause", json={"action": "resume"})
        self.assertEqual(r2.status_code, 200)
        j2 = r2.get_json()
        self.assertEqual(j2.get("status"), "success")
        self.assertFalse(j2.get("paused"))

    def test_post_invalid_action(self):
        c = app.test_client()
        r = c.post("/api/refill-scheduler/pause", json={"action": "nope"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
