# -*- coding: utf-8 -*-
import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    normalize_order_schedule_date_key,
    _order_row_eligible_for_mine_slots,
    matrix_cell_indicates_mine,
    mine_overview_window_dates,
    count_mine_cells_in_matrix,
)


class TestMineOrderParse(unittest.TestCase):
    def test_normalize_date_variants(self):
        self.assertEqual(normalize_order_schedule_date_key("2026-04-04"), "2026-04-04")
        self.assertEqual(normalize_order_schedule_date_key("2026/4/4"), "2026-04-04")
        self.assertEqual(normalize_order_schedule_date_key("2026-04-04T00:00:00"), "2026-04-04")
        self.assertEqual(normalize_order_schedule_date_key("2026-04-04 12:00:00"), "2026-04-04")

    def test_show_status_missing_still_eligible(self):
        o = {"prestatus": "等待", "jsonArray": []}
        self.assertTrue(_order_row_eligible_for_mine_slots(o))

    def test_show_status_zero_eligible(self):
        o = {"showStatus": "0", "prestatus": "等待", "jsonArray": []}
        self.assertTrue(_order_row_eligible_for_mine_slots(o))

    def test_cancelled_out(self):
        o = {"showStatus": "0", "prestatus": "取消", "jsonArray": []}
        self.assertFalse(_order_row_eligible_for_mine_slots(o))

    def test_matrix_cell_mine(self):
        self.assertTrue(matrix_cell_indicates_mine("mine"))
        self.assertTrue(matrix_cell_indicates_mine(2))
        self.assertFalse(matrix_cell_indicates_mine("available"))
        self.assertFalse(matrix_cell_indicates_mine(1))

    def test_mine_window_len(self):
        self.assertEqual(len(mine_overview_window_dates(8)), 8)

    def test_count_mine_cells(self):
        m = {"1": {"10:00": "mine", "11:00": "available"}, "2": {"10:00": 2}}
        self.assertEqual(count_mine_cells_in_matrix(m), 2)


if __name__ == "__main__":
    unittest.main()
