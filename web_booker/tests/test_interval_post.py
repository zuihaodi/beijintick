# -*- coding: utf-8 -*-
"""全量间隔 POST：delivery_mode、候选枚举、分类映射（零 pytest 依赖）。"""
import os
import sys
import unittest

_WEB_BOOKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _WEB_BOOKER_DIR not in sys.path:
    sys.path.insert(0, _WEB_BOOKER_DIR)

import app as booker  # noqa: E402


class TestGetTaskDeliveryMode(unittest.TestCase):
    def test_default_matrix(self):
        self.assertEqual(booker.get_task_delivery_mode(None), "matrix")
        self.assertEqual(booker.get_task_delivery_mode({}), "matrix")
        self.assertEqual(booker.get_task_delivery_mode({"delivery_mode": "matrix"}), "matrix")

    def test_interval(self):
        self.assertEqual(booker.get_task_delivery_mode({"delivery_mode": "interval_post"}), "interval_post")


class TestBuildIntervalPostCandidateBlocks(unittest.TestCase):
    def test_two_places_two_hour_start_20(self):
        tc = {
            "delivery_matrix_place_min": 3,
            "delivery_matrix_place_max": 4,
            "interval_post_consecutive_hours": 2,
            "interval_post_candidate_order": "ordered",
            "delivery_target_times": ["20:00", "21:00"],
            "delivery_time_preference_order": ["20:00", "21:00"],
        }
        primary = [{"place": "3", "time": "20:00"}, {"place": "3", "time": "21:00"}]
        vm = booker._merge_task_venue_strategy(tc, primary)
        blocks, err = booker.build_interval_post_candidate_blocks(tc, primary, vm)
        self.assertEqual(err, "")
        by_id = {b["id"]: b for b in blocks}
        self.assertIn("3:20:00x2", by_id)
        self.assertIn("4:20:00x2", by_id)
        self.assertEqual(by_id["3:20:00x2"]["times"], ["20:00", "21:00"])

    def test_matrix_range_when_no_candidate_places(self):
        tc = {
            "delivery_matrix_place_min": 5,
            "delivery_matrix_place_max": 6,
            "interval_post_consecutive_hours": 1,
            "delivery_target_times": ["19:00"],
            "delivery_time_preference_order": ["19:00"],
        }
        primary = [{"place": "5", "time": "19:00"}]
        vm = booker._merge_task_venue_strategy(tc, primary)
        blocks, err = booker.build_interval_post_candidate_blocks(tc, primary, vm)
        self.assertEqual(err, "")
        places = {b["place"] for b in blocks}
        self.assertEqual(places, {"5", "6"})

    def test_candidate_places_ignored_uses_matrix_span(self):
        """主组 candidate_places 不决定枚举号；以矩阵号段为准。"""
        tc = {
            "candidate_places": ["9"],
            "delivery_matrix_place_min": 1,
            "delivery_matrix_place_max": 2,
            "interval_post_consecutive_hours": 1,
            "delivery_target_times": ["10:00"],
            "delivery_time_preference_order": ["10:00"],
        }
        primary = [{"place": "9", "time": "10:00"}]
        vm = booker._merge_task_venue_strategy(tc, primary)
        blocks, err = booker.build_interval_post_candidate_blocks(tc, primary, vm)
        self.assertEqual(err, "")
        places = sorted({b["place"] for b in blocks})
        self.assertEqual(places, ["1", "2"])


class TestMapIntervalPostClassifiedAction(unittest.TestCase):
    def test_switch_backup_is_drop(self):
        self.assertEqual(
            booker.map_interval_post_classified_action({"action": "switch_backup"}),
            "drop_candidate",
        )

    def test_success(self):
        self.assertEqual(
            booker.map_interval_post_classified_action({"action": "stop_success"}),
            "success",
        )

    def test_continue_is_retry(self):
        self.assertEqual(
            booker.map_interval_post_classified_action({"action": "continue_delivery"}),
            "retry_tail",
        )


class TestValidateIntervalPostStrategy(unittest.TestCase):
    def test_ok_minimal(self):
        cfg = {
            "delivery_matrix_place_min": 1,
            "delivery_matrix_place_max": 1,
            "delivery_target_times": ["20:00"],
            "interval_post_consecutive_hours": 2,
        }
        self.assertEqual(booker.validate_interval_post_strategy(cfg), [])

    def test_bad_order(self):
        cfg = {
            "delivery_matrix_place_min": 1,
            "delivery_matrix_place_max": 2,
            "delivery_target_times": ["20:00"],
            "interval_post_candidate_order": "nope",
        }
        errs = booker.validate_interval_post_strategy(cfg)
        self.assertTrue(any("ordered" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
