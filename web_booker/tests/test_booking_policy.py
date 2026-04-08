# -*- coding: utf-8 -*-
"""金标回归：散号 streak、单调总需求第一可行档、fieldinfo 切批（零 pytest 依赖）。"""
import os
import sys
import unittest

_WEB_BOOKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _WEB_BOOKER_DIR not in sys.path:
    sys.path.insert(0, _WEB_BOOKER_DIR)

import app as booker  # noqa: E402


class TestScatterStreakBias(unittest.TestCase):
    def test_prefers_same_place_across_consecutive_hours(self):
        matrix = {
            "1": {"10:00": "available", "11:00": "available"},
            "2": {"10:00": "available", "11:00": "available"},
        }
        level_spec = {"10:00": 1, "11:00": 1}
        items = booker.scatter_pick_items_with_time_streak_bias(
            matrix,
            ["1", "2"],
            level_spec,
            ["10:00", "11:00"],
            {},
            False,
            0,
            0,
        )
        self.assertEqual(len(items), 2)
        t_to_p = {str(it["time"]): str(it["place"]) for it in items}
        self.assertEqual(t_to_p["10:00"], t_to_p["11:00"])


class TestBookableCell(unittest.TestCase):
    def test_locked_and_available_bookable(self):
        self.assertTrue(booker.is_matrix_cell_bookable_for_new_booking("available"))
        self.assertTrue(booker.is_matrix_cell_bookable_for_new_booking("locked"))
        self.assertFalse(booker.is_matrix_cell_bookable_for_new_booking("booked"))
        self.assertFalse(booker.is_matrix_cell_bookable_for_new_booking("mine"))
        self.assertFalse(booker.is_matrix_cell_bookable_for_new_booking(None))


class TestAggressiveBestTier(unittest.TestCase):
    def test_picks_feasible_tier_when_full_tier_impossible(self):
        """双时段满块因矩阵遮挡不可行；单调递降后首档可行（如单时段 2 连号）应返回有效 items。（遮挡用 booked；locked 与 available 同为可订）"""
        matrix = {
            "1": {"18:00": "available", "19:00": "booked"},
            "2": {"18:00": "available", "19:00": "booked"},
            "3": {"18:00": "booked", "19:00": "available"},
            "4": {"18:00": "booked", "19:00": "available"},
        }
        places = ["1", "2", "3", "4"]
        intent = {
            "target_blocks": 2,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 10,
            "require_consecutive": True,
            "solver_scoring": "auto_time_consecutive",
            "solver_aggressive_best_tier": True,
        }
        sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="aggressive")
        self.assertIsNotNone(sol)
        items = sol.get("items") or []
        self.assertEqual(len(items), 2)
        times = {str(it["time"]) for it in items}
        self.assertTrue(times == {"18:00"} or times == {"19:00"})
        places_used = sorted({str(it["place"]) for it in items})
        if times == {"18:00"}:
            self.assertEqual(places_used, ["1", "2"])
        else:
            self.assertEqual(places_used, ["3", "4"])


class TestMonotoneTotalBudgetLevels(unittest.TestCase):
    def test_two_by_two_sums_desc_unique(self):
        lv = booker._build_monotone_total_budget_levels(2, ["18:00", "19:00"], ["18:00", "19:00"])
        sums = [booker._sum_level_spec(x) for x in lv]
        self.assertEqual(sums, [4, 3, 2, 1])


class TestMonotoneMaxCells(unittest.TestCase):
    def test_full_two_by_two_places_gets_four_cells(self):
        matrix = {
            "1": {"18:00": "available", "19:00": "available"},
            "2": {"18:00": "available", "19:00": "available"},
        }
        places = ["1", "2"]
        intent = {
            "target_blocks": 2,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 10,
            "require_consecutive": True,
            "solver_scoring": "auto_time_consecutive",
            "solver_aggressive_best_tier": True,
        }
        prev = booker.CONFIG.get("delivery_monotone_total_downgrade")
        booker.CONFIG["delivery_monotone_total_downgrade"] = True
        try:
            sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="aggressive")
        finally:
            if prev is None:
                booker.CONFIG.pop("delivery_monotone_total_downgrade", None)
            else:
                booker.CONFIG["delivery_monotone_total_downgrade"] = prev
        self.assertIsNotNone(sol)
        items = sol.get("items") or []
        self.assertEqual(len(items), 4)
        self.assertEqual(booker._sum_level_spec(sol.get("level_spec") or {}), 4)

    def test_partial_occlusion_first_feasible_is_max_sum(self):
        """满 2×2 不可行时，第一可行档总格数应为力所能及的最大（此处为 3）。遮挡用 booked。"""
        matrix = {
            "1": {"18:00": "available", "19:00": "available"},
            "2": {"18:00": "available", "19:00": "booked"},
        }
        places = ["1", "2"]
        intent = {
            "target_blocks": 2,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 10,
            "require_consecutive": True,
            "solver_scoring": "auto_time_consecutive",
            "solver_aggressive_best_tier": False,
        }
        booker.CONFIG["delivery_monotone_total_downgrade"] = True
        sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="aggressive")
        self.assertIsNotNone(sol)
        items = sol.get("items") or []
        self.assertEqual(len(items), 3)
        self.assertEqual(booker._sum_level_spec(sol.get("level_spec") or {}), 3)


class TestMaxTotalCellsVsMonotoneChain(unittest.TestCase):
    """B=3、两时段、仅两连号位：规范单调链先可行为 18:00 两格；全 spec 枚举应拿到 18+19 各 2 格共 4 格。"""

    def test_enumeration_prefers_four_cells_over_two(self):
        matrix = {
            "15": {"18:00": "available", "19:00": "available"},
            "16": {"18:00": "available", "19:00": "available"},
        }
        places = ["15", "16"]
        intent = {
            "target_blocks": 3,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 50,
            "require_consecutive": True,
            "solver_scoring": "auto_time_consecutive",
            "solver_aggressive_best_tier": False,
            "solver_max_total_cells": True,
        }
        prev_m = booker.CONFIG.get("delivery_monotone_total_downgrade")
        booker.CONFIG["delivery_monotone_total_downgrade"] = True
        try:
            sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="aggressive")
        finally:
            if prev_m is None:
                booker.CONFIG.pop("delivery_monotone_total_downgrade", None)
            else:
                booker.CONFIG["delivery_monotone_total_downgrade"] = prev_m
        self.assertIsNotNone(sol)
        items = sol.get("items") or []
        self.assertEqual(len(items), 4)
        self.assertEqual(booker._sum_level_spec(sol.get("level_spec") or {}), 4)

    def test_monotone_first_feasible_when_max_total_off(self):
        matrix = {
            "15": {"18:00": "available", "19:00": "available"},
            "16": {"18:00": "available", "19:00": "available"},
        }
        places = ["15", "16"]
        intent = {
            "target_blocks": 3,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 50,
            "require_consecutive": True,
            "solver_scoring": "auto_time_consecutive",
            "solver_aggressive_best_tier": False,
            "solver_max_total_cells": False,
        }
        booker.CONFIG["delivery_monotone_total_downgrade"] = True
        sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="aggressive")
        self.assertIsNotNone(sol)
        items = sol.get("items") or []
        self.assertEqual(len(items), 2)
        self.assertEqual({str(it["time"]) for it in items}, {"18:00"})


class TestScatterCrossVenueFirstGroup(unittest.TestCase):
    def test_scatter_picks_across_places_and_times(self):
        matrix = {
            "1": {"18:00": "available", "19:00": "booked"},
            "2": {"18:00": "booked", "19:00": "available"},
        }
        places = ["1", "2"]
        intent = {
            "target_blocks": 1,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 10,
            "require_consecutive": False,
            "solver_scoring": "auto_time_consecutive",
            "solver_max_total_cells": True,
        }
        sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="aggressive")
        self.assertIsNotNone(sol)
        items = sol.get("items") or []
        self.assertEqual(len(items), 2)
        by_t = {str(it["time"]): str(it["place"]) for it in items}
        self.assertEqual(by_t.get("18:00"), "1")
        self.assertEqual(by_t.get("19:00"), "2")


class TestMonotoneNeedRelaxMaxTotal(unittest.TestCase):
    """strict + monotone_need_relax 与 aggressive 总预算枚举共用「按 S 全 spec + 择优」逻辑。"""

    def test_refill_relax_finds_four_cells_under_caps(self):
        matrix = {
            "15": {"18:00": "available", "19:00": "available"},
            "16": {"18:00": "available", "19:00": "available"},
        }
        places = ["15", "16"]
        intent = {
            "target_blocks": 3,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 50,
            "require_consecutive": True,
            "solver_scoring": "auto_time_consecutive",
            "need_by_time": {"18:00": 3, "19:00": 3},
            "monotone_need_relax": True,
            "solver_max_total_cells": True,
        }
        sol = booker.solve_candidate_from_matrix(matrix, places, intent, mode="strict")
        self.assertIsNotNone(sol)
        self.assertEqual(len(sol.get("items") or []), 4)
        self.assertEqual(booker._sum_level_spec(sol.get("level_spec") or {}), 4)


class TestRefillPolicyMonotone(unittest.TestCase):
    def test_auto_campaign_uses_monotone_tier_label_when_feasible(self):
        matrix = {
            "1": {"18:00": "available", "19:00": "available"},
            "2": {"18:00": "available", "19:00": "available"},
        }
        places = ["1", "2"]
        intent_base = {
            "target_blocks": 2,
            "target_times": ["18:00", "19:00"],
            "time_preference_order": ["18:00", "19:00"],
            "preferred_place_min": 0,
            "preferred_place_max": 0,
            "selectable_place_min": 1,
            "selectable_place_max": 10,
        }
        prev = booker.CONFIG.get("delivery_monotone_total_downgrade")
        booker.CONFIG["delivery_monotone_total_downgrade"] = True
        try:
            solved, used, tier = booker.solve_refill_with_policy(
                matrix,
                places,
                intent_base,
                {"18:00": 1, "19:00": 1},
                True,
                booker.REFILL_POLICY_AUTO_CAMPAIGN,
            )
        finally:
            if prev is None:
                booker.CONFIG.pop("delivery_monotone_total_downgrade", None)
            else:
                booker.CONFIG["delivery_monotone_total_downgrade"] = prev
        self.assertIsNotNone(solved)
        self.assertEqual(tier, "单调缺口递降")
        self.assertEqual(sum(int(v) for v in used.values()), 2)


class TestMergeTaskVenueStrategy(unittest.TestCase):
    def test_first_group_matrix_clamps_blocks_to_primary_derived(self):
        primary = [
            {"place": "6", "time": "18:00"},
            {"place": "7", "time": "18:00"},
            {"place": "6", "time": "19:00"},
            {"place": "7", "time": "19:00"},
        ]
        task_cfg = {
            "delivery_first_group_from_matrix": True,
            "delivery_target_blocks": 3,
            "delivery_first_group_times": ["18:00", "19:00"],
        }
        merged = booker._merge_task_venue_strategy(task_cfg, primary)
        self.assertEqual(merged["delivery_target_blocks"], 2)

    def test_first_group_off_uses_task_target_blocks(self):
        primary = [
            {"place": "6", "time": "18:00"},
            {"place": "7", "time": "18:00"},
        ]
        task_cfg = {"delivery_first_group_from_matrix": False, "delivery_target_blocks": 3}
        merged = booker._merge_task_venue_strategy(task_cfg, primary)
        self.assertEqual(merged["delivery_target_blocks"], 3)

    def test_first_group_allow_scatter_merged_from_task(self):
        primary = [{"place": "6", "time": "18:00"}]
        merged = booker._merge_task_venue_strategy(
            {"delivery_first_group_allow_scatter": True}, primary
        )
        self.assertTrue(merged.get("delivery_first_group_allow_scatter"))


class TestValidateTaskVenueStrategy(unittest.TestCase):
    def test_matrix_on_empty_first_times_ok(self):
        errs = booker.validate_task_venue_strategy({"delivery_first_group_from_matrix": True})
        self.assertEqual(errs, [])

    def test_matrix_off_skips_first_group_checks(self):
        errs = booker.validate_task_venue_strategy(
            {"delivery_first_group_from_matrix": False, "delivery_first_group_times": []}
        )
        self.assertEqual(errs, [])

    def test_matrix_on_preference_without_times_errors(self):
        errs = booker.validate_task_venue_strategy(
            {
                "delivery_first_group_from_matrix": True,
                "delivery_first_group_time_preference_order": ["18:00"],
            }
        )
        self.assertTrue(len(errs) >= 1)

    def test_matrix_on_times_and_order_consistent_ok(self):
        errs = booker.validate_task_venue_strategy(
            {
                "delivery_first_group_from_matrix": True,
                "delivery_first_group_times": ["18:00", "19:00"],
                "delivery_first_group_time_preference_order": ["19:00", "18:00"],
            }
        )
        self.assertEqual(errs, [])


class TestFieldinfoChunking(unittest.TestCase):
    def test_legal_batches_split_by_max_slot_count(self):
        items = [{"place": "1", "time": "%02d:00" % h} for h in (10, 11, 12, 13)]
        batches = booker.legal_batches_from_booking_record_chunking(items, max_records_per_post=1, max_slot_count=2)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 2)
        self.assertEqual(len(batches[1]), 2)

    def test_build_field_info_respects_max_hours(self):
        cfg = booker.CONFIG
        prev = cfg.get("delivery_max_fieldinfo_hours")
        try:
            cfg["delivery_max_fieldinfo_hours"] = 2
            cli = booker.ApiClient(inherit_global_auth=False)
            items = [{"place": "5", "time": "%02d:00" % h} for h in (10, 11, 12, 13)]
            lst, total = cli._build_field_info_list("2026-06-01", items)
            self.assertEqual(len(lst), 2)
            self.assertEqual(lst[0]["startTime"], "10:00")
            self.assertEqual(lst[0]["endTime"], "12:00")
            self.assertEqual(lst[1]["startTime"], "12:00")
            self.assertEqual(lst[1]["endTime"], "14:00")
            self.assertEqual(total, lst[0]["newMoney"] + lst[1]["newMoney"])
        finally:
            if prev is None:
                cfg.pop("delivery_max_fieldinfo_hours", None)
            else:
                cfg["delivery_max_fieldinfo_hours"] = prev


if __name__ == "__main__":
    unittest.main()
