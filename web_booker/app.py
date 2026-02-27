"""
变更记录（手动维护）:
- 2026-02-09 03:29 保留健康检查调度并统一任务通知/结果上报
- 2026-02-09 04:10 健康检查增加起始时间并在前端显示预计下次检查
- 2026-02-09 04:40 接入 PushPlus 并增加微信通知配置入口
"""

from flask import Flask, render_template, request, jsonify
from jinja2 import Environment, TemplateSyntaxError
import requests
import json
import urllib.parse
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timedelta, timezone
import traceback
import schedule
import time
import threading
import os
import hashlib
import re
import random
import builtins


def timestamped_print(*args, **kwargs):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args:
        args = (f"[{ts}] {args[0]}",) + args[1:]
    else:
        args = (f"[{ts}]",)
    return builtins.print(*args, **kwargs)


print = timestamped_print

HEALTH_CHECK_NEXT_RUN = None


class StateSampler:
    """按秒聚合 state 分布（仅计数）并给出 locked 推荐。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._bucket = {}
        self._max_buckets = 300

    def ingest(self, raw_list):
        now_sec = int(time.time())
        counts = {}
        for place in raw_list or []:
            for slot in place.get('projectInfo', []) or []:
                try:
                    key = int(slot.get('state'))
                except Exception:
                    key = -999
                counts[key] = counts.get(key, 0) + 1
        with self._lock:
            self._bucket[now_sec] = counts
            stale_before = now_sec - self._max_buckets
            for ts in list(self._bucket.keys()):
                if ts < stale_before:
                    self._bucket.pop(ts, None)

    def snapshot(self):
        with self._lock:
            data = dict(self._bucket)
        merged = {}
        for v in data.values():
            for s, c in v.items():
                merged[s] = merged.get(s, 0) + int(c)

        available_count = merged.get(1, 0)
        locked_recommend = []
        for state, cnt in sorted(merged.items(), key=lambda x: (-x[1], x[0])):
            if state in (1, 4, -999):
                continue
            if cnt <= 0:
                continue
            if cnt >= max(5, int(available_count * 0.05)):
                locked_recommend.append(state)

        return {
            'seconds': len(data),
            'states': merged,
            'recommended_locked_states': locked_recommend,
        }


STATE_SAMPLER = StateSampler()


def normalize_time_str(value):
    if not value:
        return None
    if isinstance(value, str):
        value = value.strip()
        try:
            dt = datetime.strptime(value, "%H:%M")
            return dt.strftime("%H:%M")
        except ValueError:
            return None
    return None

# 定期健康检查的函数
def health_check():
    """
    定期检查获取场地状态是否正常，并发送短信通知。
    """
    phones = CONFIG.get('notification_phones') or []
    pushplus_tokens = CONFIG.get('pushplus_tokens') or []
    today = datetime.now().strftime("%Y-%m-%d")
    matrix_res = client.get_matrix(today)
    if "error" in matrix_res:
        err_msg = matrix_res["error"]
        log(f"❌ 健康检查失败: 获取场地状态异常: {err_msg}")
        if phones:
            task_manager.send_notification(f"⚠️ 健康检查失败：获取场地状态异常({err_msg})", phones=phones)
        if pushplus_tokens:
            task_manager.send_wechat_notification(
                f"⚠️ 健康检查失败：获取场地状态异常({err_msg})",
                tokens=pushplus_tokens,
            )
    else:
        booking_probe = client.check_booking_auth_probe()
        if booking_probe.get('ok') and booking_probe.get('unknown'):
            log(f"✅ 健康检查通过：场地状态获取正常；⚠️ 下单链路仅完成探测，结果未确认( {booking_probe.get('msg')} )")
        elif booking_probe.get('ok'):
            log("✅ 健康检查通过：场地状态获取正常；下单鉴权探测未见明显异常")
        else:
            if booking_probe.get('unknown'):
                log(f"✅ 健康检查通过：场地状态获取正常；⚠️ 下单链路探测异常/未知( {booking_probe.get('msg')} )")
            else:
                log(f"⚠️ 健康检查：查询正常，但下单链路疑似鉴权异常( {booking_probe.get('msg')} )")

# 每隔一段时间执行健康检查
def schedule_health_check():
    """
    定时任务：按照配置的间隔时间运行健康检查。
    """
    # 清理已有的健康检查任务，避免重复调度
    schedule.clear("health_check")

    if not CONFIG.get('health_check_enabled', True):
        print("🛑 健康检查已关闭，不安排定时任务。")
        return

    check_interval = CONFIG.get('health_check_interval_min', 30)
    try:
        check_interval = float(check_interval)
    except (TypeError, ValueError):
        check_interval = 30.0
    if check_interval < 1:
        check_interval = 1
    start_time = CONFIG.get('health_check_start_time', '00:00')
    start_time = normalize_time_str(start_time) or '00:00'

    def compute_next_run():
        now = datetime.now()
        start_dt = datetime.strptime(
            f"{now.strftime('%Y-%m-%d')} {start_time}", "%Y-%m-%d %H:%M"
        )
        if now <= start_dt:
            return start_dt
        elapsed = (now - start_dt).total_seconds() / 60.0
        steps = int(elapsed // check_interval) + 1
        return start_dt + timedelta(minutes=steps * check_interval)

    def health_check_tick():
        global HEALTH_CHECK_NEXT_RUN
        if HEALTH_CHECK_NEXT_RUN is None:
            HEALTH_CHECK_NEXT_RUN = compute_next_run()
        if datetime.now() >= HEALTH_CHECK_NEXT_RUN:
            health_check()
            HEALTH_CHECK_NEXT_RUN = HEALTH_CHECK_NEXT_RUN + timedelta(minutes=check_interval)

    global HEALTH_CHECK_NEXT_RUN
    HEALTH_CHECK_NEXT_RUN = compute_next_run()
    schedule.every(1).minutes.do(health_check_tick).tag("health_check")
    print(
        f"📅 健康检查已安排，起始时间 {start_time}，每 {check_interval} 分钟执行一次."
    )


app = Flask(__name__)

# ================= 配置 =================
CONFIG = {
    "auth": {
        "token": "oy9Aj1fKpR3Yxwd6iV7VIlg3Vo-A", # 请确保有效
        "cookie": "JSESSIONID=FFE6C0633F33D9CE71354D0D1110AC0D",
        "card_index": "0873612446",
        "card_st_id": "289", 
        "shop_num": "1001"
    },
    "sms": {
        "user": "18600291931",
        "api_key": "6127d94d28a04c06a8f61b70eac79cc3"
    },
    "notification_phones": [],
    "pushplus_tokens": [],
    "retry_interval": 1.0,
    "aggressive_retry_interval": 1.0,
    "batch_retry_times": 2,
    "batch_retry_interval": 0.5,
    "submit_batch_size": 3,
    "initial_submit_batch_size": 2,
    "submit_timeout_seconds": 4.0,
    "submit_split_retry_times": 1,
    "batch_min_interval": 0.8,
    "order_query_timeout_seconds": 2.5,
    "order_query_max_pages": 2,
    "post_submit_orders_join_timeout_seconds": 1.2,
    "post_submit_verify_orders_on_matrix_partial_only": True,
    "post_submit_orders_sync_fallback": False,
    "post_submit_verify_pending_retry_seconds": 0.35,
    "post_submit_treat_verify_timeout_as_retry": True,
    "refill_window_seconds": 8.0,
    "locked_retry_interval": 1.0,  # ✅ 新增：锁定状态重试间隔(秒)
    "locked_max_seconds": 60,  # ✅ 新增：锁定状态最多刷 N 秒
    "locked_state_values": [2, 3, 5, 6],  # 接口 state 落在这些值时视为“锁定/暂不可下单”
    "open_retry_seconds": 30,  # ✅ 新增：已开放无组合时继续重试窗口(秒)
    "matrix_timeout_seconds": 3.0,  # 高峰查询超时(秒)，建议短超时+高频重试
    "stop_on_none_stage_without_refill": False,  # pipeline 阶段结束且无 refill 时是否立即结束
    # 🔍 新增：凭证健康检查
    "health_check_enabled": True,      # 是否开启自动健康检查
    "health_check_interval_min": 30.0, # 检查间隔（分钟）
    "health_check_start_time": "00:00", # 起始时间 (HH:MM)
    "verbose_logs": False,  # 是否打印高频调试日志
    "same_time_precheck_limit": 0,  # 同时段预检上限；<=0 表示关闭预检
    "biz_fail_cooldown_seconds": 15.0,  # pipeline 中业务失败组合冷却时间
    "preselect_enabled": True,
    "preselect_ttl_seconds": 2.0,
    "preselect_only_before_first_submit": True,
}


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_TEMPLATE_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_FILE = CONFIG_TEMPLATE_FILE
LOG_BUFFER = []
MAX_LOG_SIZE = 500
MAX_TARGET_COUNT = 9
REFILL_TASKS_FILE = os.path.join(BASE_DIR, "refill_tasks.json")
TASK_RUN_METRICS_FILE = os.path.join(BASE_DIR, "task_run_metrics.json")
_TASK_RUN_METRICS_LOCK = threading.Lock()


def _percentile(sorted_values, p):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = int(round((len(sorted_values) - 1) * float(p)))
    idx = max(0, min(len(sorted_values) - 1, idx))
    return float(sorted_values[idx])


def append_task_run_metric(record, keep_last=200):
    try:
        with _TASK_RUN_METRICS_LOCK:
            old = []
            if os.path.exists(TASK_RUN_METRICS_FILE):
                try:
                    with open(TASK_RUN_METRICS_FILE, 'r', encoding='utf-8') as f:
                        old = json.load(f) or []
                except Exception:
                    old = []
            if not isinstance(old, list):
                old = []
            old.append(record)
            old = old[-max(10, int(keep_last or 200)):]
            with open(TASK_RUN_METRICS_FILE, 'w', encoding='utf-8') as f:
                json.dump(old, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 任务指标写入失败: {e}")

def log(msg):
    """记录日志到内存缓冲区和控制台"""
    print(msg)
    timestamp = datetime.now().strftime("%H:%M:%S")
    LOG_BUFFER.append(f"[{timestamp}] {msg}")
    if len(LOG_BUFFER) > MAX_LOG_SIZE:
        LOG_BUFFER.pop(0)


def is_verbose_logs_enabled():
    return bool(CONFIG.get("verbose_logs", False))

if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
            if 'notification_phones' in saved:
                CONFIG['notification_phones'] = saved['notification_phones']
            if 'pushplus_tokens' in saved:
                CONFIG['pushplus_tokens'] = saved['pushplus_tokens']
            if 'retry_interval' in saved:
                CONFIG['retry_interval'] = saved['retry_interval']
            if 'aggressive_retry_interval' in saved:
                CONFIG['aggressive_retry_interval'] = saved['aggressive_retry_interval']
            if 'batch_retry_times' in saved:
                CONFIG['batch_retry_times'] = saved['batch_retry_times']
            if 'batch_retry_interval' in saved:
                CONFIG['batch_retry_interval'] = saved['batch_retry_interval']
            if 'submit_batch_size' in saved:
                CONFIG['submit_batch_size'] = saved['submit_batch_size']
            if 'initial_submit_batch_size' in saved:
                try:
                    CONFIG['initial_submit_batch_size'] = max(1, min(9, int(saved['initial_submit_batch_size'])))
                except Exception:
                    pass
            if 'submit_timeout_seconds' in saved:
                try:
                    CONFIG['submit_timeout_seconds'] = max(0.5, float(saved['submit_timeout_seconds']))
                except Exception:
                    pass
            if 'submit_split_retry_times' in saved:
                try:
                    CONFIG['submit_split_retry_times'] = max(0, min(3, int(saved['submit_split_retry_times'])))
                except Exception:
                    pass
            if 'batch_min_interval' in saved:
                CONFIG['batch_min_interval'] = saved['batch_min_interval']
            if 'order_query_timeout_seconds' in saved:
                try:
                    CONFIG['order_query_timeout_seconds'] = max(0.5, float(saved['order_query_timeout_seconds']))
                except Exception:
                    pass
            if 'order_query_max_pages' in saved:
                try:
                    CONFIG['order_query_max_pages'] = max(1, min(10, int(saved['order_query_max_pages'])))
                except Exception:
                    pass
            if 'post_submit_orders_join_timeout_seconds' in saved:
                try:
                    CONFIG['post_submit_orders_join_timeout_seconds'] = max(0.1, float(saved['post_submit_orders_join_timeout_seconds']))
                except Exception:
                    pass
            if 'post_submit_verify_orders_on_matrix_partial_only' in saved:
                CONFIG['post_submit_verify_orders_on_matrix_partial_only'] = bool(saved['post_submit_verify_orders_on_matrix_partial_only'])
            if 'post_submit_orders_sync_fallback' in saved:
                CONFIG['post_submit_orders_sync_fallback'] = bool(saved['post_submit_orders_sync_fallback'])
            if 'post_submit_verify_pending_retry_seconds' in saved:
                try:
                    CONFIG['post_submit_verify_pending_retry_seconds'] = max(0.05, float(saved['post_submit_verify_pending_retry_seconds']))
                except Exception:
                    pass
            if 'post_submit_treat_verify_timeout_as_retry' in saved:
                CONFIG['post_submit_treat_verify_timeout_as_retry'] = bool(saved['post_submit_treat_verify_timeout_as_retry'])
            if 'refill_window_seconds' in saved:
                CONFIG['refill_window_seconds'] = saved['refill_window_seconds']
            # ✅ 新增：锁定重试的两个配置
            if 'locked_retry_interval' in saved:
                CONFIG['locked_retry_interval'] = saved['locked_retry_interval']
            if 'locked_max_seconds' in saved:
                CONFIG['locked_max_seconds'] = saved['locked_max_seconds']
            if 'locked_state_values' in saved and isinstance(saved['locked_state_values'], list):
                parsed_locked_states = []
                for v in saved['locked_state_values']:
                    try:
                        parsed_locked_states.append(int(v))
                    except Exception:
                        continue
                if parsed_locked_states:
                    CONFIG['locked_state_values'] = parsed_locked_states
            if 'open_retry_seconds' in saved:
                CONFIG['open_retry_seconds'] = saved['open_retry_seconds']
            if 'matrix_timeout_seconds' in saved:
                try:
                    CONFIG['matrix_timeout_seconds'] = max(0.5, float(saved['matrix_timeout_seconds']))
                except Exception:
                    pass
            if 'stop_on_none_stage_without_refill' in saved:
                CONFIG['stop_on_none_stage_without_refill'] = bool(saved['stop_on_none_stage_without_refill'])
            if 'health_check_enabled' in saved:
                CONFIG['health_check_enabled'] = saved['health_check_enabled']
            if 'health_check_interval_min' in saved:
                CONFIG['health_check_interval_min'] = saved['health_check_interval_min']
            if 'health_check_start_time' in saved:
                CONFIG['health_check_start_time'] = normalize_time_str(saved['health_check_start_time']) or CONFIG['health_check_start_time']
            if 'verbose_logs' in saved:
                CONFIG['verbose_logs'] = bool(saved['verbose_logs'])
            if 'preselect_enabled' in saved:
                CONFIG['preselect_enabled'] = bool(saved['preselect_enabled'])
            if 'preselect_ttl_seconds' in saved:
                try:
                    CONFIG['preselect_ttl_seconds'] = max(0.2, float(saved['preselect_ttl_seconds']))
                except Exception:
                    pass
            if 'preselect_only_before_first_submit' in saved:
                CONFIG['preselect_only_before_first_submit'] = bool(saved['preselect_only_before_first_submit'])
            if 'same_time_precheck_limit' in saved:
                try:
                    CONFIG['same_time_precheck_limit'] = int(saved['same_time_precheck_limit'])
                except Exception:
                    pass
            if 'biz_fail_cooldown_seconds' in saved:
                try:
                    CONFIG['biz_fail_cooldown_seconds'] = max(1.0, float(saved['biz_fail_cooldown_seconds']))
                except Exception:
                    pass
            if 'auth' in saved:
                # 覆盖默认的 auth 配置
                CONFIG['auth'].update(saved['auth'])
    except Exception as e:
        print(f"加载配置失败: {e}")

TASKS_TEMPLATE_FILE = os.path.join(BASE_DIR, "tasks.json")
TASKS_FILE = TASKS_TEMPLATE_FILE

class ApiClient:
    def __init__(self):
        self.host = "gymvip.bfsu.edu.cn"
        self.headers = {
            "Host": self.host,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254162e) XWEB/18151 Flue",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"https://{self.host}",
            "Referer": f"https://{self.host}/easyserp/index.html",
        }
        cookie = str(CONFIG["auth"].get("cookie", "")).strip()
        if cookie:
            self.headers["Cookie"] = cookie
        self.token = CONFIG["auth"]["token"]
        self.session = requests.Session()
        self.server_time_offset_seconds = 0.0
        self._matrix_cache = {}
        self._matrix_cache_window_s = 0.12
        self._matrix_cache_lock = threading.Lock()

    def _update_server_time_offset(self, resp, started_at, ended_at):
        date_header = (resp.headers or {}).get("Date") if resp is not None else None
        if not date_header:
            return
        try:
            from email.utils import parsedate_to_datetime
            server_dt = parsedate_to_datetime(date_header)
            if server_dt.tzinfo is None:
                server_dt = server_dt.replace(tzinfo=timezone.utc)
            server_ts = server_dt.timestamp()
            midpoint = (started_at + ended_at) / 2.0
            self.server_time_offset_seconds = server_ts - midpoint
        except Exception:
            return

    def get_aligned_now(self):
        return datetime.now() + timedelta(seconds=float(self.server_time_offset_seconds or 0.0))

    def check_token(self):
        # 简单请求一次接口，看是否返回 token 失效相关的错误
        # 这里用获取矩阵接口测试，因为它只读且轻量
        today = datetime.now().strftime("%Y-%m-%d")
        res = self.get_matrix(today)
        
        # 假设接口返回 msg 包含 "token" 或 "登录" 字样代表失效
        # 具体根据实际抓包错误码调整
        if "error" in res:
            err = res["error"]
            # 扩展关键词：增加 "失效", "凭证", "-1"
            if any(k in err.lower() for k in ["token", "登录", "session", "失效", "凭证", "-1"]):
                return False, err
        return True, "Valid"

    def check_booking_auth_probe(self):
        """
        尝试用“无效业务参数”的轻量请求探测 reservationPlace 鉴权链路。
        说明：此探测不提交有效场次，不会产生真实订单；
        仅用于区分“鉴权失败”和“业务参数错误/未知”。
        """
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"
        probe_body = (
            f"token={self.token}&"
            f"shopNum={CONFIG['auth']['shop_num']}&"
            f"fieldinfo=%5B%5D&"
            f"cardStId={CONFIG['auth'].get('card_st_id', '')}&"
            f"oldTotal=0.00&"
            f"cardPayType=0&"
            f"type=&"
            f"offerId=&"
            f"offerType=&"
            f"total=0.00&"
            f"premerother=&"
            f"cardIndex={CONFIG['auth'].get('card_index', '')}"
        )

        try:
            resp = self.session.post(url, headers=self.headers, data=probe_body, timeout=10, verify=False)
            text = (resp.text or '').strip()
            data = None
            try:
                data = resp.json()
            except Exception:
                data = None

            msg_raw = ''
            if isinstance(data, dict):
                msg_raw = str(data.get('msg') or data.get('data') or '')
            if not msg_raw:
                msg_raw = text[:160]
            msg_l = msg_raw.lower()

            auth_keywords = ['token', 'session', '登录', '失效', '凭证', '-1', '未登录']
            if any(k in msg_l for k in auth_keywords):
                return {'ok': False, 'unknown': False, 'msg': msg_raw}

            # 能走到这里通常说明接口可达且未被直接鉴权拦截；
            # 但由于是无效业务参数探测，不能视为“下单一定成功”。
            return {'ok': True, 'unknown': True, 'msg': f"探测响应: {msg_raw}"}
        except Exception as e:
            return {'ok': False, 'unknown': True, 'msg': f"探测异常: {e}"}

    def get_place_orders(self, page_size=20, max_pages=6, timeout_s=10):
        """获取我的场地订单列表（用于识别 mine 状态）。"""
        url = f"https://{self.host}/easyserpClient/place/getPlaceOrder"
        all_orders = []

        for page_no in range(max_pages):
            params = {
                "pageNo": page_no,
                "pageSize": page_size,
                "shopNum": CONFIG["auth"]["shop_num"],
                "token": self.token,
            }
            try:
                resp = self.session.get(
                    url,
                    headers=self.headers,
                    params=params,
                    timeout=max(0.5, float(timeout_s or 10)),
                    verify=False,
                )
                data = resp.json()
            except Exception as e:
                return {"error": f"获取订单失败: {e}"}

            if not isinstance(data, dict):
                return {"error": f"订单接口返回格式错误: {data}"}
            if data.get("msg") != "success":
                return {"error": f"订单接口返回异常: {data.get('msg')}"}

            page_items = data.get("data") or []
            if not isinstance(page_items, list):
                page_items = []

            all_orders.extend(page_items)
            if len(page_items) < page_size:
                break

        return {"data": all_orders}

    def _extract_mine_slots(self, orders, target_date):
        """把订单列表转换为 mine 格子集合，格式: {(place, HH:MM)}。"""
        mine_slots = set()
        for order in orders:
            if str(order.get("showStatus", "")) != "0":
                continue
            if str(order.get("prestatus", "")).strip() in ("取消", "已取消"):
                continue

            arr = order.get("jsonArray") or []
            if not isinstance(arr, list):
                continue

            for seg in arr:
                if str(seg.get("reversionDate", "")).strip() != target_date:
                    continue

                site_name = str(seg.get("siteName", ""))
                m = re.search(r"(\d+)", site_name)
                if not m:
                    continue
                place = m.group(1)

                start = str(seg.get("start", "")).strip()
                end = str(seg.get("end", "")).strip()
                try:
                    start_dt = datetime.strptime(start, "%H:%M:%S")
                    end_dt = datetime.strptime(end, "%H:%M:%S")
                except ValueError:
                    continue

                cur = start_dt
                while cur < end_dt:
                    mine_slots.add((place, cur.strftime("%H:%M")))
                    cur += timedelta(hours=1)

        return mine_slots

    def extract_mine_slots_by_date(self, orders):
        """按日期聚合 mine 格子，返回 {date: [{'place':'7','time':'20:00'}]}。"""
        grouped = {}
        for order in orders or []:
            if str(order.get("showStatus", "")) != "0":
                continue
            if str(order.get("prestatus", "")).strip() in ("取消", "已取消"):
                continue
            arr = order.get("jsonArray") or []
            if not isinstance(arr, list):
                continue
            for seg in arr:
                date_str = str(seg.get("reversionDate", "")).strip()
                if not date_str:
                    continue
                site_name = str(seg.get("siteName", ""))
                m = re.search(r"(\d+)", site_name)
                if not m:
                    continue
                place = m.group(1)
                start = str(seg.get("start", "")).strip()
                end = str(seg.get("end", "")).strip()
                try:
                    start_dt = datetime.strptime(start, "%H:%M:%S")
                    end_dt = datetime.strptime(end, "%H:%M:%S")
                except ValueError:
                    continue
                cur = start_dt
                while cur < end_dt:
                    grouped.setdefault(date_str, set()).add((place, cur.strftime("%H:%M")))
                    cur += timedelta(hours=1)

        result = {}
        for d, slots in grouped.items():
            result[d] = [
                {"place": p, "time": t}
                for p, t in sorted(slots, key=lambda x: (int(x[0]) if str(x[0]).isdigit() else 999, x[1]))
            ]
        return result

    def get_matrix(self, date_str, include_mine_overlay=True):
        cache_key = (str(date_str or ''), bool(include_mine_overlay))
        now_ts = time.time()
        with self._matrix_cache_lock:
            cache_hit = self._matrix_cache.get(cache_key)
        if cache_hit and (now_ts - float(cache_hit.get('ts', 0.0))) <= float(self._matrix_cache_window_s):
            try:
                return json.loads(json.dumps(cache_hit.get('data')))
            except Exception:
                return cache_hit.get('data')

        url = f"https://{self.host}/easyserpClient/place/getPlaceInfoByShortName"
        params = {
            "shopNum": CONFIG["auth"]["shop_num"],
            "dateymd": date_str,
            "shortName": "ymq",
            "token": self.token
        }
        try:
            # 抢票高峰期采用短超时，避免单次请求卡住吞掉黄金窗口；配合上层高频重试。
            started_at = time.time()
            matrix_timeout = max(0.5, float(CONFIG.get('matrix_timeout_seconds', 3.0) or 3.0))
            resp = self.session.get(url, headers=self.headers, params=params, timeout=matrix_timeout, verify=False)
            ended_at = time.time()
            self._update_server_time_offset(resp, started_at, ended_at)

            try:
                data = resp.json()
            except json.JSONDecodeError:
                # 服务器可能返回了 HTML 错误页或空内容
                print(f"❌ [原始响应] 非JSON格式: {resp.text[:100]}...")
                return {"error": "服务器返回无效数据(可能是崩了)"}
            
            # 安全检查：确保 data 是字典
            if not isinstance(data, dict):
                print(f"❌ [API响应异常] 响应不是字典: {type(data)} - {data}")
                # 特殊处理 -1 (通常代表 Session/Token 失效)
                if data == -1 or str(data) == "-1":
                    return {"error": "会话失效(返回-1)，请更新Token（必要）与Cookie（可选）"}
                return {"error": f"API返回格式错误: {data}"}

            if data.get("msg") != "success":
                return {"error": data.get("msg")}
            
            raw_data = data.get('data')
            if isinstance(raw_data, str):
                try: raw_list = json.loads(raw_data)
                except: return {"error": "JSON解析失败"}
            else:
                raw_list = raw_data
                
            if isinstance(raw_list, dict):
                if 'placeArray' in raw_list:
                    raw_list = raw_list['placeArray']
                else:
                    return {"error": "无法找到场地列表"}

            STATE_SAMPLER.ingest(raw_list)

            matrix = {}
            all_times = set()
            
            # 添加调试日志，打印前几个数据的状态值，以便分析“全红”原因
            debug_states = []

            locked_state_values = set()
            for raw_state in CONFIG.get('locked_state_values', [2, 3, 5, 6]):
                try:
                    locked_state_values.add(int(raw_state))
                except Exception:
                    continue
            if not locked_state_values:
                locked_state_values = {6}

            for place in raw_list:
                p_name = place['projectName']['shortname'] 
                p_num = p_name.replace('ymq', '').replace('mdb', '')
                
                status_map = {}
                for slot in place['projectInfo']:
                    t = slot['starttime']
                    s = slot['state']
                    all_times.add(t)
                    
                    if len(debug_states) < 5:
                        debug_states.append(f"{p_num}号{t}={s}")

                    # 1=可用, 其他=占用
                    # 根据调试日志修正：
                    # state=4: 似乎是“已占用”或“锁定” (全红时全是4)
                    # state=6: 似乎是“未开放”或“未来” (周五全是6)
                    # state=1: 偶尔出现，应该是“可用”
                    # state=0: 未知
                    
                    # 关键修改：
                    # 既然用户目的是“提前选中然后准时下单”，我们需要把“未开放”的状态也视为“可用(available)”
                    # 这样用户在前端就能选中并添加到愿望单了。
                    # 假设 6 是未开放但将来会开放。
                    # 假设 4 是已经被别人订了（不可选）。
                    # 假设 1 是当前就能买（可用）。
                    
                    # 策略：只要不是明确的“已预订(4?)”，都算 available？
                    # 或者更精确点：1(可用) 和 6(未开放) 都算 available。
                    # 暂时把 6 也加进去。

                    try:
                        state_int = int(s)
                    except Exception:
                        state_int = -999

                    if state_int == 1:
                        # 真正可以下单
                        status_map[t] = "available"
                    elif state_int in locked_state_values:
                        # 锁定/暂不可下单：继续走 locked 轮询，不提前放弃
                        status_map[t] = "locked"
                    else:
                        # 已被别人订了 / 不可用
                        status_map[t] = "booked"

                matrix[p_num] = status_map
            
            if is_verbose_logs_enabled():
                print(f"🔍 [状态调试] 前5个样本状态: {debug_states}")

            # 用我的订单覆盖 mine 状态（仅 showStatus=0 且非取消订单）
            mine_overlay_ok = False
            mine_overlay_error = ""
            mine_slots_count = 0

            if include_mine_overlay:
                orders_res = self.get_place_orders()
                if "error" not in orders_res:
                    mine_overlay_ok = True
                    mine_slots = self._extract_mine_slots(orders_res.get("data", []), date_str)
                    mine_slots_count = len(mine_slots)
                    for p, t in mine_slots:
                        if p in matrix and t in matrix[p]:
                            matrix[p][t] = "mine"
                    if mine_slots and is_verbose_logs_enabled():
                        print(f"🔵 [mine覆盖] 日期{date_str} 共标记 {len(mine_slots)} 个mine格子")
                else:
                    mine_overlay_error = str(orders_res.get('error') or '')
                    if is_verbose_logs_enabled():
                        print(f"⚠️ [mine覆盖] 订单查询失败，跳过mine状态: {mine_overlay_error}")
            else:
                mine_overlay_error = "首轮加速模式：跳过mine覆盖"

            sorted_places = sorted(matrix.keys(), key=lambda x: int(x) if x.isdigit() else 999)
            sorted_times = sorted(list(all_times))

            result = {
                "places": sorted_places,
                "times": sorted_times,
                "matrix": matrix,
                "meta": {
                    "mine_overlay_ok": mine_overlay_ok,
                    "mine_slots_count": mine_slots_count,
                    "mine_overlay_error": mine_overlay_error,
                }
            }
            with self._matrix_cache_lock:
                self._matrix_cache[cache_key] = {'ts': time.time(), 'data': result}
                if len(self._matrix_cache) > 8:
                    oldest = min(self._matrix_cache.keys(), key=lambda k: self._matrix_cache[k].get('ts', 0.0))
                    self._matrix_cache.pop(oldest, None)
            return result
            
        except Exception as e:
            return {"error": str(e)}

    def submit_order(self, date_str, selected_items):
        """
        提交预订订单。
        关键修正：不再单纯依赖 reservationPlace 返回的 "msg":"success"，
        而是提交完成后重新拉取矩阵，确认选中场次的状态是否从 available 变为 booked。
        """
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"

        results = []
        try:
            degrade_batch_size = int(CONFIG.get("submit_batch_size", 3))
        except Exception:
            degrade_batch_size = 3
        degrade_batch_size = max(1, min(9, degrade_batch_size))
        configured_initial_batch_size = int(CONFIG.get("initial_submit_batch_size", CONFIG.get("submit_batch_size", 3)) or 3)
        initial_batch_size = max(1, min(9, configured_initial_batch_size))
        batch_retry_times = int(CONFIG.get("batch_retry_times", 2))
        batch_retry_interval = float(CONFIG.get("batch_retry_interval", CONFIG.get("retry_interval", 0.5)))
        batch_min_interval = float(CONFIG.get("batch_min_interval", 0.8))
        refill_window_seconds = float(CONFIG.get("refill_window_seconds", 8.0))
        submit_timeout_seconds = max(0.5, float(CONFIG.get("submit_timeout_seconds", 4.0) or 4.0))
        submit_split_retry_times = max(0, min(3, int(CONFIG.get("submit_split_retry_times", 1) or 1)))

        print(
            f"🧭 [批次策略] 首批=按配置 initial_submit_batch_size→{initial_batch_size}；"
            f"降级=按配置 submit_batch_size→{degrade_batch_size}；本次选择={len(selected_items)}"
        )
        print(
            f"⏱️ [提交超时] submit_timeout={submit_timeout_seconds}s, split_retry_times={submit_split_retry_times}"
        )

        def normalize_fail_message(msg):
            text = str(msg or "").strip()
            if not text:
                return "下单失败(空响应)"
            lower = text.lower()
            if "<html" in lower and "404" in lower:
                return "下单接口暂时不可用(404)"
            if "404 not found" in lower:
                return "下单接口暂时不可用(404)"
            if "502" in lower or "503" in lower or "504" in lower:
                return "下单接口暂时不可用(网关异常)"
            if len(text) > 180:
                return text[:180] + "..."
            return text

        def is_retryable_fail(msg):
            text = str(msg or "").lower()
            keywords = [
                "操作过快", "稍后重试", "请求过于频繁", "too fast", "频繁",
                "404 not found", "nginx", "bad gateway", "service unavailable",
                "502", "503", "504", "timeout", "timed out", "connection reset",
                "max retries exceeded", "temporarily unavailable", "non-json", "非json",
                "暂时不可用", "网关异常", "下单接口暂时不可用", "空响应",
            ]
            return any(k in text for k in keywords)

        def should_degrade(msg):
            text = str(msg or "")
            rule_keywords = [
                "规则",
                "最多预约3个",
                "最多预约",
                "上限",
            ]
            return is_retryable_fail(text) or any(k in text for k in rule_keywords)

        def filter_still_available(items):
            try:
                verify = self.get_matrix(date_str, include_mine_overlay=False)
                if not isinstance(verify, dict) or verify.get("error"):
                    return list(items)
                matrix = verify.get("matrix") or {}
                remain = []
                for it in items:
                    p = str(it.get("place"))
                    t = it.get("time")
                    if matrix.get(p, {}).get(t) == "available":
                        remain.append(it)
                return remain
            except Exception:
                return list(items)

        submit_items = list(selected_items or [])
        preblocked_items = []
        same_time_limit = int(CONFIG.get("same_time_precheck_limit", 0) or 0)
        if same_time_limit > 0:
            try:
                verify = self.get_matrix(date_str)
                if isinstance(verify, dict) and not verify.get("error"):
                    matrix = verify.get("matrix") or {}
                    mine_by_time = {}
                    for row in matrix.values():
                        if not isinstance(row, dict):
                            continue
                        for t, state in row.items():
                            if state == "mine":
                                mine_by_time[t] = mine_by_time.get(t, 0) + 1

                    planned_by_time = {}
                    allowed_items = []
                    for it in submit_items:
                        t = it.get("time")
                        quota = max(0, same_time_limit - mine_by_time.get(t, 0))
                        used = planned_by_time.get(t, 0)
                        if used < quota:
                            allowed_items.append(it)
                            planned_by_time[t] = used + 1
                        else:
                            preblocked_items.append(it)

                    if preblocked_items:
                        print(
                            f"⚠️ [同时段上限预检] 触发上限{same_time_limit}，"
                            f"本轮跳过 {len(preblocked_items)} 项: {preblocked_items}"
                        )
                    submit_items = allowed_items
            except Exception as e:
                print(f"⚠️ [同时段上限预检] 预检异常，按原始选择提交: {e}")
        else:
            if is_verbose_logs_enabled():
                print("⚡ [同时段上限预检] 已关闭（same_time_precheck_limit<=0）")

        # 首轮提交：按“本次选择数量”自适应分批
        for i in range(0, len(submit_items), initial_batch_size):
            batch = submit_items[i:i + initial_batch_size]
            print(f"📦 正在提交分批订单 ({i // initial_batch_size + 1}): {batch}")

            field_info_list = []
            total_money = 0

            for item in batch:
                p_num = item["place"]
                start = item["time"]
                # 计算结束时间 & 按开始时间决定价格
                try:
                    st_obj = datetime.strptime(start, "%H:%M")
                    et_obj = st_obj + timedelta(hours=1)
                    end = et_obj.strftime("%H:%M")
                    # 简单价格规则：14:00 之前 80 元，之后 100 元
                    # 对应抓包中的 oldMoney 分布（10–13 点为 80，14 点以后为 100）
                    if st_obj.hour < 14:
                        price = 80
                    else:
                        price = 100
                except Exception:
                    # 异常时兜底：把结束时间和价格都设为常规晚间价格
                    end = "22:00"
                    price = 100

                # 根据场地号区分普通场 (1-14) 和木地板场 (15-17)
                try:
                    p_int = int(p_num)
                except (TypeError, ValueError):
                    p_int = None

                if p_int is not None and p_int >= 15:
                    # 木地板场：shortname 形如 mdb15，name 为 "木地板15"
                    place_short = f"mdb{p_num}"
                    place_name = f"木地板{p_num}"
                else:
                    # 普通羽毛球场：shortname 形如 ymq10，name 为 "羽毛球10"
                    place_short = f"ymq{p_num}"
                    place_name = f"羽毛球{p_num}"

                info = {
                    "day": date_str,
                    "oldMoney": price,
                    "startTime": start,
                    "endTime": end,
                    "placeShortName": place_short,
                    "name": place_name,
                    "stageTypeShortName": "ymq",
                    "newMoney": price,
                }
                field_info_list.append(info)
                total_money += price

            info_str = urllib.parse.quote(
                json.dumps(field_info_list, separators=(",", ":"), ensure_ascii=False)
            )
            type_encoded = urllib.parse.quote("羽毛球")

            body = (
                f"token={self.token}&"
                f"shopNum={CONFIG['auth']['shop_num']}&"
                f"fieldinfo={info_str}&"
                f"cardStId={CONFIG['auth']['card_st_id']}&"
                f"oldTotal={total_money}.00&"
                f"cardPayType=0&"
                f"type={type_encoded}&"
                f"offerId=&"
                f"offerType=&"
                f"total={total_money}.00&"
                f"premerother=&"
                f"cardIndex={CONFIG['auth']['card_index']}"
            )

            final_result = None
            for attempt in range(batch_retry_times + 1):
                try:
                    resp = self.session.post(
                        url, headers=self.headers, data=body, timeout=submit_timeout_seconds, verify=False
                    )

                    try:
                        resp_data = resp.json()
                    except ValueError:
                        resp_data = None

                    if is_verbose_logs_enabled():
                        print(
                            f"📨 [submit_order调试] 批次 {i // initial_batch_size + 1} 响应: {resp.text}"
                        )

                    if resp_data and resp_data.get("msg") == "success":
                        final_result = {"status": "success", "batch": batch}
                        break

                    fail_msg = None
                    if isinstance(resp_data, dict):
                        fail_msg = resp_data.get("data") or resp_data.get("msg")
                    if not fail_msg:
                        fail_msg = resp.text
                    fail_msg = normalize_fail_message(fail_msg)

                    if attempt < batch_retry_times and is_retryable_fail(fail_msg):
                        sleep_s = batch_retry_interval * (2 ** attempt) + random.uniform(0, 0.25)
                        print(
                            f"⏳ 批次 {i // initial_batch_size + 1} 命中可重试错误，"
                            f"{round(sleep_s, 2)}s 后重试 ({attempt + 1}/{batch_retry_times})"
                        )
                        time.sleep(sleep_s)
                        continue

                    # 命中“可重试/规则异常”时，按配置分批降级重提一次
                    if len(batch) > degrade_batch_size and should_degrade(fail_msg):
                        print(f"↘️ 批次 {i // initial_batch_size + 1} 降级重提: size {len(batch)} -> {degrade_batch_size}")
                        degrade_fail = list(batch)
                        current = list(batch)
                        for split_round in range(submit_split_retry_times + 1):
                            round_fail = []
                            for j in range(0, len(current), degrade_batch_size):
                                sub = current[j:j + degrade_batch_size]
                                try:
                                    sub_field_info = []
                                    sub_total = 0
                                    for item in sub:
                                        p_num = item["place"]
                                        start = item["time"]
                                        try:
                                            st_obj = datetime.strptime(start, "%H:%M")
                                            et_obj = st_obj + timedelta(hours=1)
                                            end = et_obj.strftime("%H:%M")
                                            price = 80 if st_obj.hour < 14 else 100
                                        except Exception:
                                            end = "22:00"
                                            price = 100
                                        try:
                                            p_int = int(p_num)
                                        except (TypeError, ValueError):
                                            p_int = None
                                        if p_int is not None and p_int >= 15:
                                            place_short = f"mdb{p_num}"
                                            place_name = f"木地板{p_num}"
                                        else:
                                            place_short = f"ymq{p_num}"
                                            place_name = f"羽毛球{p_num}"
                                        sub_field_info.append({
                                            "day": date_str,
                                            "oldMoney": price,
                                            "startTime": start,
                                            "endTime": end,
                                            "placeShortName": place_short,
                                            "name": place_name,
                                            "stageTypeShortName": "ymq",
                                            "newMoney": price,
                                        })
                                        sub_total += price

                                    info_str = urllib.parse.quote(json.dumps(sub_field_info, separators=(",", ":"), ensure_ascii=False))
                                    type_encoded = urllib.parse.quote("羽毛球")
                                    sub_body = (
                                        f"token={self.token}&shopNum={CONFIG['auth']['shop_num']}&fieldinfo={info_str}&"
                                        f"cardStId={CONFIG['auth']['card_st_id']}&oldTotal={sub_total}.00&cardPayType=0&"
                                        f"type={type_encoded}&offerId=&offerType=&total={sub_total}.00&premerother=&"
                                        f"cardIndex={CONFIG['auth']['card_index']}"
                                    )
                                    sub_resp = self.session.post(url, headers=self.headers, data=sub_body, timeout=submit_timeout_seconds, verify=False)
                                    sub_data = sub_resp.json() if sub_resp.text else None
                                    if not (isinstance(sub_data, dict) and sub_data.get("msg") == "success"):
                                        round_fail.extend(sub)
                                except Exception:
                                    round_fail.extend(sub)
                                time.sleep(max(batch_min_interval, CONFIG.get("retry_interval", 0.5)))
                            degrade_fail = round_fail
                            if not degrade_fail:
                                break
                            if split_round < submit_split_retry_times:
                                current = list(degrade_fail)
                                print(f"🔁 [降级分段重试] round={split_round + 1}/{submit_split_retry_times}, remain={len(current)}")
                                time.sleep(batch_retry_interval)

                        if not degrade_fail:
                            final_result = {"status": "success", "batch": batch}
                        else:
                            final_result = {"status": "fail", "msg": fail_msg, "batch": degrade_fail}
                        break

                    final_result = {"status": "fail", "msg": fail_msg, "batch": batch}
                    break
                except Exception as e:
                    if attempt < batch_retry_times:
                        print(
                            f"⏳ 批次 {i // initial_batch_size + 1} 异常，{batch_retry_interval}s 后重试 "
                            f"({attempt + 1}/{batch_retry_times}): {e}"
                        )
                        time.sleep(batch_retry_interval)
                        continue
                    final_result = {"status": "error", "msg": str(e), "batch": batch}
                    break

            results.append(final_result or {"status": "error", "msg": "未知错误", "batch": batch})

            # 批次间最小停顿，防止触发“操作过快”
            time.sleep(max(batch_min_interval, CONFIG.get("retry_interval", 0.5)))

        # 对失败项做补提（窗口内仅补提仍 available 的项）
        try:
            refill_deadline = time.time() + max(0.0, refill_window_seconds)
            while time.time() < refill_deadline:
                failed_items = []
                for r in results:
                    if r.get("status") in ("fail", "error"):
                        failed_items.extend(r.get("batch") or [])
                if not failed_items:
                    break

                still_available = filter_still_available(failed_items)
                if not still_available:
                    break

                print(f"🔁 [补提] 窗口内补提仍可用项: {still_available}")
                results = [r for r in results if r.get("status") == "success"]
                for i in range(0, len(still_available), degrade_batch_size):
                    batch = still_available[i:i + degrade_batch_size]
                    field_info_list = []
                    total_money = 0
                    for item in batch:
                        p_num = item["place"]
                        start = item["time"]
                        try:
                            st_obj = datetime.strptime(start, "%H:%M")
                            et_obj = st_obj + timedelta(hours=1)
                            end = et_obj.strftime("%H:%M")
                            price = 80 if st_obj.hour < 14 else 100
                        except Exception:
                            end = "22:00"
                            price = 100
                        try:
                            p_int = int(p_num)
                        except (TypeError, ValueError):
                            p_int = None
                        if p_int is not None and p_int >= 15:
                            place_short = f"mdb{p_num}"
                            place_name = f"木地板{p_num}"
                        else:
                            place_short = f"ymq{p_num}"
                            place_name = f"羽毛球{p_num}"
                        field_info_list.append({
                            "day": date_str,
                            "oldMoney": price,
                            "startTime": start,
                            "endTime": end,
                            "placeShortName": place_short,
                            "name": place_name,
                            "stageTypeShortName": "ymq",
                            "newMoney": price,
                        })
                        total_money += price
                    info_str = urllib.parse.quote(json.dumps(field_info_list, separators=(",", ":"), ensure_ascii=False))
                    type_encoded = urllib.parse.quote("羽毛球")
                    body = (
                        f"token={self.token}&shopNum={CONFIG['auth']['shop_num']}&fieldinfo={info_str}&"
                        f"cardStId={CONFIG['auth']['card_st_id']}&oldTotal={total_money}.00&cardPayType=0&"
                        f"type={type_encoded}&offerId=&offerType=&total={total_money}.00&premerother=&"
                        f"cardIndex={CONFIG['auth']['card_index']}"
                    )
                    try:
                        resp = self.session.post(url, headers=self.headers, data=body, timeout=10, verify=False)
                        resp_data = resp.json() if resp.text else None
                        if isinstance(resp_data, dict) and resp_data.get("msg") == "success":
                            results.append({"status": "success", "batch": batch})
                        else:
                            msg = resp_data.get("data") if isinstance(resp_data, dict) else resp.text
                            results.append({"status": "fail", "msg": msg, "batch": batch})
                    except Exception as e:
                        results.append({"status": "error", "msg": str(e), "batch": batch})
                    time.sleep(max(batch_min_interval, CONFIG.get("retry_interval", 0.5)))

                # 补提只做一轮，避免无限轰炸
                break
        except Exception as e:
            print(f"⚠️ [补提] 处理异常: {e}")

        if preblocked_items:
            results.append({
                "status": "fail",
                "msg": "同一时间的场地最多预约3个(含已预约mine)",
                "batch": preblocked_items,
            })

        # ---------- 下单后验证 ----------
        api_success_count = sum(1 for r in results if r.get("status") == "success")
        verify_success_count = None
        verify_success_items = []
        verify_failed_items = []
        orders_query_ok = False
        orders_query_error = ""
        try:
            verify = self.get_matrix(date_str, include_mine_overlay=False)
            orders_res = {"error": "按配置跳过"}
            order_timeout_s = max(0.5, float(CONFIG.get('order_query_timeout_seconds', 2.5) or 2.5))
            order_max_pages = max(1, min(10, int(CONFIG.get('order_query_max_pages', 2) or 2)))

            if isinstance(verify, dict) and not verify.get("error"):
                v_matrix = verify["matrix"]
                verify_states = []

                matrix_success_items = []
                matrix_failed_items = []
                for item in submit_items:
                    p = str(item["place"])
                    t = item["time"]
                    status = v_matrix.get(p, {}).get(t, "N/A")
                    if status in ("booked", "mine"):
                        matrix_success_items.append({"place": p, "time": t})
                    else:
                        matrix_failed_items.append({"place": p, "time": t})

                verify_orders_only_on_partial = bool(CONFIG.get('post_submit_verify_orders_on_matrix_partial_only', True))
                needs_orders_query = bool(matrix_failed_items) if verify_orders_only_on_partial else True

                if needs_orders_query:
                    orders_res = {"error": "未执行"}

                    def _fetch_orders():
                        nonlocal orders_res
                        orders_res = self.get_place_orders(max_pages=order_max_pages, timeout_s=order_timeout_s)

                    t_orders = threading.Thread(target=_fetch_orders, daemon=True)
                    t_orders.start()
                    join_timeout_s = max(0.1, float(CONFIG.get('post_submit_orders_join_timeout_seconds', 1.2) or 1.2))
                    t_orders.join(timeout=join_timeout_s)
                    if isinstance(orders_res, dict) and orders_res.get("error") == "未执行":
                        orders_res = {"error": f"订单查询超时(>{join_timeout_s}s)"}
                        if bool(CONFIG.get('post_submit_orders_sync_fallback', False)):
                            orders_res = self.get_place_orders(max_pages=order_max_pages, timeout_s=order_timeout_s)

                mine_slots = set()
                if "error" not in orders_res:
                    mine_slots = self._extract_mine_slots(orders_res.get("data", []), date_str)
                    orders_query_ok = True
                else:
                    orders_query_error = str(orders_res.get("error") or "")
                    print(
                        f"🧾 [提交后验证调试] 订单拉取失败，mine校验降级为矩阵状态: {orders_query_error}"
                    )

                for item in submit_items:
                    p = str(item["place"])
                    t = item["time"]
                    status = v_matrix.get(p, {}).get(t, "N/A")
                    mine_hit = (p, t) in mine_slots
                    verify_states.append(f"{p}号{t}={status},mine={'Y' if mine_hit else 'N'}")

                    # 若订单查询可用：优先用 mine 兜底；否则只用矩阵状态，保证验证链路尽快收敛。
                    success = mine_hit or status in ("booked", "mine") if orders_query_ok else status in ("booked", "mine")

                    if success:
                        verify_success_items.append({"place": p, "time": t})
                    else:
                        verify_failed_items.append({"place": p, "time": t})

                if preblocked_items:
                    verify_failed_items.extend(preblocked_items)
                    verify_states.extend([
                        f"{str(it.get('place'))}号{it.get('time')}=preblocked"
                        for it in preblocked_items
                    ])

                if is_verbose_logs_enabled():
                    print(f"🧾 [提交后验证调试] 选中场次最新状态: {verify_states}")
                verify_success_count = len(verify_success_items)
            else:
                print(
                    f"🧾 [提交后验证调试] 获取矩阵失败: "
                    f"{verify.get('error') if isinstance(verify, dict) else verify}"
                )
        except Exception as e:
            print(f"🧾 [提交后验证调试] 异常: {e}")

        # ---------- 汇总结果 ----------
        # 1) 接口返回层面的成功批次数

        # 2) 真实已被占用的场次数量（如果验证成功）
        verify_ok = verify_success_count is not None
        if verify_ok:
            success_count = verify_success_count
        else:
            # 验证失败时不再把“接口 success”直接当作最终成功，避免误报
            success_count = 0

        # 3) 本次计划总共尝试下单的场次数
        total_items = len(selected_items) if selected_items else 0

        # 兼容老逻辑：如果 selected_items 为空（理论上不应该），
        # 退回到按批次数统计，防止 denominator 为 0。
        denominator = total_items or len(results)

        if denominator == 0:
            msg = "没有生成任何下单项目，请检查配置或场地状态。"
            return {"status": "fail", "msg": msg}

        cross_instance_suspected = verify_ok and api_success_count == 0 and success_count > 0

        if verify_ok and api_success_count > 0 and success_count == denominator:
            return {
                "status": "success",
                "msg": "全部下单成功",
                "success_items": verify_success_items,
                "failed_items": verify_failed_items,
            }
        elif verify_ok and api_success_count > 0 and success_count > 0:
            return {
                "status": "partial",
                "msg": f"部分成功 ({success_count}/{denominator})",
                "success_items": verify_success_items,
                "failed_items": verify_failed_items,
            }
        else:
            # 未收到任何提交成功响应，但校验命中 mine，疑似并发实例下单导致的“归因串扰”
            if cross_instance_suspected:
                msg = "检测到我的订单已占位，但本进程提交未收到 success，可能由并发实例下单导致；本任务按失败处理。"
            # 验证失败时，宁可报失败也不误报成功
            elif not verify_ok and api_success_count > 0:
                msg = "下单接口返回 success，但提交后状态验证失败（网络/服务波动），请以官方系统为准。"
            elif api_success_count > 0 and verify_success_count == 0:
                allow_verify_pending = bool(CONFIG.get("post_submit_treat_verify_timeout_as_retry", True))
                if allow_verify_pending and (not orders_query_ok):
                    return {
                        "status": "verify_pending",
                        "msg": f"提交已返回success，但验证尚未收敛({orders_query_error or '订单校验未完成'})，将快速复核。",
                        "success_items": verify_success_items,
                        "failed_items": verify_failed_items,
                    }
                msg = "接口返回 success，但场地状态未变化，请在微信小程序确认或检查参数。"
            else:
                first_fail = results[0] if results else {"msg": "无数据"}
                msg = first_fail.get("msg")
            return {
                "status": "fail",
                "msg": msg,
                "success_items": verify_success_items,
                "failed_items": verify_failed_items,
            }

    def x_submit_order_old(self, date_str, selected_items):
        pass

client = ApiClient()

# ================= 任务调度系统 =================

class TaskManager:
    def __init__(self):
        self.tasks = []
        self.refill_tasks = []
        self._refill_lock = threading.Lock()
        self._refill_last_run = {}
        self._refill_notify_last_bucket = {}
        self._task_run_lock = threading.Lock()
        self._running_task_ids = set()
        self.load_tasks()
        self.load_refill_tasks()

    def _try_mark_task_running(self, task_id):
        tid = str(task_id)
        with self._task_run_lock:
            if tid in self._running_task_ids:
                return False
            self._running_task_ids.add(tid)
            return True

    def _unmark_task_running(self, task_id):
        tid = str(task_id)
        with self._task_run_lock:
            self._running_task_ids.discard(tid)

    def is_task_running(self, task_id):
        tid = str(task_id)
        with self._task_run_lock:
            return tid in self._running_task_ids

    def execute_task_with_lock(self, task):
        task_id = task.get('id')
        if task_id is not None and not self._try_mark_task_running(task_id):
            log(f"⏭️ [任务锁] 任务{task_id}仍在执行，跳过本次触发")
            return False
        try:
            self.execute_task(task)
            return True
        finally:
            if task_id is not None:
                self._unmark_task_running(task_id)
        

    def load_refill_tasks(self):
        if os.path.exists(REFILL_TASKS_FILE):
            try:
                with open(REFILL_TASKS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.refill_tasks = data if isinstance(data, list) else []
            except Exception:
                self.refill_tasks = []

    def save_refill_tasks(self):
        with self._refill_lock:
            with open(REFILL_TASKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.refill_tasks, f, ensure_ascii=False, indent=2)

    def add_refill_task(self, task):
        now_ms = int(time.time() * 1000)
        task = dict(task or {})
        task['id'] = now_ms
        task['enabled'] = bool(task.get('enabled', True))
        task['interval_seconds'] = max(1.0, float(task.get('interval_seconds', 10.0) or 10.0))
        task['target_count'] = max(1, min(MAX_TARGET_COUNT, int(task.get('target_count', 1) or 1)))
        task['last_run_at'] = task.get('last_run_at')
        task['last_result'] = task.get('last_result')
        task['deadline'] = str(task.get('deadline') or '').strip()
        task['deadline_mode'] = str(task.get('deadline_mode') or 'absolute').strip() or 'absolute'
        try:
            task['deadline_before_hours'] = float(task.get('deadline_before_hours') or 2.0)
        except Exception:
            task['deadline_before_hours'] = 2.0
        task['exec_history'] = list(task.get('exec_history') or [])[-10:]
        self.refill_tasks.append(task)
        self.save_refill_tasks()
        return task

    def delete_refill_task(self, task_id):
        tid = int(task_id)
        self.refill_tasks = [t for t in self.refill_tasks if int(t.get('id', -1)) != tid]
        self._refill_last_run.pop(tid, None)
        self.save_refill_tasks()


    def update_refill_task(self, task_id, patch):
        tid = int(task_id)
        for t in self.refill_tasks:
            if int(t.get('id', -1)) != tid:
                continue
            payload = dict(patch or {})
            if 'date' in payload:
                t['date'] = str(payload.get('date') or '').strip()
            if 'target_times' in payload and isinstance(payload.get('target_times'), list):
                t['target_times'] = [str(x).strip() for x in payload.get('target_times') if str(x).strip()]
            if 'candidate_places' in payload and isinstance(payload.get('candidate_places'), list):
                t['candidate_places'] = [str(x).strip() for x in payload.get('candidate_places') if str(x).strip()]
            if 'interval_seconds' in payload:
                try:
                    t['interval_seconds'] = max(1.0, float(payload.get('interval_seconds') or 10.0))
                except Exception:
                    pass
            if 'target_count' in payload:
                try:
                    t['target_count'] = max(1, min(MAX_TARGET_COUNT, int(payload.get('target_count') or 1)))
                except Exception:
                    pass
            if 'enabled' in payload:
                t['enabled'] = bool(payload.get('enabled'))
            if 'deadline' in payload:
                t['deadline'] = str(payload.get('deadline') or '').strip()
            if 'deadline_mode' in payload:
                mode = str(payload.get('deadline_mode') or '').strip()
                t['deadline_mode'] = mode if mode in ('absolute', 'before_start') else 'absolute'
            if 'deadline_before_hours' in payload:
                try:
                    t['deadline_before_hours'] = max(0.0, float(payload.get('deadline_before_hours') or 0.0))
                except Exception:
                    pass
            self.save_refill_tasks()
            return t
        return None

    def append_refill_history(self, task, result):
        history = list(task.get('exec_history') or [])
        history.append({
            'ts': int(time.time() * 1000),
            'status': result.get('status'),
            'msg': str(result.get('msg') or '')[:120],
        })
        task['exec_history'] = history[-10:]


    def _compute_refill_deadline(self, refill_task):
        mode = str(refill_task.get('deadline_mode') or 'absolute').strip()
        if mode == 'before_start':
            date_str = str(refill_task.get('date') or '').strip()
            times = sorted([str(t).strip() for t in (refill_task.get('target_times') or []) if str(t).strip()])
            if not date_str or not times:
                return None, ''
            time0 = times[0]
            try:
                start_dt = datetime.strptime(f"{date_str} {time0}:00" if len(time0) == 5 else f"{date_str} {time0}", "%Y-%m-%d %H:%M:%S")
                before_h = max(0.0, float(refill_task.get('deadline_before_hours') or 0.0))
                deadline_dt = start_dt - timedelta(hours=before_h)
                return deadline_dt, deadline_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return None, ''

        deadline_raw = str(refill_task.get('deadline') or '').strip()
        if not deadline_raw:
            return None, ''
        try:
            return datetime.strptime(deadline_raw, "%Y-%m-%d %H:%M:%S"), deadline_raw
        except Exception:
            return None, deadline_raw

    def _should_notify_refill_success(self, task_id):
        bucket = datetime.now().strftime("%Y%m%d%H%M")
        key = f"{task_id}:{bucket}"
        if self._refill_notify_last_bucket.get(str(task_id)) == bucket:
            return False
        self._refill_notify_last_bucket[str(task_id)] = bucket
        return True

    def _run_refill_task_once(self, refill_task, source='auto'):
        task_id = str(refill_task.get('id') or 'unknown')
        date_str = str(refill_task.get('date') or '').strip()
        target_times = [str(t).strip() for t in (refill_task.get('target_times') or []) if str(t).strip()]
        candidate_places = [str(p).strip() for p in (refill_task.get('candidate_places') or []) if str(p).strip()]
        target_count = max(1, min(MAX_TARGET_COUNT, int(refill_task.get('target_count', 1) or 1)))
        tag = f"[refill#{task_id}|{source}]"

        deadline_dt, deadline_text = self._compute_refill_deadline(refill_task)
        if deadline_dt and datetime.now() >= deadline_dt:
            msg = f"已超过截止时间({deadline_text})，停止执行"
            log(f"⏹️ {tag} {msg}")
            return {'status': 'stopped', 'msg': msg}

        log(f"🧩 {tag} 开始执行: date={date_str}, target_count={target_count}, times={target_times}, places={len(candidate_places)}")
        if not date_str or not target_times or not candidate_places:
            msg = 'refill任务缺少 date/target_times/candidate_places'
            log(f"❌ {tag} {msg}")
            return {'status': 'fail', 'msg': msg}

        need_res = {'need_by_time': {}}
        orders_res = client.get_place_orders()
        mine_slots = set()
        if 'error' not in orders_res:
            mine_slots = client._extract_mine_slots(orders_res.get('data', []), date_str)

        for t in target_times:
            mine_cnt = sum(1 for p in candidate_places if (p, t) in mine_slots)
            need_res['need_by_time'][t] = max(0, target_count - mine_cnt)

        if sum(need_res['need_by_time'].values()) <= 0:
            msg = 'refill目标已满足'
            log(f"✅ {tag} {msg}")
            return {'status': 'success', 'msg': msg, 'success_items': []}

        matrix_res = client.get_matrix(date_str)
        if 'error' in matrix_res:
            msg = f"获取矩阵失败: {matrix_res.get('error')}"
            log(f"❌ {tag} {msg}")
            return {'status': 'error', 'msg': msg}
        matrix = matrix_res.get('matrix') or {}

        picks = []
        for t in target_times:
            remain = int(need_res['need_by_time'].get(t, 0))
            if remain <= 0:
                continue
            for p in candidate_places:
                if remain <= 0:
                    break
                if matrix.get(p, {}).get(t) == 'available':
                    picks.append({'place': p, 'time': t})
                    remain -= 1

        if not picks:
            msg = f"当前无可补订组合，缺口: {need_res['need_by_time']}"
            log(f"🙈 {tag} {msg}")
            return {'status': 'fail', 'msg': msg}

        log(f"📦 {tag} 本轮提交: {picks}")
        submit_res = client.submit_order(date_str, picks)
        log(f"🧾 {tag} 本轮结果: {submit_res.get('status')} - {submit_res.get('msg')}")
        if submit_res.get('status') in ('success', 'partial') and (submit_res.get('success_items') or []):
            ok_items = submit_res.get('success_items') or []
            item_text = '、'.join([f"{it.get('place')}号{it.get('time')}" for it in ok_items[:6]])
            msg = f"Refill#{task_id}补订成功({len(ok_items)}项): {date_str} {item_text}"
            if self._should_notify_refill_success(task_id):
                self.send_notification(msg)
                self.send_wechat_notification(msg)
            else:
                log(f"🔕 {tag} 本分钟内已通知，跳过重复成功通知")
        return submit_res

    def run_refill_scheduler_tick(self):
        now = time.time()
        for t in list(self.refill_tasks):
            if not bool(t.get('enabled', True)):
                continue
            tid = int(t.get('id', 0))
            deadline_dt, deadline_text = self._compute_refill_deadline(t)
            if deadline_dt and datetime.now() >= deadline_dt:
                t['enabled'] = False
                t['last_result'] = {'status': 'stopped', 'msg': f'达到截止时间({deadline_text})，自动停用'}
                self.append_refill_history(t, t['last_result'])
                self.save_refill_tasks()
                try:
                    task_id = str(t.get('id') or '-')
                    date_str = str(t.get('date') or '')
                    content = f"Refill#{task_id} 已到截止时间({deadline_text})，任务自动停用。日期: {date_str}"
                    self.send_notification(content)
                    self.send_wechat_notification(content)
                except Exception as e:
                    log(f"⚠️ [refill#{t.get('id')}] 截止停用通知发送失败: {e}")
                continue
            interval = max(1.0, float(t.get('interval_seconds', 10.0) or 10.0))
            last = float(self._refill_last_run.get(tid, 0.0))
            if now - last < interval:
                continue
            self._refill_last_run[tid] = now
            t['last_result'] = {'status': 'running', 'msg': '自动轮询执行中'}
            self.save_refill_tasks()
            try:
                res = self._run_refill_task_once(t, source='auto')
            except Exception as e:
                res = {'status': 'error', 'msg': str(e)}
            t['last_run_at'] = int(time.time() * 1000)
            t['last_result'] = res
            self.append_refill_history(t, res)
            self.save_refill_tasks()

    def load_tasks(self):
        if os.path.exists(TASKS_FILE):
            try:
                with open(TASKS_FILE, 'r', encoding='utf-8') as f:
                    self.tasks = json.load(f)
            except:
                self.tasks = []
                
    def save_tasks(self):
        with open(TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)
            
    def add_task(self, task):
        # task: {id, type='daily'|'weekly', run_time='08:00', target_day_offset=2, items=[...]}
        cfg = task.get('config') if isinstance(task, dict) else None
        if isinstance(cfg, dict) and 'target_count' in cfg:
            try:
                cfg['target_count'] = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
            except Exception:
                cfg['target_count'] = 2

        task['id'] = int(time.time() * 1000)
        self.tasks.append(task)
        self.save_tasks()
        self.refresh_schedule()

    def update_task(self, task_id, task):
        task_id = int(task_id)
        for i, old in enumerate(self.tasks):
            if int(old.get('id', -1)) == task_id:
                cfg = task.get('config') if isinstance(task, dict) else None
                if isinstance(cfg, dict) and 'target_count' in cfg:
                    try:
                        cfg['target_count'] = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
                    except Exception:
                        cfg['target_count'] = 2

                task['id'] = task_id
                task['last_run_at'] = old.get('last_run_at')
                self.tasks[i] = task
                self.save_tasks()
                self.refresh_schedule()
                return True
        return False

    def mark_task_run(self, task_id):
        task_id = int(task_id)
        for task in self.tasks:
            if int(task.get('id', -1)) == task_id:
                task['last_run_at'] = int(time.time() * 1000)
                self.save_tasks()
                return

    def delete_task(self, task_id, refresh=True):
        self.tasks = [t for t in self.tasks if t['id'] != int(task_id)]
        self.save_tasks()
        if refresh:
            self.refresh_schedule()

    def send_notification(self, content, phones=None):
        """
        发送短信通知：
        - phones 不为 None 时，优先使用传入的号码（任务级别）
        - 否则退回到全局 CONFIG['notification_phones']
        """
        if phones is None:
            phones = CONFIG.get('notification_phones', [])

        # 归一化手机号：允许字符串/列表混用
        if isinstance(phones, str):
            phones = [p.strip() for p in phones.split(',') if p.strip()]
        elif isinstance(phones, list):
            phones = [str(p).strip() for p in phones if str(p).strip()]

        if not phones:
            log(f"⚠️ 未配置短信手机号，通知内容未发送: {content}")
            return  # 没有号码就直接返回

        log(f"📧 正在发送短信通知给: {phones}")
        try:
            u = CONFIG['sms']['user']
            p = CONFIG['sms']['api_key']

            error_map = {
                '0': '发送成功',
                '30': '密码错误',
                '40': '账号不存在',
                '41': '余额不足',
                '42': '帐号过期',
                '43': 'IP地址限制',
                '50': '内容含有敏感词',
                '51': '手机号码不正确'
            }

            m = ",".join(phones)
            c = f"【数数云端】{content}"

            params = {
                "u": u,
                "p": p,
                "m": m,
                "c": c
            }

            resp = requests.get("https://api.smsbao.com/sms", params=params, timeout=10)

            code = resp.text
            msg = error_map.get(code, f"未知错误({code})")
            log(f"📧 短信接口返回: [{code}] {msg}")

            if code != '0':
                log(f"⚠️ 短信发送异常: {msg}")
                return False, msg
            return True, "发送成功"

        except Exception as e:
            log(f"❌ 短信发送异常: {e}")
            return False, str(e)

    def send_wechat_notification(self, content, tokens=None):
        """
        发送微信通知（PushPlus）：
        - tokens 不为 None 时，优先使用传入的 token（任务级别）
        - 否则退回到全局 CONFIG['pushplus_tokens']
        """
        if tokens is None:
            tokens = CONFIG.get('pushplus_tokens', [])

        if isinstance(tokens, str):
            tokens = [t.strip() for t in tokens.split(',') if t.strip()]
        elif isinstance(tokens, list):
            tokens = [str(t).strip() for t in tokens if str(t).strip()]

        if not tokens:
            log(f"⚠️ 未配置 PushPlus token，微信通知未发送: {content}")
            return False, "未配置 PushPlus token"

        try:
            payload = {
                "title": "场地预订通知",
                "content": content,
                "template": "txt",
            }
            for token in tokens:
                payload["token"] = token
                resp = requests.post(
                    "http://www.pushplus.plus/send",
                    json=payload,
                    timeout=10,
                )
                try:
                    data = resp.json()
                except ValueError:
                    data = {"code": -1, "msg": resp.text}
                if data.get("code") != 200:
                    log(f"⚠️ PushPlus 发送失败: {data}")
                else:
                    log("📩 PushPlus 发送成功")
            return True, "发送成功"
        except Exception as e:
            log(f"❌ PushPlus 发送异常: {e}")
            return False, str(e)

    def execute_task(self, task):
        log(f"⏰ [自动任务] 开始执行任务: {task.get('id')}")
        if task.get('id') is not None:
            self.mark_task_run(task['id'])

        run_started_ts = time.time()
        run_metrics = {
            "task_id": task.get('id'),
            "task_type": task.get('type', 'daily'),
            "started_at": int(run_started_ts * 1000),
            "active_started_at": int(run_started_ts * 1000),
            "prestart_wait_ms": 0,
            "attempt_count": 0,
            "first_matrix_ok_ms": None,
            "first_submit_ms": None,
            "submit_latencies_ms": [],
            "first_success_ms": None,
            "result_status": None,
            "result_msg": None,
            "target_date": None,
            "saw_locked": False,
            "unlocked_after_locked": False,
            "config_snapshot": {
                "retry_interval": float(CONFIG.get("retry_interval", 1.0) or 1.0),
                "aggressive_retry_interval": float(CONFIG.get("aggressive_retry_interval", 0.3) or 0.3),
                "batch_retry_times": int(CONFIG.get("batch_retry_times", 2) or 2),
                "batch_retry_interval": float(CONFIG.get("batch_retry_interval", 0.5) or 0.5),
                "submit_batch_size": int(CONFIG.get("submit_batch_size", 3) or 3),
                "initial_submit_batch_size": int(CONFIG.get("initial_submit_batch_size", CONFIG.get("submit_batch_size", 3)) or 3),
                "submit_timeout_seconds": float(CONFIG.get("submit_timeout_seconds", 4.0) or 4.0),
                "submit_split_retry_times": int(CONFIG.get("submit_split_retry_times", 1) or 1),
                "batch_min_interval": float(CONFIG.get("batch_min_interval", 0.8) or 0.8),
                "order_query_timeout_seconds": float(CONFIG.get("order_query_timeout_seconds", 2.5) or 2.5),
                "order_query_max_pages": int(CONFIG.get("order_query_max_pages", 2) or 2),
                "locked_retry_interval": float(CONFIG.get("locked_retry_interval", 1.0) or 1.0),
                "locked_max_seconds": float(CONFIG.get("locked_max_seconds", 60.0) or 60.0),
                "open_retry_seconds": float(CONFIG.get("open_retry_seconds", 30.0) or 30.0),
                "matrix_timeout_seconds": float(CONFIG.get("matrix_timeout_seconds", 3.0) or 3.0),
                "stop_on_none_stage_without_refill": bool(CONFIG.get("stop_on_none_stage_without_refill", False)),
                "preselect_enabled": bool(CONFIG.get("preselect_enabled", True)),
                "preselect_ttl_seconds": float(CONFIG.get("preselect_ttl_seconds", 2.0) or 2.0),
            },
            "goal_achieved": False,
            "success_item_count": 0,
            "failed_item_count": 0,
            "preselect_hit_count": 0,
            "preselect_miss_count": 0,
        }

        active_started_ts = run_started_ts

        # 每个任务自己配置的通知手机号（列表），用于“下单成功”类通知
        task_phones = task.get('notification_phones') or None
        task_pushplus_tokens = task.get('pushplus_tokens') or None
        task_id = task.get('id')
        last_fail_reason = None

        def build_date_display(date_str):
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                weekday_label = weekday_map[dt.weekday()]
                return dt.strftime("%Y-%m-%d") + f"（{weekday_label}）"
            except Exception:
                return date_str

        def notify_task_result(success, message, items=None, date_str=None, partial=False):
            if partial:
                prefix = "预订部分成功。"
            else:
                prefix = "预订成功。" if success else "【预订失败】"
            details = message
            if (success or partial) and date_str and items:
                success_pairs = []
                seen = set()
                for it in items:
                    p = it.get("place")
                    t = it.get("time")
                    if p is None or not t:
                        continue
                    key = f"{p}|{t}"
                    if key in seen:
                        continue
                    seen.add(key)
                    success_pairs.append(f"{p}号{t}")
                pair_text = "、".join(success_pairs) if success_pairs else message
                details = f"{build_date_display(date_str)}，{pair_text}"
            elif date_str:
                details = f"{build_date_display(date_str)} {message}"
            content = f"{prefix}{details}"
            self.send_notification(content, phones=task_phones)
            self.send_wechat_notification(content, tokens=task_pushplus_tokens)

            run_metrics["result_status"] = "success" if success else ("partial" if partial else "fail")
            run_metrics["result_msg"] = str(message or "")[:200]
            if success:
                run_metrics["goal_achieved"] = True
                run_metrics["failed_item_count"] = 0
                if items:
                    run_metrics["success_item_count"] = max(int(run_metrics.get("success_item_count") or 0), len(items))
            if date_str:
                run_metrics["target_date"] = str(date_str)
            if (success or partial) and run_metrics.get("first_success_ms") is None:
                run_metrics["first_success_ms"] = int(max(0.0, time.time() - active_started_ts) * 1000)

        def finalize_run_metrics(date_str=None):
            try:
                now_ts = time.time()
                run_metrics["finished_at"] = int(now_ts * 1000)
                run_metrics["duration_ms"] = int(max(0.0, now_ts - run_started_ts) * 1000)
                run_metrics["active_duration_ms"] = int(max(0.0, now_ts - active_started_ts) * 1000)
                if date_str and not run_metrics.get("target_date"):
                    run_metrics["target_date"] = str(date_str)
                samples = sorted(int(x) for x in (run_metrics.get("submit_latencies_ms") or []) if x is not None)
                run_metrics["submit_latency_p50_ms"] = int(_percentile(samples, 0.5)) if samples else None
                run_metrics["submit_latency_p95_ms"] = int(_percentile(samples, 0.95)) if samples else None
                run_metrics["success_within_60s"] = bool(
                    run_metrics.get("first_success_ms") is not None and int(run_metrics.get("first_success_ms") or 0) <= 60000
                )
                append_task_run_metric(run_metrics)
                log(
                    f"📊 [run-metric] task={run_metrics.get('task_id')} attempts={run_metrics.get('attempt_count')} "
                    f"first_matrix={run_metrics.get('first_matrix_ok_ms')}ms first_submit={run_metrics.get('first_submit_ms')}ms "
                    f"first_success={run_metrics.get('first_success_ms')}ms p95={run_metrics.get('submit_latency_p95_ms')}ms"
                )
            except Exception as e:
                log(f"⚠️ [run-metric] 汇总失败: {e}")

        # 0. 先检查 token 是否有效（只记录日志，不立刻报警）
        #    以“获取场地状态异常”为准触发短信提醒，避免误报
        is_valid, token_msg = client.check_token()
        if not is_valid:
            log(f"⚠️ Token 可能已失效，但继续尝试获取场地状态: {token_msg}")

        # 1. 计算目标日期
        # 新增 target_mode / target_date 支持：
        # - target_mode == 'fixed' 且有 target_date 时，直接使用该日期
        # - 否则退回到旧逻辑：使用 target_day_offset 延后 N 天
        target_mode = task.get('target_mode', 'offset')
        if target_mode == 'fixed' and task.get('target_date'):
            target_date = str(task['target_date'])
        else:
            offset_days = int(task.get('target_day_offset', 0))
            run_time = str(task.get('run_time') or '00:00:00')
            if len(run_time) == 5:
                run_time += ':00'
            try:
                hh, mm, ss = [int(x) for x in run_time.split(':')[:3]]
            except Exception:
                hh, mm, ss = 0, 0, 0

            aligned_now = client.get_aligned_now()
            base_run = aligned_now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            # 调度线程触发和服务端时间存在秒级偏差，给一个小宽限避免“刚过点就滚到明天/下周”
            trigger_grace_seconds = 90
            t_type = task.get('type', 'daily')
            if t_type in ('daily', 'once'):
                if (aligned_now - base_run).total_seconds() > trigger_grace_seconds:
                    base_run = base_run + timedelta(days=1)
            elif t_type == 'weekly':
                current_weekday = aligned_now.weekday()  # 周一=0
                target_weekday = int(task.get('weekly_day', 0))
                diff = target_weekday - current_weekday
                if diff < 0:
                    diff += 7
                elif diff == 0 and (aligned_now - base_run).total_seconds() > trigger_grace_seconds:
                    diff += 7
                base_run = base_run + timedelta(days=diff)

            target_date = (base_run + timedelta(days=offset_days)).strftime("%Y-%m-%d")
            log(
                f"🕒 [时间对齐] server_offset={round(client.server_time_offset_seconds, 3)}s, "
                f"base_run={base_run.strftime('%Y-%m-%d %H:%M:%S')}, target_date={target_date}"
            )

            aligned_now_after = client.get_aligned_now()
            if aligned_now_after < base_run:
                wait_s = (base_run - aligned_now_after).total_seconds()
                if 0 < wait_s <= 120:
                    log(f"⏳ [时间对齐] 服务端未到触发时刻，等待 {round(wait_s, 2)}s 后开始抢票")
                    time.sleep(wait_s)

        active_started_ts = time.time()
        run_metrics["active_started_at"] = int(active_started_ts * 1000)
        run_metrics["prestart_wait_ms"] = int(max(0.0, active_started_ts - run_started_ts) * 1000)

        config = task.get('config')

        # 2. 安全检查：确保 config 是 dict
        if not isinstance(config, dict):
            if config is not None:
                log(f"⚠️ 警告: 任务 {task.get('id')} 的 config 字段类型异常 ({type(config)})，已重置为空字典")
            config = {}

        # 3. 旧版兼容：没有新配置时走最早的 items 逻辑
        if not config and 'items' in task:
            res = client.submit_order(target_date, task['items'])
            status = res.get("status")
            if status == "success":
                notify_task_result(True, "已预订", items=res.get('success_items') or task['items'], date_str=target_date)
            elif status == "partial":
                notify_task_result(False, "部分成功", items=res.get('success_items') or task['items'], date_str=target_date, partial=True)
            else:
                notify_task_result(False, f"下单失败：{res.get('msg')}", items=task['items'], date_str=target_date)
            finalize_run_metrics(target_date)
            return

        # 4. 这次任务真正关心的 (场地, 时间) 组合，用来判断是否还在“锁定未开放”阶段
        def enumerate_candidate_pairs(cfg):
            pairs = set()
            mode = cfg.get('mode', 'normal')
            target_times = cfg.get('target_times', [])

            if mode in ('normal', 'pipeline'):
                for p in cfg.get('candidate_places', []):
                    for t in target_times:
                        pairs.add((str(p), t))

            elif mode == 'priority':
                sequences = cfg.get('priority_sequences', [])
                for t in target_times:
                    for seq in sequences:
                        for p in seq:
                            pairs.add((str(p), t))

            elif mode == 'time_priority':
                candidate_places = [str(p) for p in cfg.get('candidate_places', [])]
                if not candidate_places:
                    candidate_places = [str(i) for i in range(1, 16)]
                sequences = cfg.get('priority_time_sequences', []) or [[t] for t in target_times]
                for seq in sequences:
                    for t in seq:
                        for p in candidate_places:
                            pairs.add((p, t))
            return pairs

        def calc_pipeline_deadline(cfg, date_str):
            pipeline_cfg = cfg.get('pipeline') if isinstance(cfg.get('pipeline'), dict) else {}
            mode = str(pipeline_cfg.get('greedy_end_mode') or '').strip()

            abs_raw = str(pipeline_cfg.get('greedy_end_time') or '').strip()
            if abs_raw:
                try:
                    return datetime.strptime(abs_raw, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            if mode == 'before_start':
                hours_raw = pipeline_cfg.get('greedy_end_before_hours', 24)
                try:
                    hours = float(hours_raw)
                except Exception:
                    hours = 24.0
                times = [str(t).strip() for t in (cfg.get('target_times') or []) if str(t).strip()]
                if times:
                    start_time = sorted(times)[0]
                    fmt = "%Y-%m-%d %H:%M:%S" if len(start_time) == 8 else "%Y-%m-%d %H:%M"
                    try:
                        start_dt = datetime.strptime(f"{date_str} {start_time}", fmt)
                        return start_dt - timedelta(hours=hours)
                    except Exception:
                        return None
            return None

        def build_pipeline_cfg(cfg):
            pipe = cfg.get('pipeline') if isinstance(cfg.get('pipeline'), dict) else {}
            stages = pipe.get('stages') if isinstance(pipe.get('stages'), list) else []
            if not stages:
                stages = [
                    {"type": "continuous", "enabled": True, "window_seconds": 8},
                    {"type": "random", "enabled": True, "window_seconds": 12},
                    {"type": "refill", "enabled": False, "interval_seconds": 15},
                ]
            return {
                "stages": stages,
                "stop_when_reached": bool(pipe.get('stop_when_reached', True)),
                "continuous_prefer_adjacent": bool(pipe.get('continuous_prefer_adjacent', True)),
                "no_progress_switch_rounds": max(1, int(pipe.get('no_progress_switch_rounds', 2) or 2)),
            }

        def calc_pipeline_need(cfg, date_str):
            nonlocal pipeline_last_known_mine_slots
            target_times = [str(t) for t in (cfg.get('target_times') or [])]
            candidate_places = [str(p) for p in (cfg.get('candidate_places') or [])]
            target_count = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))

            task_scope = {(p, t) for p in candidate_places for t in target_times}
            mine_slots = set()
            orders_res = client.get_place_orders()
            if "error" not in orders_res:
                mine_slots = client._extract_mine_slots(orders_res.get("data", []), date_str)
                pipeline_last_known_mine_slots = set(mine_slots)
            else:
                if isinstance(pipeline_last_known_mine_slots, set) and pipeline_last_known_mine_slots:
                    mine_slots = set(pipeline_last_known_mine_slots)
                    log(f"⚠️ [pipeline] 订单拉取失败，使用最近一次成功订单快照: {orders_res.get('error')}")
                else:
                    log(f"⚠️ [pipeline] 订单拉取失败，按0占位处理: {orders_res.get('error')}")

            task_mine = mine_slots & task_scope
            need_by_time = {}
            for t in target_times:
                mine_count = sum(1 for p in candidate_places if (p, t) in task_mine)
                need_by_time[t] = max(0, target_count - mine_count)

            return {
                "task_scope": task_scope,
                "task_mine": task_mine,
                "need_by_time": need_by_time,
                "target_times": target_times,
                "candidate_places": candidate_places,
                "target_count": target_count,
            }

        def choose_pipeline_items(matrix, need_res, stage_type, prefer_adjacent=True, pair_fail_cache=None, biz_fail_cooldown_seconds=15.0):
            target_times = need_res['target_times']
            candidate_places = need_res['candidate_places']
            need_by_time = dict(need_res['need_by_time'])
            items = []
            picked_pairs = set()

            def add_pick(p, t):
                key = (str(p), str(t))
                if key in picked_pairs:
                    return False
                if int(need_by_time.get(t, 0)) <= 0:
                    return False
                if matrix.get(str(p), {}).get(str(t)) != 'available':
                    return False
                if isinstance(pair_fail_cache, dict):
                    fail_meta = pair_fail_cache.get(key)
                    if fail_meta and fail_meta.get('type') == 'biz':
                        if (time.time() - float(fail_meta.get('ts', 0.0))) < float(biz_fail_cooldown_seconds):
                            return False
                picked_pairs.add(key)
                items.append({"place": str(p), "time": str(t)})
                need_by_time[str(t)] = max(0, int(need_by_time.get(str(t), 0)) - 1)
                return True

            # continuous 阶段先做“跨时段交集选场”，优先把同一块场在多个时段一起拿下。
            if stage_type == 'continuous':
                required_times = [t for t in target_times if int(need_by_time.get(t, 0)) > 0]
                if required_times:
                    avail_all = [
                        str(p)
                        for p in candidate_places
                        if all(matrix.get(str(p), {}).get(str(t)) == 'available' for t in required_times)
                    ]
                    if avail_all:
                        if prefer_adjacent:
                            nums = sorted({int(p) for p in avail_all if str(p).isdigit()})
                            best = []
                            run = []
                            for n in nums:
                                if not run or n == run[-1] + 1:
                                    run.append(n)
                                else:
                                    if len(run) > len(best):
                                        best = run
                                    run = [n]
                            if len(run) > len(best):
                                best = run

                            if best:
                                ordered = [str(n) for n in best] + [p for p in avail_all if p not in {str(n) for n in best}]
                            else:
                                ordered = list(avail_all)
                        else:
                            ordered = list(avail_all)

                        court_need = max(int(need_by_time.get(t, 0)) for t in required_times)
                        for p in ordered[:max(0, court_need)]:
                            for t in required_times:
                                add_pick(p, t)

            # 第二步：按时段补齐剩余缺口（continuous/random 都会走这步）
            for t in target_times:
                need = int(need_by_time.get(t, 0))
                if need <= 0:
                    continue
                avail = [str(p) for p in candidate_places if matrix.get(str(p), {}).get(str(t)) == 'available']
                if not avail:
                    continue

                if stage_type == 'continuous':
                    if prefer_adjacent:
                        nums = sorted({int(p) for p in avail if str(p).isdigit()})
                        best = []
                        run = []
                        for n in nums:
                            if not run or n == run[-1] + 1:
                                run.append(n)
                            else:
                                if len(run) > len(best):
                                    best = run
                                run = [n]
                        if len(run) > len(best):
                            best = run
                        ordered = [str(n) for n in best] + [p for p in avail if p not in {str(n) for n in best}] if best else avail
                    else:
                        ordered = list(avail)
                else:
                    ordered = list(avail)
                    random.shuffle(ordered)

                if stage_type != 'continuous' and isinstance(pair_fail_cache, dict):
                    def _pair_score(px):
                        meta = pair_fail_cache.get((str(px), str(t))) or {}
                        if meta.get('type') == 'network':
                            return (0, -float(meta.get('ts', 0.0)))
                        if meta.get('type') == 'biz':
                            return (2, float(meta.get('ts', 0.0)))
                        return (1, 0.0)
                    ordered = sorted(ordered, key=_pair_score)

                for p in ordered:
                    if int(need_by_time.get(t, 0)) <= 0:
                        break
                    add_pick(p, t)

            return items

        # === 智能抢票核心逻辑 ===
        retry_interval = CONFIG.get('retry_interval', 0.5)
        aggressive_retry_interval = CONFIG.get('aggressive_retry_interval', 0.3)

        # 新增：锁定状态下的重试间隔 & 最多等待时间
        locked_retry_interval = CONFIG.get('locked_retry_interval', retry_interval)
        locked_max_seconds = CONFIG.get('locked_max_seconds', 60)
        open_retry_seconds = CONFIG.get('open_retry_seconds', 30)

        # 记录进入「锁定等待模式」的起始时间，用于统计已等待多久
        locked_mode_started_at = None
        # 记录进入「已开放但无可用结果」状态的起始时间
        open_mode_started_at = None
        # pipeline 状态
        pipeline_started_at = None
        pipeline_refill_last_at = 0.0
        pipeline_force_random_after_continuous = False
        pipeline_no_progress_rounds = 0
        pipeline_need_before_submit = None
        pipeline_none_stage_without_refill = False
        pipeline_last_known_mine_slots = None
        pair_fail_cache = {}
        pair_fail_cache_ttl_s = 120.0
        pair_fail_cache_max = 300
        has_submitted_once = False
        preselect_cache = None

        def _preselect_candidates_from_need(matrix, need_res, prefer_adjacent=True):
            target_times = [str(t) for t in (need_res.get("target_times") or [])]
            candidate_places = [str(p) for p in (need_res.get("candidate_places") or [])]
            need_by_time = dict(need_res.get("need_by_time") or {})
            picks = []
            picked = set()

            def add_pick(p, t):
                k = (str(p), str(t))
                if k in picked:
                    return
                if int(need_by_time.get(str(t), 0)) <= 0:
                    return
                st = matrix.get(str(p), {}).get(str(t))
                if st not in ("available", "locked"):
                    return
                picks.append({"place": str(p), "time": str(t)})
                picked.add(k)
                need_by_time[str(t)] = max(0, int(need_by_time.get(str(t), 0)) - 1)

            req_times = [t for t in target_times if int(need_by_time.get(t, 0)) > 0]
            if req_times:
                common = []
                for p in candidate_places:
                    ok = True
                    score = 0
                    for t in req_times:
                        st = matrix.get(str(p), {}).get(str(t))
                        if st not in ("available", "locked"):
                            ok = False
                            break
                        score += 2 if st == "available" else 1
                    if ok:
                        common.append((str(p), score))
                if common:
                    ordered = [p for p,_ in sorted(common, key=lambda x: (-x[1], int(x[0]) if x[0].isdigit() else 999))]
                    if prefer_adjacent:
                        nums=[int(p) for p in ordered if p.isdigit()]
                        best=[];run=[]
                        for n in nums:
                            if not run or n==run[-1]+1: run.append(n)
                            else:
                                if len(run)>len(best): best=run
                                run=[n]
                        if len(run)>len(best): best=run
                        if best:
                            b=[str(n) for n in best]
                            ordered=b+[p for p in ordered if p not in set(b)]
                    court_need = max(int(need_by_time.get(t, 0)) for t in req_times)
                    for p in ordered[:max(0,court_need)]:
                        for t in req_times:
                            add_pick(p,t)

            for t in target_times:
                need = int(need_by_time.get(t, 0))
                if need <= 0:
                    continue
                avail = [str(p) for p in candidate_places if matrix.get(str(p), {}).get(str(t)) in ("available", "locked")]
                if not avail:
                    continue
                avail = sorted(avail, key=lambda p: (0 if matrix.get(str(p), {}).get(str(t))=="available" else 1, int(p) if p.isdigit() else 999))
                for p in avail:
                    if int(need_by_time.get(str(t), 0)) <= 0:
                        break
                    add_pick(p, t)
            return picks

        def compact_pair_fail_cache(now_ts=None):
            ts = float(now_ts or time.time())
            for k in list(pair_fail_cache.keys()):
                item = pair_fail_cache.get(k) or {}
                if (ts - float(item.get('ts', 0.0))) > pair_fail_cache_ttl_s:
                    pair_fail_cache.pop(k, None)
            if len(pair_fail_cache) > pair_fail_cache_max:
                extra = len(pair_fail_cache) - pair_fail_cache_max
                old_keys = sorted(pair_fail_cache.keys(), key=lambda k: float((pair_fail_cache.get(k) or {}).get('ts', 0.0)))[:extra]
                for k in old_keys:
                    pair_fail_cache.pop(k, None)

        def classify_fail_type(msg):
            text = str(msg or "").lower()
            network_keys = [
                "timeout", "timed out", "connection", "non-json", "404", "502", "503", "504",
                "nginx", "bad gateway", "service unavailable", "temporarily unavailable",
            ]
            if any(k in text for k in network_keys):
                return "network"
            return "biz"

        attempt = 0
        while True:

            # 允许在运行过程中通过 config.json 调整重试速度
            retry_interval = CONFIG.get('retry_interval', retry_interval)
            aggressive_retry_interval = CONFIG.get('aggressive_retry_interval', aggressive_retry_interval)
            locked_retry_interval = CONFIG.get('locked_retry_interval', locked_retry_interval)
            locked_max_seconds = CONFIG.get('locked_max_seconds', locked_max_seconds)
            open_retry_seconds = CONFIG.get('open_retry_seconds', open_retry_seconds)

            attempt += 1
            run_metrics["attempt_count"] = int(attempt)
            compact_pair_fail_cache()
            log(f"🔄 第 {attempt} 轮无限尝试...喵")

            # 1. 获取最新场地状态
            include_mine_overlay = attempt > 1
            if not include_mine_overlay:
                log("⚡ [加速] 首轮跳过mine覆盖，优先抢占可用库存")
            matrix_res = client.get_matrix(target_date, include_mine_overlay=include_mine_overlay)

            # 1.1 错误处理（服务器崩了 / token 失效等）
            if "error" in matrix_res:
                err_msg = matrix_res["error"]
                log(f"获取状态失败: {err_msg} 喵")

                # 服务器短时异常（404/5xx/网关/超时/非JSON等）—— 死磕模式
                err_l = str(err_msg or "").lower()
                transient_keywords = [
                    "非json格式", "non-json", "404", "502", "503", "504", "无效数据",
                    "nginx", "bad gateway", "service unavailable", "timeout", "timed out",
                    "connection reset", "max retries exceeded", "temporarily unavailable",
                ]
                if any(k in err_l for k in transient_keywords):
                    log(f"⚠️ 检测到服务器短时异常，启用高频重试 ({aggressive_retry_interval}s)")
                    time.sleep(aggressive_retry_interval)
                    continue

                # 会话 / 凭证失效，这种重试也没用，直接报警退出
                if "失效" in err_msg or "凭证" in err_msg or "token" in err_msg.lower():
                    log(f"❌ 严重错误: {err_msg}，任务终止。")
                    notify_task_result(False, f"登录状态/Token 失效({err_msg})，请尽快处理！", date_str=target_date)
                    finalize_run_metrics(target_date)
                    return

                # 普通错误：按普通间隔重试
                time.sleep(retry_interval)
                continue

        # 执行循环之外：落盘本次任务关键指标（用于次日复盘）
        # 注意：正常流程基本都在 while 内 return，本段作为兜底；
        # 另外在 finally 中统一写盘可覆盖绝大多数 return 路径。

            # 1.2 正常拿到矩阵
            if run_metrics.get("first_matrix_ok_ms") is None:
                run_metrics["first_matrix_ok_ms"] = int(max(0.0, time.time() - active_started_ts) * 1000)
            matrix = matrix_res.get("matrix", {})

            mode_configs = config.get('modes') if isinstance(config.get('modes'), list) and config.get('modes') else [config]

            # 2. 判断当前目标是否还有「锁定未开放」的场次
            locked_exists = False
            for cfg in mode_configs:
                for p, t in enumerate_candidate_pairs(cfg):
                    state = matrix.get(str(p), {}).get(t)
                    if state == "locked":
                        locked_exists = True
                        break
                if locked_exists:
                    break

            if locked_exists:
                run_metrics["saw_locked"] = True
            elif run_metrics.get("saw_locked"):
                run_metrics["unlocked_after_locked"] = True

            preselect_enabled = bool(CONFIG.get("preselect_enabled", True))
            preselect_ttl_s = max(0.2, float(CONFIG.get("preselect_ttl_seconds", 2.0) or 2.0))
            preselect_only_before_first_submit = bool(CONFIG.get("preselect_only_before_first_submit", True))

            # 3. 单任务多模式：按顺序尝试，命中一个模式后仅使用该模式结果，不跨模式补齐
            final_items: list[dict] = []
            selected_mode = None
            selected_cfg = None
            pipeline_active_stage = None
            pipeline_cfg_for_retry = None
            pipeline_refill_wait_seconds = 0.0
            pipeline_none_stage_without_refill = False
            preselect_used = False
            if preselect_enabled and preselect_cache and not locked_exists and (not preselect_only_before_first_submit or not has_submitted_once):
                age_s = max(0.0, time.time() - float(preselect_cache.get("ts", 0.0)))
                if age_s <= preselect_ttl_s and (preselect_cache.get("date") == target_date):
                    final_items = [dict(x) for x in (preselect_cache.get("items") or [])]
                    selected_mode = preselect_cache.get("mode")
                    selected_cfg = mode_configs[int(preselect_cache.get("cfg_idx", 0))] if mode_configs else None
                    preselect_used = bool(final_items)
                    if preselect_used:
                        run_metrics["preselect_hit_count"] = int(run_metrics.get("preselect_hit_count") or 0) + 1
                        log(f"🚀 [preselect] 命中预选组合，age={round(age_s*1000)}ms，直接下单: {final_items}")
                else:
                    run_metrics["preselect_miss_count"] = int(run_metrics.get("preselect_miss_count") or 0) + 1

            if not preselect_used:
                for cfg_idx, cfg in enumerate(mode_configs):
                    mode = cfg.get('mode', 'normal')
                    target_times = cfg.get('target_times', [])
                    mode_items: list[dict] = []

                    # --- 模式 P: pipeline(continuous/random/refill) ---
                    if mode == 'pipeline':
                        pipeline_cfg_for_retry = cfg
                        now_ts = time.time()
                        if pipeline_started_at is None:
                            pipeline_started_at = now_ts

                        need_res = calc_pipeline_need(cfg, target_date)
                        pipe_cfg = build_pipeline_cfg(cfg)
                        current_need_total = sum(int(v) for v in (need_res.get('need_by_time') or {}).values())

                        if sum(need_res['need_by_time'].values()) == 0 and pipe_cfg['stop_when_reached']:
                            achieved_count = len(need_res.get("task_mine") or [])
                            run_metrics["success_item_count"] = max(int(run_metrics.get("success_item_count") or 0), achieved_count)
                            run_metrics["failed_item_count"] = 0
                            notify_task_result(True, "已达任务目标，无需补齐", date_str=target_date)
                            finalize_run_metrics(target_date)
                            return

                        deadline = calc_pipeline_deadline(cfg, target_date)
                        if deadline and client.get_aligned_now() >= deadline:
                            notify_task_result(False, f"达到截止时间({deadline.strftime('%Y-%m-%d %H:%M:%S')})，停止补齐", date_str=target_date)
                            finalize_run_metrics(target_date)
                            return

                        stages = pipe_cfg['stages']

                        elapsed = now_ts - pipeline_started_at
                        active_stage = None
                        consumed = 0.0
                        refill_stage = None
                        for st in stages:
                            if not isinstance(st, dict) or not st.get('enabled', True):
                                continue
                            stype = str(st.get('type') or '').strip()
                            if stype == 'refill':
                                refill_stage = st
                                continue
                            win = float(st.get('window_seconds', 0) or 0)
                            if win <= 0:
                                continue
                            if elapsed < consumed + win:
                                active_stage = st
                                break
                            consumed += win

                        if active_stage is None and refill_stage is not None:
                            active_stage = refill_stage

                        stype = str((active_stage or {}).get('type') or '').strip()
                        if stype == 'continuous' and pipeline_no_progress_rounds >= int(pipe_cfg.get('no_progress_switch_rounds', 2)):
                            log(f"🧪 [pipeline] 连续{pipeline_no_progress_rounds}轮缺口未改善，提前切换到random")
                            stype = 'random'
                        if stype == 'continuous' and pipeline_force_random_after_continuous:
                            log("🧪 [pipeline] 检测到continuous阶段已出现缺口，提前切换到random补齐")
                            stype = 'random'
                        pipeline_active_stage = stype
                        log(f"🧪 [pipeline] 当前阶段={stype or 'none'} elapsed={round(elapsed, 2)}s")
                        if not stype and refill_stage is None and bool(CONFIG.get('stop_on_none_stage_without_refill', False)):
                            pipeline_none_stage_without_refill = True
                            log("🧪 [pipeline] 阶段窗口已结束且未启用refill，按配置立即结束任务")
                        if stype == 'continuous':
                            mode_items = choose_pipeline_items(matrix, need_res, 'continuous', prefer_adjacent=pipe_cfg.get('continuous_prefer_adjacent', True), pair_fail_cache=pair_fail_cache, biz_fail_cooldown_seconds=CONFIG.get('biz_fail_cooldown_seconds', 15.0))
                        elif stype == 'random':
                            mode_items = choose_pipeline_items(matrix, need_res, 'random', prefer_adjacent=pipe_cfg.get('continuous_prefer_adjacent', True), pair_fail_cache=pair_fail_cache, biz_fail_cooldown_seconds=CONFIG.get('biz_fail_cooldown_seconds', 15.0))
                        elif stype == 'refill':
                            interval = float((active_stage or {}).get('interval_seconds', 15) or 15)
                            refill_interval = max(1.0, interval)
                            refill_elapsed = now_ts - pipeline_refill_last_at
                            if refill_elapsed >= refill_interval:
                                mode_items = choose_pipeline_items(matrix, need_res, 'random', prefer_adjacent=pipe_cfg.get('continuous_prefer_adjacent', True), pair_fail_cache=pair_fail_cache, biz_fail_cooldown_seconds=CONFIG.get('biz_fail_cooldown_seconds', 15.0))
                                pipeline_refill_last_at = now_ts
                                pipeline_refill_wait_seconds = 0.0
                            else:
                                pipeline_refill_wait_seconds = max(0.0, refill_interval - refill_elapsed)
                                log(f"🧪 [pipeline-refill] 未到下次补齐窗口，剩余 {round(pipeline_refill_wait_seconds, 2)}s")
                                mode_items = []
                        else:
                            mode_items = []

                        if stype in ('continuous', 'random', 'refill'):
                            pipeline_need_before_submit = current_need_total
                            if preselect_enabled and (not preselect_only_before_first_submit or not has_submitted_once):
                                pre_items = _preselect_candidates_from_need(matrix, need_res, prefer_adjacent=pipe_cfg.get('continuous_prefer_adjacent', True))
                                if pre_items:
                                    preselect_cache = {"items": pre_items, "ts": time.time(), "date": target_date, "mode": "pipeline", "cfg_idx": cfg_idx}
                                    if locked_exists:
                                        log(f"🧠 [preselect] 锁定期预选组合已更新: {pre_items}")

                    # --- 模式 A: 场地优先优先级序列 (priority) ---
                    elif mode == 'priority':
                        sequences = cfg.get('priority_sequences', [])
                        target_count = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
                        allow_partial = cfg.get('allow_partial', True)

                        for time_slot in target_times:
                            if len(mode_items) >= target_count:
                                break
                            for seq in sequences:
                                if len(mode_items) >= target_count:
                                    break
                                if len(seq) > (target_count - len(mode_items)):
                                    continue

                                all_avail = True
                                for p in seq:
                                    if p not in matrix or matrix[p].get(time_slot) != "available":
                                        all_avail = False
                                        break

                                if all_avail:
                                    for p in seq:
                                        for item in mode_items:
                                            if item['place'] == str(p) and item['time'] == time_slot:
                                                all_avail = False
                                                break

                                if all_avail:
                                    log(f"   -> 🎯 [优先级-整] 命中完整组合: {seq} @ {time_slot}")
                                    for p in seq:
                                        mode_items.append({"place": str(p), "time": time_slot})

                        if allow_partial and len(mode_items) < target_count:
                            log(f"   -> ⚠️ [优先级-散] 完整组合不足，开始散单填充 (目标{target_count}, 已有{len(mode_items)})")
                            for time_slot in target_times:
                                if len(mode_items) >= target_count:
                                    break
                                for seq in sequences:
                                    if len(mode_items) >= target_count:
                                        break
                                    for p in seq:
                                        if p in matrix and matrix[p].get(time_slot) == "available":
                                            is_picked = False
                                            for item in mode_items:
                                                if item['place'] == str(p) and item['time'] == time_slot:
                                                    is_picked = True
                                                    break
                                            if not is_picked:
                                                log(f"   -> 🧩 [优先级-散] 捡漏: {p}号 @ {time_slot}")
                                                mode_items.append({"place": str(p), "time": time_slot})
                                                if len(mode_items) >= target_count:
                                                    break

                    # --- 模式 B: 时间优先 (time_priority) ---
                    elif mode == 'time_priority':
                        sequences = cfg.get('priority_time_sequences', []) or [[t] for t in target_times]
                        candidate_places = [str(p) for p in cfg.get('candidate_places', [])]
                        if not candidate_places:
                            candidate_places = [str(i) for i in range(1, 16)]

                        target_count = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
                        allow_partial = cfg.get('allow_partial', True)

                        for seq in sequences:
                            if len(mode_items) >= target_count:
                                break
                            for p in candidate_places:
                                if len(mode_items) >= target_count:
                                    break

                                ok = True
                                for t in seq:
                                    if p not in matrix or matrix[p].get(t) != "available":
                                        ok = False
                                        break
                                if not ok:
                                    continue

                                already = False
                                for t in seq:
                                    for item in mode_items:
                                        if item["place"] == p and item["time"] == t:
                                            already = True
                                            break
                                    if already:
                                        break
                                if already:
                                    continue

                                log(f"   -> 🎯 [时间优先-整] {p}号 命中时间段 {seq}")
                                for t in seq:
                                    mode_items.append({"place": p, "time": t})
                                if len(mode_items) >= target_count:
                                    break

                        if allow_partial and len(mode_items) < target_count:
                            for t in target_times:
                                if len(mode_items) >= target_count:
                                    break
                                for p in candidate_places:
                                    if len(mode_items) >= target_count:
                                        break
                                    if p in matrix and matrix[p].get(t) == "available":
                                        already = False
                                        for item in mode_items:
                                            if item["place"] == p and item["time"] == t:
                                                already = True
                                                break
                                        if not already:
                                            mode_items.append({"place": p, "time": t})
                                            log(f"   -> 🧩 [时间优先-散] 捡漏: {p}号 @ {t}")

                    # --- 模式 C: 普通 / 智能连号 (normal) ---
                    else:
                        if 'candidate_places' not in cfg:
                            log(f"❌ 任务配置错误: 非优先级模式必须包含 candidate_places")
                            notify_task_result(False, "任务配置错误：缺少 candidate_places。", date_str=target_date)
                            finalize_run_metrics(target_date)
                            return

                        candidate_places = [str(p) for p in cfg['candidate_places']]
                        target_courts = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
                        smart_mode = cfg.get('smart_continuous', False)

                        if target_courts <= 0:
                            log("⚠️ 目标场地数量 target_count <= 0，跳过本轮。")
                        else:
                            available_courts: list[int] = []
                            for p in candidate_places:
                                p_str = str(p)
                                ok = True
                                for t in target_times:
                                    if p_str not in matrix or matrix[p_str].get(t) != "available":
                                        ok = False
                                        break
                                if ok:
                                    available_courts.append(int(p))

                            if not available_courts:
                                log("⚠️ 当前没有同时满足所有时间段的候选场地。")
                            else:
                                available_courts.sort()
                                need = min(target_courts, len(available_courts))
                                selected_courts: list[int] = []

                                if smart_mode and len(available_courts) > 1:
                                    best_run: list[int] | None = None
                                    best_len = 0
                                    i = 0
                                    while i < len(available_courts):
                                        j = i
                                        while j + 1 < len(available_courts) and                                             available_courts[j + 1] == available_courts[j] + 1:
                                            j += 1
                                        run = available_courts[i: j + 1]
                                        if len(run) > best_len:
                                            best_len = len(run)
                                            best_run = run
                                        i = j + 1

                                    if best_run:
                                        selected_courts = best_run[:need]

                                if not selected_courts:
                                    selected_courts = available_courts[:need]

                                for p_int in selected_courts:
                                    p_str = str(p_int)
                                    for t in target_times:
                                        mode_items.append({"place": p_str, "time": t})

                    if mode_items and preselect_enabled and (not preselect_only_before_first_submit or not has_submitted_once):
                        preselect_cache = {"items": [dict(x) for x in mode_items], "ts": time.time(), "date": target_date, "mode": mode, "cfg_idx": cfg_idx}
                    if mode_items:
                        final_items = mode_items
                        selected_mode = mode
                        selected_cfg = cfg
                        break

                if selected_mode and len(mode_configs) > 1:
                    log(f"🎛️ 单任务多模式命中: 当前使用 {selected_mode} 模式提交，不跨模式补齐")

                if not final_items and pipeline_none_stage_without_refill:
                    notify_task_result(False, "pipeline阶段窗口已结束且未启用refill，停止继续轮询", date_str=target_date)
                    finalize_run_metrics(target_date)
                    return

                # 4. 提交订单
            if final_items:
                submit_started_at = time.time()
                if run_metrics.get("first_submit_ms") is None:
                    run_metrics["first_submit_ms"] = int(max(0.0, submit_started_at - active_started_ts) * 1000)
                log(f"正在提交分批订单: {final_items}")
                res = client.submit_order(target_date, final_items)
                has_submitted_once = True
                submit_spent_s = max(0.0, time.time() - submit_started_at)
                run_metrics.setdefault("submit_latencies_ms", []).append(int(submit_spent_s * 1000))
                if selected_mode == 'pipeline' and pipeline_started_at is not None and submit_spent_s > 0:
                    # 提交/校验耗时不应吞掉 pipeline 阶段窗口，否则会导致 random/refill 阶段被提前跳过
                    pipeline_started_at += submit_spent_s
                    log(f"⏱️ [pipeline] 扣除本轮提交流水耗时 {round(submit_spent_s, 2)}s，避免阶段窗口被网络耗时吃掉")
                log(f"[submit_order调试] 批次响应: {res}")

                status = res.get("status")

                # pipeline 模式下，单次提交 success/partial 不代表任务目标已达成；
                # 若仍有缺口，应继续进入下一轮（含 refill）补齐。
                if selected_mode == 'pipeline' and isinstance(selected_cfg, dict):
                    post_need = calc_pipeline_need(selected_cfg, target_date)
                    remaining_slots = sum(int(v) for v in (post_need.get('need_by_time') or {}).values())
                    if remaining_slots > 0:
                        if pipeline_active_stage == 'continuous' and status in ('success', 'partial'):
                            pipeline_force_random_after_continuous = True
                            log("⚡ [pipeline] continuous阶段已提交但仍有缺口，下一轮将直接切到random")
                        deadline = calc_pipeline_deadline(selected_cfg, target_date)
                        if deadline and client.get_aligned_now() >= deadline:
                            notify_task_result(False, f"达到截止时间({deadline.strftime('%Y-%m-%d %H:%M:%S')})，停止补齐", date_str=target_date)
                            finalize_run_metrics(target_date)
                            return
                        need_detail = post_need.get('need_by_time') or {}
                        before_need = int(pipeline_need_before_submit if pipeline_need_before_submit is not None else remaining_slots)
                        if remaining_slots < before_need:
                            pipeline_no_progress_rounds = 0
                        else:
                            pipeline_no_progress_rounds += 1
                        log(f"🔁 [pipeline] 本轮提交后仍缺 {remaining_slots} 个时段，缺口明细: {need_detail}，继续补齐下一轮")

                        if status in ('success', 'partial'):
                            try:
                                progress_items = res.get('success_items') or final_items
                                progress_msg = f"本轮已预订 {len(progress_items)} 个时段，缺口 {remaining_slots}，继续补齐中"
                                notify_task_result(
                                    False,
                                    progress_msg,
                                    items=progress_items,
                                    date_str=target_date,
                                    partial=True,
                                )
                            except Exception as e:
                                log(f"⚠️ [pipeline] 阶段通知构建失败: {e}")

                        time.sleep(retry_interval)
                        continue

                if status == "success":
                    run_metrics["success_item_count"] = max(int(run_metrics.get("success_item_count") or 0), len(res.get("success_items") or final_items or []))
                    run_metrics["failed_item_count"] = len(res.get("failed_items") or [])
                    run_metrics["goal_achieved"] = True
                    log(f"✅ 下单完成: 全部成功 ({status})")
                    for it in (res.get('success_items') or final_items or []):
                        pair_fail_cache.pop((str(it.get('place')), str(it.get('time'))), None)
                    try:
                        notify_task_result(
                            True,
                            "已预订",
                            items=res.get('success_items') or final_items,
                            date_str=target_date,
                        )
                    except Exception as e:
                        log(f"构建短信内容失败: {e}")
                    finalize_run_metrics(target_date)
                    return
                elif status == "partial":
                    run_metrics["success_item_count"] = max(int(run_metrics.get("success_item_count") or 0), len(res.get("success_items") or []))
                    run_metrics["failed_item_count"] = max(int(run_metrics.get("failed_item_count") or 0), len(res.get("failed_items") or []))
                    log(f"⚠️ 下单完成: 部分成功 ({status})")
                    for it in (res.get('success_items') or []):
                        pair_fail_cache.pop((str(it.get('place')), str(it.get('time'))), None)
                    fail_type = classify_fail_type(res.get('msg'))
                    for it in (res.get('failed_items') or []):
                        pair_fail_cache[(str(it.get('place')), str(it.get('time')))] = {'type': fail_type, 'ts': time.time()}
                    try:
                        notify_task_result(
                            False,
                            "部分成功",
                            items=res.get('success_items') or final_items,
                            date_str=target_date,
                            partial=True,
                        )
                    except Exception as e:
                        log(f"构建短信内容失败: {e}")
                    finalize_run_metrics(target_date)
                    return
                elif status == "verify_pending":
                    fast_retry_s = max(0.05, float(CONFIG.get("post_submit_verify_pending_retry_seconds", 0.35) or 0.35))
                    log(f"⏳ 提交成功但验证未收敛，{round(fast_retry_s, 2)}s 后快速复核: {res.get('msg')}")
                    time.sleep(fast_retry_s)
                    continue
                else:
                    run_metrics["failed_item_count"] = max(int(run_metrics.get("failed_item_count") or 0), len(res.get("failed_items") or final_items or []))
                    log(f"❌ 下单失败: {res.get('msg')}")
                    last_fail_reason = str(res.get('msg') or "下单失败")
                    fail_type = classify_fail_type(last_fail_reason)
                    for it in (res.get('failed_items') or final_items or []):
                        k = (str(it.get('place')), str(it.get('time')))
                        pair_fail_cache[k] = {'type': fail_type, 'ts': time.time()}
                    last_fail_lower = last_fail_reason.lower()
                    if "<html" in last_fail_lower and "404" in last_fail_lower:
                        last_fail_reason = "下单接口暂时不可用(404)"
                    elif len(last_fail_reason) > 120:
                        last_fail_reason = last_fail_reason[:120] + "..."

            # 5. 根据 locked 状态决定是否继续死磕（使用锁定配置 + 最多刷 N 秒保护）
            if locked_exists:
                now_ts = time.time()
                open_mode_started_at = None

                # 第一次发现 locked，开始计时
                if locked_mode_started_at is None:
                    locked_mode_started_at = now_ts

                elapsed = now_ts - locked_mode_started_at

                # 超过配置的最大等待时间 -> 放弃本次任务
                if elapsed >= locked_max_seconds:
                    log(
                        f"⏳ 已连续等待『锁定未开放』状态约 {int(elapsed)} 秒，"
                        f"达到上限 {locked_max_seconds}s，本次任务结束。"
                    )
                    fail_msg = "锁定未开放等待超时，任务结束。"
                    if last_fail_reason:
                        fail_msg = f"{fail_msg} 失败原因：{last_fail_reason}"
                    notify_task_result(False, fail_msg, date_str=target_date)
                    finalize_run_metrics(target_date)
                    return

                # 仍在允许范围内，按锁定间隔继续轮询
                log(
                    f"⏳ 当前目标场地处于『锁定未开放』状态，继续等待下一轮..."
                    f" (已等待 {int(elapsed)} 秒 / 上限 {locked_max_seconds}s)"
                )
                time.sleep(locked_retry_interval)
                continue
            else:
                # 已开放：短窗口内继续重试，给“释放/回流库存”留机会
                locked_mode_started_at = None
                now_ts = time.time()
                if open_mode_started_at is None:
                    open_mode_started_at = now_ts
                elapsed = now_ts - open_mode_started_at

                # pipeline 进入 refill 后，不受 open_retry_seconds 提前截断；
                # 以 pipeline 截止时间为准继续补齐。
                if pipeline_cfg_for_retry is not None and pipeline_active_stage == 'refill':
                    deadline = calc_pipeline_deadline(pipeline_cfg_for_retry, target_date)
                    if deadline and client.get_aligned_now() >= deadline:
                        notify_task_result(False, f"达到截止时间({deadline.strftime('%Y-%m-%d %H:%M:%S')})，停止补齐", date_str=target_date)
                        finalize_run_metrics(target_date)
                        return
                    refill_sleep_s = retry_interval
                    if not final_items:
                        refill_sleep_s = max(float(retry_interval), float(pipeline_refill_wait_seconds or 0.0))
                    log(
                        f"🙈 [pipeline-refill] 当前无可用组合，继续轮询补齐..."
                        f" (已等待 {int(elapsed)} 秒；以截止时间控制结束；下次约 {round(refill_sleep_s, 2)}s)"
                    )
                    time.sleep(refill_sleep_s)
                    continue

                if elapsed < max(0.0, float(open_retry_seconds)):
                    if final_items:
                        log(
                            f"🙈 场地已开放但本轮提交未成功，继续重试..."
                            f" (已重试 {int(elapsed)} 秒 / 上限 {open_retry_seconds}s)"
                        )
                    else:
                        log(
                            f"🙈 场地已开放但当前无可用组合，继续轮询..."
                            f" (已等待 {int(elapsed)} 秒 / 上限 {open_retry_seconds}s)"
                        )
                    time.sleep(retry_interval)
                    continue

                log("🙈 目标场地已经开放但在重试窗口内仍无可用组合，本次任务结束。")
                fail_msg = "目标场地已开放但无可用组合，可能已被抢完。"
                if last_fail_reason:
                    fail_msg = f"{fail_msg} 失败原因：{last_fail_reason}"
                notify_task_result(False, fail_msg, date_str=target_date)
                finalize_run_metrics(target_date)
                return

        # print(" 所有重试均失败，放弃。")

    def refresh_schedule(self):
        schedule.clear("task")
        print(f"🔄 [调度器] 正在刷新任务列表 (共 {len(self.tasks)} 个)...")

        # 内部工具函数：支持单次任务执行完后自动删除自身
        def make_job(t, is_once=False):
            def _job():
                print(f"⏰ [调度器] 触发任务 ID: {t['id']}")
                self.execute_task_with_lock(t)
                if is_once:
                    print(f"✅ 单次任务 {t['id']} 执行完成，自动从任务列表中删除")
                    # 不再 refresh_schedule，避免在调度循环里频繁清空重建
                    self.delete_task(t['id'], refresh=False)
                    # 告诉 schedule 取消当前 job
                    return schedule.CancelJob

            return _job

        for task in self.tasks:
            run_time = task['run_time']
            # 确保时间格式是 HH:mm:ss (有的浏览器可能只返回 HH:mm)
            if len(run_time) == 5:
                run_time += ":00"

            t_type = task.get('type', 'daily')

            try:
                if t_type == 'daily':
                    schedule.every().day.at(run_time).do(make_job(task, is_once=False)).tag("task")
                    print(f"   -> 已添加每日任务: {run_time}")
                elif t_type == 'weekly':
                    days = [
                        schedule.every().monday,
                        schedule.every().tuesday,
                        schedule.every().wednesday,
                        schedule.every().thursday,
                        schedule.every().friday,
                        schedule.every().saturday,
                        schedule.every().sunday,
                    ]
                    wd = int(task['weekly_day'])
                    days[wd].at(run_time).do(make_job(task, is_once=False)).tag("task")
                    print(f"   -> 已添加每周任务: 周{['一', '二', '三', '四', '五', '六', '日'][wd]} {run_time}")
                elif t_type == 'once':
                    # 单次任务：到点执行一次，然后自动从任务列表和调度器中移除
                    schedule.every().day.at(run_time).do(make_job(task, is_once=True)).tag("task")
                    print(f"   -> 已添加单次任务: {run_time}（执行一次后自动删除）")
            except Exception as e:
                print(f"❌ 添加任务失败: {e}")






def _template_context_lines(text: str, lineno: int, radius: int = 2) -> str:
    lines = text.splitlines()
    start = max(1, lineno - radius)
    end = min(len(lines), lineno + radius)
    out = []
    for i in range(start, end + 1):
        pointer = '>>' if i == lineno else '  '
        out.append(f"{pointer} {i}: {lines[i-1]}")
    return "\n".join(out)



def auto_fix_known_template_endif_issue(template_file: str):
    """自动修复历史上反复出现的重复 endif 问题（最小、定向修复）。"""
    try:
        with open(template_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return

    fixed = re.sub(
        r"(\n\s*\{%\s*endif\s*%\}\s*\n)\s*\{%\s*endif\s*%\}(\s*\n\s*<!--\s*Tab\s*3)",
        r"\1\2",
        content,
        count=1,
    )
    if fixed != content:
        with open(template_file, 'w', encoding='utf-8') as f:
            f.write(fixed)
        print('🛠️ 已自动修复模板中的重复 endif（Tab 2/Tab 3 交界处）')

def validate_templates_on_startup():
    """启动前快速检查关键模板语法，避免线上运行时才暴露 TemplateSyntaxError。"""
    template_file = os.path.join(BASE_DIR, 'templates', 'index.html')
    auto_fix_known_template_endif_issue(template_file)
    try:
        with open(template_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        raise RuntimeError(f'模板文件不存在: {template_file}')

    digest = hashlib.md5(content.encode('utf-8')).hexdigest()[:8]
    print(f'🔎 模板文件校验: {template_file} (md5:{digest})')

    try:
        Environment().parse(content)
        print('✅ 模板语法检查通过')
    except TemplateSyntaxError as e:
        context = _template_context_lines(content, e.lineno, radius=2)
        raise RuntimeError(
            f'模板语法错误({template_file}:{e.lineno}, md5:{digest}): {e.message}\n附近内容:\n{context}'
        )

task_manager = TaskManager()



def smoke_render_pages_on_startup():
    """启动前做最小页面渲染回归，尽早发现模板运行时问题。"""
    with app.test_request_context('/'):
        render_main_page('semi')
        render_main_page('tasks')
        render_main_page('settings')
    print('✅ 页面渲染冒烟检查通过: /, /tasks, /settings')

def run_scheduler():
    print("🚀 [后台] 任务调度线程已启动...")
    while True:
        try:
            schedule.run_pending()
            task_manager.run_refill_scheduler_tick()
        except Exception as e:
            print(f"⚠️ 调度执行出错: {e}")
            print(traceback.format_exc())
        time.sleep(1)

# 启动后台线程
threading.Thread(target=run_scheduler, daemon=True).start()

# ================= 路由 =================

@app.route('/')
def index():
    return render_main_page('semi')


def build_dates():
    dates = []
    today = datetime.now()
    weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
    # 显示未来 14 天 (2周) 以支持更远的预定
    for i in range(14):
        d = today + timedelta(days=i)
        dates.append({
            "val": d.strftime("%Y-%m-%d"),
            "weekday": weekdays[d.weekday()],
            "date_only": d.strftime("%m-%d")
        })
    return dates


def render_main_page(page_mode: str):
    return render_template(
        'index.html',
        dates=build_dates(),
        tasks=task_manager.tasks,
        page_mode=page_mode,
    )


@app.route('/tasks')
@app.route('/tasks/')
def tasks_page():
    return render_main_page('tasks')


@app.route('/settings')
@app.route('/settings/')
def settings_page():
    return render_main_page('settings')

@app.route('/api/matrix')
def api_matrix():
    date = request.args.get('date')
    return jsonify(client.get_matrix(date))

@app.route('/api/mine-overview')
def api_mine_overview():
    orders_res = client.get_place_orders()
    if 'error' in orders_res:
        return jsonify({'error': orders_res.get('error')})
    grouped = client.extract_mine_slots_by_date(orders_res.get('data') or [])
    return jsonify({'records': grouped})


@app.route('/api/time')
def api_time():
    return jsonify({"timestamp": datetime.now().timestamp()})

@app.route('/api/book', methods=['POST'])
def api_book():
    data = request.json
    date = data.get('date')
    items = data.get('items')
    res = client.submit_order(date, items)
    
    # 增加手动预订后的短信通知
    # 只要状态不是 fail，就发送通知（success 或 partial）
    if res.get('status') in ['success', 'partial']:
        print(f"📧 [调试] 准备发送手动预订通知，状态: {res.get('status')}")
        try:
            status_desc = "已预订成功！" if res['status'] == 'success' else "已预订部分成功！"
            detail_msg = f"{status_desc}日期{date}: "
            items_str = []
            for item in items:
                items_str.append(f"{item['place']}号场({item['time']})")
            detail_msg += ",".join(items_str)
            detail_msg += "。"
            
            # 强制检查一次手机号配置
            phones = CONFIG.get('notification_phones', [])
            if not phones:
                print(f"⚠️ [调试] 此时内存中 notification_phones 为空，尝试重新加载...")
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                        saved = json.load(f)
                        CONFIG['notification_phones'] = saved.get('notification_phones', [])
                        print(f"⚠️ [调试] 重新加载后手机号: {CONFIG['notification_phones']}")
            
            task_manager.send_notification(detail_msg)
        except Exception as e:
            print(f"手动预订通知发送失败: {e}")
            print(traceback.format_exc())
            
    else:
        print(f"📧 [调试] 预订状态为 {res.get('status')}，不发送通知。返回msg: {res.get('msg')}")
        
    return jsonify(res)

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(CONFIG)

@app.route('/api/config', methods=['POST'])
def update_config():
    """
    更新全局配置：
    - notification_phones：全局报警手机号（列表，可以填 0~N 个）
    - pushplus_tokens：全局微信通知 token（列表或逗号分隔）
    - retry_interval：普通重试间隔
    - aggressive_retry_interval：死磕模式重试间隔
    - batch_retry_times：分批失败重试次数
    - batch_retry_interval：分批失败重试间隔
    - submit_batch_size：单批提交上限
    - submit_timeout_seconds：下单接口超时(秒)
    - submit_split_retry_times：降级分段重试轮次
    - initial_submit_batch_size：首批提交上限
    - batch_min_interval：批次间最小间隔
    - refill_window_seconds：失败后补提窗口
    - locked_retry_interval：锁定状态重试间隔
    - locked_max_seconds：锁定状态最多刷 N 秒
    - open_retry_seconds：已开放无组合时继续重试窗口
    - matrix_timeout_seconds：查询矩阵超时(秒)，建议高峰期使用短超时
    - order_query_timeout_seconds：订单查询超时(秒)
    - order_query_max_pages：订单查询最大页数
    - post_submit_orders_join_timeout_seconds：提交后订单查询线程等待上限(秒)
    - post_submit_verify_orders_on_matrix_partial_only：仅在矩阵校验存在缺口时再查订单
    - post_submit_orders_sync_fallback：订单线程超时后是否同步兜底
    - post_submit_verify_pending_retry_seconds：验证未收敛时快速复核间隔(秒)
    - post_submit_treat_verify_timeout_as_retry：验证超时是否走快速复核而非直接失败
    - stop_on_none_stage_without_refill：pipeline 阶段结束且无 refill 时立即结束
    - health_check_enabled: 健康检查是否开启
    - health_check_interval_min: 健康检查间隔（分钟）
    - health_check_start_time: 健康检查起始时间（HH:MM）
    - verbose_logs: 是否输出高频调试日志
    - same_time_precheck_limit: 同时段预检上限（<=0 关闭）
    - biz_fail_cooldown_seconds: pipeline 业务失败冷却秒数
    - preselect_enabled：是否启用解锁前预选快照
    - preselect_ttl_seconds：预选快照有效期(秒)
    - preselect_only_before_first_submit：仅首提前启用预选快照
    """
    try:
        data = request.json or {}

        # 读取旧配置，保证 auth / sms 等字段不会丢
        saved = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f) or {}
            except Exception as e:
                print(f"加载配置失败: {e}")
                saved = {}

        # 确保 auth / sms 结构存在（不改动它们）
        if 'auth' not in saved:
            saved['auth'] = CONFIG.get('auth', {}).copy()
        if 'sms' not in saved:
            saved['sms'] = CONFIG.get('sms', {}).copy()

        # 小工具：更新一个浮点字段（带最小值与默认值）
        def _update_float_field(field, min_value, default_value):
            if field not in data:
                return
            try:
                val = float(data[field])
            except (TypeError, ValueError):
                val = default_value
            if val < min_value:
                val = min_value
            CONFIG[field] = val
            saved[field] = val

        # 1) 全局报警手机号
        if 'notification_phones' in data:
            phones = data['notification_phones'] or []
            if isinstance(phones, str):
                phones = [p.strip() for p in phones.split(',') if p.strip()]
            elif isinstance(phones, list):
                phones = [str(p).strip() for p in phones if str(p).strip()]
            else:
                phones = []
            CONFIG['notification_phones'] = phones
            saved['notification_phones'] = phones

        # 1.1) 全局微信通知 token（PushPlus）
        if 'pushplus_tokens' in data:
            tokens = data['pushplus_tokens'] or []
            if isinstance(tokens, str):
                tokens = [t.strip() for t in tokens.split(',') if t.strip()]
            elif isinstance(tokens, list):
                tokens = [str(t).strip() for t in tokens if str(t).strip()]
            else:
                tokens = []
            CONFIG['pushplus_tokens'] = tokens
            saved['pushplus_tokens'] = tokens

        # 2) 各类重试 / 限制配置
        _update_float_field('retry_interval', 0.1, CONFIG.get('retry_interval', 1.0))
        _update_float_field('aggressive_retry_interval', 0.1, CONFIG.get('aggressive_retry_interval', 0.3))
        _update_float_field('batch_retry_interval', 0.1, CONFIG.get('batch_retry_interval', 0.5))
        _update_float_field('submit_timeout_seconds', 0.5, CONFIG.get('submit_timeout_seconds', 4.0))
        _update_float_field('batch_min_interval', 0.1, CONFIG.get('batch_min_interval', 0.8))
        _update_float_field('refill_window_seconds', 0.0, CONFIG.get('refill_window_seconds', 8.0))
        _update_float_field('locked_retry_interval', 0.1, CONFIG.get('locked_retry_interval', 1.0))
        _update_float_field('locked_max_seconds', 1.0, CONFIG.get('locked_max_seconds', 60.0))
        _update_float_field('open_retry_seconds', 0.0, CONFIG.get('open_retry_seconds', 30.0))
        _update_float_field('matrix_timeout_seconds', 0.5, CONFIG.get('matrix_timeout_seconds', 3.0))
        _update_float_field('order_query_timeout_seconds', 0.5, CONFIG.get('order_query_timeout_seconds', 2.5))
        _update_float_field('post_submit_orders_join_timeout_seconds', 0.1, CONFIG.get('post_submit_orders_join_timeout_seconds', 1.2))
        _update_float_field('post_submit_verify_pending_retry_seconds', 0.05, CONFIG.get('post_submit_verify_pending_retry_seconds', 0.35))
        _update_float_field('health_check_interval_min', 1.0, CONFIG.get('health_check_interval_min', 30.0))
        _update_float_field('biz_fail_cooldown_seconds', 1.0, CONFIG.get('biz_fail_cooldown_seconds', 15.0))
        _update_float_field('preselect_ttl_seconds', 0.2, CONFIG.get('preselect_ttl_seconds', 2.0))

        if 'post_submit_verify_orders_on_matrix_partial_only' in data:
            val = data['post_submit_verify_orders_on_matrix_partial_only']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['post_submit_verify_orders_on_matrix_partial_only'] = enabled
            saved['post_submit_verify_orders_on_matrix_partial_only'] = enabled

        if 'post_submit_orders_sync_fallback' in data:
            val = data['post_submit_orders_sync_fallback']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['post_submit_orders_sync_fallback'] = enabled
            saved['post_submit_orders_sync_fallback'] = enabled

        if 'post_submit_treat_verify_timeout_as_retry' in data:
            val = data['post_submit_treat_verify_timeout_as_retry']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['post_submit_treat_verify_timeout_as_retry'] = enabled
            saved['post_submit_treat_verify_timeout_as_retry'] = enabled

        if 'stop_on_none_stage_without_refill' in data:
            val = data['stop_on_none_stage_without_refill']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['stop_on_none_stage_without_refill'] = enabled
            saved['stop_on_none_stage_without_refill'] = enabled

        if 'batch_retry_times' in data:
            try:
                val = int(data['batch_retry_times'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('batch_retry_times', 2))
            val = max(0, min(5, val))
            CONFIG['batch_retry_times'] = val
            saved['batch_retry_times'] = val

        if 'submit_batch_size' in data:
            try:
                val = int(data['submit_batch_size'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('submit_batch_size', 3))
            val = max(1, min(9, val))
            CONFIG['submit_batch_size'] = val
            saved['submit_batch_size'] = val

        if 'initial_submit_batch_size' in data:
            try:
                val = int(data['initial_submit_batch_size'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('initial_submit_batch_size', CONFIG.get('submit_batch_size', 3)))
            val = max(1, min(9, val))
            CONFIG['initial_submit_batch_size'] = val
            saved['initial_submit_batch_size'] = val

        if 'order_query_max_pages' in data:
            try:
                val = int(data['order_query_max_pages'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('order_query_max_pages', 2))
            val = max(1, min(10, val))
            CONFIG['order_query_max_pages'] = val
            saved['order_query_max_pages'] = val

        if 'submit_split_retry_times' in data:
            try:
                val = int(data['submit_split_retry_times'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('submit_split_retry_times', 1))
            val = max(0, min(3, val))
            CONFIG['submit_split_retry_times'] = val
            saved['submit_split_retry_times'] = val

        if 'health_check_start_time' in data:
            time_str = normalize_time_str(data['health_check_start_time'])
            if time_str:
                CONFIG['health_check_start_time'] = time_str
                saved['health_check_start_time'] = time_str

        # 3) 健康检查开关（勾选 / 取消）
        if 'preselect_enabled' in data:
            val = data['preselect_enabled']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['preselect_enabled'] = enabled
            saved['preselect_enabled'] = enabled

        if 'preselect_only_before_first_submit' in data:
            val = data['preselect_only_before_first_submit']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['preselect_only_before_first_submit'] = enabled
            saved['preselect_only_before_first_submit'] = enabled

        if 'health_check_enabled' in data:
            val = data['health_check_enabled']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['health_check_enabled'] = enabled
            saved['health_check_enabled'] = enabled

        # 3.1) 高频调试日志开关
        if 'verbose_logs' in data:
            val = data['verbose_logs']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['verbose_logs'] = enabled
            saved['verbose_logs'] = enabled

        # 3.2) 同时段预检上限（<=0 表示关闭）
        if 'same_time_precheck_limit' in data:
            try:
                val = int(data.get('same_time_precheck_limit'))
            except (TypeError, ValueError):
                val = int(CONFIG.get('same_time_precheck_limit', 0))
            val = max(0, min(9, val))
            CONFIG['same_time_precheck_limit'] = val
            saved['same_time_precheck_limit'] = val

        # 4) 写回 config.json
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(saved, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入配置文件失败: {e}")
            # 即使写文件失败，内存中的 CONFIG 已经更新了

        # 5) 重新安排健康检查（应用新的开关/间隔）
        schedule_health_check()

        return jsonify({"status": "success"})

    except Exception as e:
        print(f"更新配置时异常: {e}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/config/auth', methods=['POST'])
def update_auth():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "msg": "请求体为空"})
            
        token = str(data.get('token') or '').strip()
        if not token:
            return jsonify({"status": "error", "msg": "Token缺失"})

        cookie_raw = data.get('cookie', None)
        cookie = str(cookie_raw).strip() if cookie_raw is not None else ''
        has_cookie_update = bool(cookie)

        CONFIG['auth']['token'] = token
        if has_cookie_update:
            CONFIG['auth']['cookie'] = cookie

        # 更新 client 实例
        client.token = token
        if has_cookie_update:
            client.headers['Cookie'] = cookie
            
            # 持久化保存
            try:
                saved = {}
                if os.path.exists(CONFIG_FILE):
                    try:
                        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                            saved = json.load(f)
                    except: pass
                
                # 确保 auth 结构存在
                if 'auth' not in saved: saved['auth'] = {}
                
                saved['auth']['token'] = token
                if has_cookie_update:
                    saved['auth']['cookie'] = cookie
                else:
                    saved['auth']['cookie'] = CONFIG['auth'].get('cookie', '')
                # 保留其他 auth 字段 (如 shop_num)
                saved['auth']['card_index'] = CONFIG['auth'].get('card_index', '')
                saved['auth']['card_st_id'] = CONFIG['auth'].get('card_st_id', '')
                saved['auth']['shop_num'] = CONFIG['auth'].get('shop_num', '')

                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(saved, f, ensure_ascii=False, indent=2)
                    
            except Exception as e:
                print(f"保存Auth配置失败: {e}")
                # 即使保存失败，内存更新成功也算成功，但记录日志

            if has_cookie_update:
                msg = "Token/Cookie 已更新"
            else:
                msg = "Token 已更新，Cookie 保持原值"
            return jsonify({"status": "success", "msg": msg})
    except Exception as e:
        print(f"Update Auth Error: {e}")
        return jsonify({"status": "error", "msg": f"服务器内部错误: {str(e)}"})

@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    return jsonify(task_manager.tasks)

@app.route('/api/tasks', methods=['POST'])
def add_task():
    data = request.json
    task_manager.add_task(data)
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
def del_task(task_id):
    task_manager.delete_task(task_id)
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>', methods=['PUT'])
def update_task(task_id):
    data = request.json or {}
    ok = task_manager.update_task(task_id, data)
    if not ok:
        return jsonify({"status": "error", "msg": "Task not found"}), 404
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>/run', methods=['POST'])
def run_task_now(task_id):
    # Find task
    task = next((t for t in task_manager.tasks if str(t['id']) == str(task_id)), None)
    if task:
        if task_manager.is_task_running(task_id):
            return jsonify({"status": "error", "msg": "任务仍在执行中，本次触发已跳过"}), 409
        # Run in a separate thread to avoid blocking the response
        threading.Thread(target=task_manager.execute_task_with_lock, args=(task,)).start()
        return jsonify({"status": "success", "msg": "Task started"})
    return jsonify({"status": "error", "msg": "Task not found"}), 404



@app.route('/api/state-sampler', methods=['GET'])
def api_state_sampler():
    snap = STATE_SAMPLER.snapshot()
    return jsonify({
        'status': 'success',
        'seconds': snap.get('seconds', 0),
        'states': snap.get('states', {}),
        'recommended_locked_states': snap.get('recommended_locked_states', []),
        'current_locked_state_values': CONFIG.get('locked_state_values', []),
    })


@app.route('/api/refill-tasks', methods=['GET'])
def get_refill_tasks():
    return jsonify(task_manager.refill_tasks)


@app.route('/api/refill-tasks', methods=['POST'])
def add_refill_task_api():
    data = request.json or {}
    task = task_manager.add_refill_task(data)
    return jsonify({'status': 'success', 'task': task})




@app.route('/api/refill-tasks/<task_id>', methods=['PUT'])
def update_refill_task_api(task_id):
    data = request.json or {}
    task = task_manager.update_refill_task(task_id, data)
    if not task:
        return jsonify({'status': 'error', 'msg': 'Refill task not found'}), 404
    return jsonify({'status': 'success', 'task': task})

@app.route('/api/refill-tasks/<task_id>', methods=['DELETE'])
def del_refill_task_api(task_id):
    task_manager.delete_refill_task(task_id)
    return jsonify({'status': 'success'})


@app.route('/api/refill-tasks/<task_id>/run', methods=['POST'])
def run_refill_task_now(task_id):
    task = next((t for t in task_manager.refill_tasks if str(t.get('id')) == str(task_id)), None)
    if not task:
        return jsonify({'status': 'error', 'msg': 'Refill task not found'}), 404

    task['last_result'] = {'status': 'running', 'msg': '手动执行中(1轮)'}
    task_manager.save_refill_tasks()

    def _run():
        try:
            res = task_manager._run_refill_task_once(task, source='manual')
            task['last_run_at'] = int(time.time() * 1000)
            task['last_result'] = res
            task_manager.append_refill_history(task, res)
            task_manager.save_refill_tasks()
        except Exception as e:
            task['last_run_at'] = int(time.time() * 1000)
            task['last_result'] = {'status': 'error', 'msg': str(e)}
            task_manager.append_refill_history(task, task['last_result'])
            task_manager.save_refill_tasks()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'success', 'msg': 'Refill task one-shot started'})

@app.route('/api/config/check-token', methods=['POST'])
def check_token_api():
    query_ok, query_msg = client.check_token()
    booking_probe = client.check_booking_auth_probe()

    if query_ok:
        status = "success"
        msg = "查询链路鉴权通过。"
    else:
        status = "error"
        msg = f"查询链路鉴权失败: {query_msg}"
        # 如果失效，尝试发短信提醒（如果配置了手机号）
        task_manager.send_notification(f"警告：您的 Token 可能已失效 ({query_msg})，请及时更新喵！")

    return jsonify({
        "status": status,
        "msg": msg,
        "query_auth_ok": query_ok,
        "query_auth_msg": query_msg,
        "booking_auth_ok": booking_probe.get('ok', False),
        "booking_auth_unknown": booking_probe.get('unknown', True),
        "booking_auth_msg": booking_probe.get('msg', ''),
    })

@app.route('/api/config/test-sms', methods=['POST'])
def test_sms():
    data = request.json
    phones = data.get('phones', [])
    if not phones: return jsonify({"status": "error", "msg": "请输入手机号喵"})
    
    # 临时覆盖配置以测试
    original_phones = CONFIG.get('notification_phones', [])
    CONFIG['notification_phones'] = phones
    
    try:
        # 尝试发送
        success, msg = task_manager.send_notification("这是一条测试短信，收到代表配置成功喵！")
        if success:
            return jsonify({"status": "success", "msg": "接口调用成功(返回码0)，请留意手机短信喵"})
        else:
            return jsonify({"status": "error", "msg": f"发送失败: {msg} 喵"})
    except Exception as e:
        print(f"测试接口异常: {e}")
        return jsonify({"status": "error", "msg": f"服务端异常: {str(e)}"})
    finally:
        # 恢复配置
        CONFIG['notification_phones'] = original_phones



@app.route('/<path:path_like>')
def page_route_fallback(path_like):
    # reverse proxy / sub-path compatibility: support /xxx/tasks or /xxx/settings
    normalized = (path_like or '').strip('/')
    if not normalized:
        return render_main_page('semi')

    # keep API/static 404 behavior
    if normalized.startswith('api/') or normalized.startswith('static/'):
        return jsonify({"status": "error", "msg": "Not Found"}), 404

    last = normalized.split('/')[-1]
    if last in ('tasks', 'settings'):
        return render_main_page(last)
    if last in ('', 'index', 'semi'):
        return render_main_page('semi')

    return jsonify({"status": "error", "msg": "Not Found"}), 404

@app.route('/api/logs', methods=['GET'])
def get_logs():
    refill_id = (request.args.get('refill_id') or '').strip()
    status_kw = (request.args.get('status_kw') or '').strip().lower()
    try:
        window_min = max(0, int(float(request.args.get('window_min', 0) or 0)))
    except Exception:
        window_min = 0

    logs = list(LOG_BUFFER)
    if window_min > 0:
        now_dt = datetime.now()
        cutoff = now_dt - timedelta(minutes=window_min)
        filtered = []
        for line in logs:
            try:
                if len(line) >= 10 and line[0] == '[' and line[9] == ']':
                    t = datetime.strptime(line[1:9], '%H:%M:%S')
                    cur = now_dt.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
                    if cur > now_dt:
                        cur = cur - timedelta(days=1)
                    if cur >= cutoff:
                        filtered.append(line)
                else:
                    filtered.append(line)
            except Exception:
                filtered.append(line)
        logs = filtered
    if refill_id:
        key = f"[refill#{refill_id}|"
        logs = [line for line in logs if key in line]
    if status_kw:
        logs = [line for line in logs if status_kw in str(line).lower()]
    return jsonify(logs)


@app.route('/api/run-metrics', methods=['GET'])
def get_run_metrics():
    task_id = request.args.get('task_id')
    unlock_only = str(request.args.get('unlock_only', '1')).lower() in ('1', 'true', 'yes', 'on')
    limit = request.args.get('limit', default=50, type=int)
    limit = max(1, min(500, int(limit or 50)))
    records = []
    if os.path.exists(TASK_RUN_METRICS_FILE):
        try:
            with open(TASK_RUN_METRICS_FILE, 'r', encoding='utf-8') as f:
                records = json.load(f) or []
        except Exception:
            records = []
    if not isinstance(records, list):
        records = []
    if task_id:
        records = [r for r in records if str(r.get('task_id')) == str(task_id)]
    if unlock_only:
        records = [
            r for r in records
            if bool(r.get('saw_locked')) and bool(r.get('unlocked_after_locked'))
        ]
    records = records[-limit:]

    success_within_60 = [r for r in records if r.get('success_within_60s') is True]
    first_success_samples = sorted(int(r.get('first_success_ms')) for r in records if r.get('first_success_ms') is not None)
    submit_p95_samples = sorted(int(r.get('submit_latency_p95_ms')) for r in records if r.get('submit_latency_p95_ms') is not None)
    summary = {
        'total_runs': len(records),
        'unlock_only': unlock_only,
        'focus_scope': 'locked_to_unlocked_only' if unlock_only else 'all_runs',
        'success_within_60_rate': round(len(success_within_60) / len(records), 4) if records else None,
        'first_success_p50_ms': int(_percentile(first_success_samples, 0.5)) if first_success_samples else None,
        'first_success_p95_ms': int(_percentile(first_success_samples, 0.95)) if first_success_samples else None,
        'submit_p95_p50_ms': int(_percentile(submit_p95_samples, 0.5)) if submit_p95_samples else None,
        'goal_achieved_rate': round(sum(1 for r in records if bool(r.get('goal_achieved'))) / len(records), 4) if records else None,
    }

    recommendation = {
        'profile': 'balanced',
        'reason': '数据不足，先用平衡档持续采样',
    }
    rate = summary.get('success_within_60_rate')
    first_p95 = summary.get('first_success_p95_ms')
    submit_p95_med = summary.get('submit_p95_p50_ms')
    min_sample_size = 12
    confidence = 'low'
    if records:
        if len(records) >= min_sample_size and rate is not None and first_p95 is not None and submit_p95_med is not None:
            confidence = 'high'
            if rate >= 0.6 and first_p95 <= 12000 and submit_p95_med <= 2500:
                recommendation = {'profile': 'stable', 'reason': '命中率高且时延稳定，建议稳健档降低风控风险'}
            elif rate < 0.35 or first_p95 > 25000 or submit_p95_med > 4500:
                recommendation = {'profile': 'aggressive', 'reason': '60秒命中率偏低或时延偏高，建议激进档提升前60秒命中'}
            else:
                recommendation = {'profile': 'balanced', 'reason': '命中率与时延居中，建议平衡档持续观察'}
        else:
            recommendation = {'profile': 'balanced', 'reason': f'样本不足（{len(records)}/{min_sample_size}），先保持平衡档并继续采样'}
    recommendation['confidence'] = confidence
    recommendation['sample_size'] = len(records)
    recommendation['min_sample_size'] = min_sample_size
    return jsonify({'summary': summary, 'recommendation': recommendation, 'records': records})

if __name__ == "__main__":
    validate_templates_on_startup()
    smoke_render_pages_on_startup()

    # 首次启动刷新调度
    task_manager.refresh_schedule()

    # 启动健康检查调度（如果启用）
    schedule_health_check()

    print("🚀 服务已启动，访问 http://127.0.0.1:5000")
    print("📋 已加载测试接口: /api/config/test-sms")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)  # 关闭 reloader 防止线程重复启动
