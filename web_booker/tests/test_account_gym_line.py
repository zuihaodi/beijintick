# -*- coding: utf-8 -*-
"""账号 gym_connect_ip 白名单与 ApiClient 线路解析（零 pytest 依赖）。"""
import os
import sys
import unittest

_WEB_BOOKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _WEB_BOOKER_DIR not in sys.path:
    sys.path.insert(0, _WEB_BOOKER_DIR)

import app as booker  # noqa: E402


def _acc(**kwargs):
    base = {
        "id": "acc_t",
        "name": "t",
        "token": "tok",
        "shop_num": "1001",
        "delivery_max_places_per_timeslot": 2,
    }
    base.update(kwargs)
    return base


class TestGymConnectIpNormalize(unittest.TestCase):
    def test_whitelisted_ip_kept(self):
        a = booker._normalize_account_item(_acc(gym_connect_ip="114.247.63.124"), 0)
        self.assertEqual(a["gym_connect_ip"], "114.247.63.124")

    def test_second_line(self):
        a = booker._normalize_account_item(_acc(gym_connect_ip="60.247.76.34"), 0)
        self.assertEqual(a["gym_connect_ip"], "60.247.76.34")

    def test_unknown_ip_cleared(self):
        a = booker._normalize_account_item(_acc(gym_connect_ip="1.2.3.4"), 0)
        self.assertEqual(a["gym_connect_ip"], "")

    def test_empty_means_dns(self):
        a = booker._normalize_account_item(_acc(), 0)
        self.assertEqual(a["gym_connect_ip"], "")


class TestApiClientGymTcpNetloc(unittest.TestCase):
    def test_dns_mode_uses_domain(self):
        c = booker.ApiClient(inherit_global_auth=False)
        c.gym_connect_ip = ""
        self.assertEqual(c._gym_tcp_netloc(), booker.GYM_API_TARGET_HOST)

    def test_forced_ip(self):
        c = booker.ApiClient(inherit_global_auth=False)
        c.gym_connect_ip = "60.247.76.34"
        self.assertEqual(c._gym_tcp_netloc(), "60.247.76.34")

    def test_url_helper(self):
        c = booker.ApiClient(inherit_global_auth=False)
        c.gym_connect_ip = "114.247.63.124"
        u = c._gym_https_url("easyserpClient/place/getPlaceInfoByShortName")
        self.assertTrue(u.startswith("https://114.247.63.124/easyserpClient/"))


if __name__ == "__main__":
    unittest.main()
