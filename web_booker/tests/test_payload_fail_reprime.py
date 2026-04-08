# -*- coding: utf-8 -*-
"""极速递送：数据错误后同批重试前会重拉矩阵（零 pytest 依赖）。"""
import os
import sys
import unittest
from unittest.mock import patch

_WEB_BOOKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _WEB_BOOKER_DIR not in sys.path:
    sys.path.insert(0, _WEB_BOOKER_DIR)

import app as booker  # noqa: E402


def _minimal_matrix():
    return {
        "places": ["1"],
        "times": ["10:00"],
        "matrix": {"1": {"10:00": "available"}},
    }


class TestPayloadFailReprime(unittest.TestCase):
    def test_data_error_triggers_extra_get_matrix_before_second_post(self):
        c = booker.ApiClient(inherit_global_auth=False)
        c.token = "t"
        c.shop_num = "1001"
        c.card_index = "0"
        c.card_st_id = "cs"
        c.delivery_max_places_per_timeslot = 3

        gm_calls = []

        def fake_get_matrix(date_str, include_mine_overlay=True, request_timeout=None, bypass_cache=False):
            gm_calls.append(
                {
                    "date": date_str,
                    "bypass": bypass_cache,
                }
            )
            return _minimal_matrix()

        err = {
            "ok": True,
            "status_code": 200,
            "resp_data": {"msg": "fail", "data": "数据错误，请重试"},
            "raw_text": "{}",
            "raw_message": "数据错误，请重试",
            "elapsed_ms": 1,
        }
        ok = {
            "ok": True,
            "status_code": 200,
            "resp_data": {"msg": "success"},
            "raw_text": "{}",
            "raw_message": "success",
            "elapsed_ms": 1,
        }

        post_calls = []

        def fake_post_once(sess, hdrs, url, body, timeout_s):
            post_calls.append(1)
            return err if len(post_calls) == 1 else ok

        groups = [
            {
                "id": "primary",
                "label": "主",
                "items": [{"place": "1", "time": "10:00"}],
            }
        ]
        tc = {
            "delivery_target_blocks": 1,
            "delivery_target_times": ["10:00"],
            "delivery_time_preference_order": ["10:00"],
            "delivery_matrix_place_min": 1,
            "delivery_matrix_place_max": 14,
        }

        with patch.object(c, "get_matrix", side_effect=fake_get_matrix):
            with patch.object(c, "_post_reservation_once", side_effect=fake_post_once):
                res = c.submit_delivery_campaign(
                    "2026-04-12",
                    groups,
                    submit_profile="auto_minimal",
                    task_config=tc,
                    skip_warmup=True,
                )

        self.assertEqual(res.get("status"), "success")
        rm = res.get("run_metric") or {}
        self.assertGreaterEqual(int(rm.get("payload_fail_reprime_count") or 0), 1)
        # 首轮循环 get_matrix + 同批内重拉 + 主单成功后下一轮 get_matrix
        self.assertGreaterEqual(len(gm_calls), 3)
        self.assertTrue(all(x.get("bypass") for x in gm_calls))
        self.assertEqual(len(post_calls), 2)

    def test_no_reprime_when_first_post_succeeds(self):
        c = booker.ApiClient(inherit_global_auth=False)
        c.token = "t"
        c.shop_num = "1001"
        c.card_index = "0"
        c.card_st_id = "cs"
        c.delivery_max_places_per_timeslot = 3

        gm_calls = []

        def fake_get_matrix(date_str, include_mine_overlay=True, request_timeout=None, bypass_cache=False):
            gm_calls.append(1)
            return _minimal_matrix()

        ok = {
            "ok": True,
            "status_code": 200,
            "resp_data": {"msg": "success"},
            "raw_text": "{}",
            "raw_message": "success",
            "elapsed_ms": 1,
        }

        post_calls = []

        def fake_post_once(sess, hdrs, url, body, timeout_s):
            post_calls.append(1)
            return ok

        groups = [
            {
                "id": "primary",
                "label": "主",
                "items": [{"place": "1", "time": "10:00"}],
            }
        ]
        tc = {
            "delivery_target_blocks": 1,
            "delivery_target_times": ["10:00"],
            "delivery_time_preference_order": ["10:00"],
            "delivery_matrix_place_min": 1,
            "delivery_matrix_place_max": 14,
        }

        with patch.object(c, "get_matrix", side_effect=fake_get_matrix):
            with patch.object(c, "_post_reservation_once", side_effect=fake_post_once):
                res = c.submit_delivery_campaign(
                    "2026-04-12",
                    groups,
                    submit_profile="auto_minimal",
                    task_config=tc,
                    skip_warmup=True,
                )

        self.assertEqual(res.get("status"), "success")
        rm = res.get("run_metric") or {}
        self.assertEqual(int(rm.get("payload_fail_reprime_count") or 0), 0)
        self.assertEqual(len(post_calls), 1)
        self.assertGreaterEqual(len(gm_calls), 2)


if __name__ == "__main__":
    unittest.main()
