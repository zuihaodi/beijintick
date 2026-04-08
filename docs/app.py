"""
变更记录（手动维护）:
- 2026-03-21 delivery_target_blocks 取消全局/profile 默认；由任务 config 显式值或主组 items 推导
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
import copy
from contextlib import contextmanager
from requests.adapters import HTTPAdapter


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
    with runtime_request_context("health_check", owner=False):
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
                content = f"⚠️ 健康检查失败：获取场地状态异常({err_msg})"
                task_manager.send_wechat_notification(
                    content,
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
            quiet_info = quiet_window_block_info("health_check", owner_allowed=False)
            if quiet_info:
                log(f"🔇 [health_check] 已跳过：{quiet_info.get('msg')}")
                HEALTH_CHECK_NEXT_RUN = HEALTH_CHECK_NEXT_RUN + timedelta(minutes=check_interval)
                return
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
    "submit_strategy_mode": "adaptive",
    "submit_adaptive_target_batches": 2,
    "submit_adaptive_min_batch_size": 1,
    "submit_adaptive_max_batch_size": 3,
    "submit_adaptive_merge_small_n": 2,
    "submit_adaptive_merge_same_time_only": True,
    "submit_grouping_mode": "smart",
    "submit_timeout_seconds": 4.0,
    "submit_split_retry_times": 1,
    "submit_timeout_backoff_seconds": 2.5,  # 提交超时后重试前的退避(秒)，避免紧接重试触发「操作过快」
    "batch_min_interval": 0.8,
    "fast_lane_enabled": True,
    "fast_lane_seconds": 2.0,
    "order_query_timeout_seconds": 2.5,
    "order_query_max_pages": 2,
    "post_submit_orders_join_timeout_seconds": 0.3,
    "post_submit_verify_matrix_timeout_seconds": 1.5,  # 提交后验证放宽，高峰时避免误判成功为失败
    "post_submit_verify_matrix_recheck_times": 5,
    "post_submit_verify_orders_on_matrix_partial_only": True,
    "post_submit_skip_sync_orders_query": True,
    "post_submit_orders_sync_fallback": False,
    "post_submit_verify_pending_retry_seconds": 0.35,
    "post_submit_verify_pending_matrix_recheck_times": 4,
    "manual_verify_pending_recheck_times": 3,
    "manual_verify_pending_retry_seconds": 0.25,
    "manual_verify_pending_orders_fallback_enabled": True,
    "too_fast_skip_refill_in_same_request": True,
    "multi_item_retry_balance_enabled": True,
    "multi_item_batch_retry_times_cap": 1,
    "multi_item_retry_total_budget": 3,
    "post_submit_treat_verify_timeout_as_retry": True,
    "refill_window_seconds": 8.0,
    "locked_state_values": [2, 3, 5, 6],  # 接口 state 落在这些值时视为“锁定/暂不可下单”
    "matrix_timeout_seconds": 3.0,  # 高峰查询超时(秒)，建议短超时+高频重试
    # 🔍 新增：凭证健康检查
    "health_check_enabled": True,      # 是否开启自动健康检查
    "health_check_interval_min": 30.0, # 检查间隔（分钟）
    "health_check_start_time": "00:00", # 起始时间 (HH:MM)
    "verbose_logs": False,  # 是否打印高频调试日志
    "log_to_file": True,  # 是否将运行日志按天写入文件，便于次日查看
    "log_file_dir": "logs",  # 日志文件目录，相对工作目录
    "log_retention_days": 3,  # 日志文件保留最近 N 天，超过自动删除，0=不清理
    "transient_storm_threshold": 8,  # 连续 N 次异常后才退避，避免过早退避导致首矩阵过晚
    "transient_storm_backoff_seconds": 1.5,  # 退避缩短，减少黄金窗口浪费
    "matrix_timeout_storm_seconds": 8.0,  # 风暴期用更长超时，争取在高峰仍拿到矩阵
    "transient_storm_extend_timeout_after": 3,  # 连续失败 >= 此数时使用 matrix_timeout_storm_seconds
    "metrics_keep_last": 300,  # 统一观测文件最大保留条数
    "metrics_retention_days": 7,  # 统一观测文件保留天数
    "same_time_precheck_limit": 0,  # 同时段预检上限；<=0 表示关闭预检
    "preselect_enabled": True,
    "preselect_ttl_seconds": 2.0,
    "preselect_only_before_first_submit": True,
    "minimal_pre_submit_matrix_once": False,
    "delivery_burst_workers": 1,
    "delivery_rate_limit_workers": 1,
    "delivery_transport_sustain_workers": 2,
    "delivery_burst_window_seconds": 1.0,
    "delivery_transport_round_interval_seconds": 0.25,
    "delivery_rate_limit_backoff_seconds": 1.7,
    "delivery_total_budget_seconds": 600.0,
    "delivery_min_post_interval_seconds": 2.2,
    "delivery_backup_switch_delay_seconds": 2.0,
    "delivery_warmup_max_retries": 200,
    "delivery_warmup_budget_seconds": 300.0,
    "delivery_first_group_from_matrix": False,
    "delivery_preferred_place_min": 0,
    "delivery_preferred_place_max": 0,
    "delivery_target_times": ["20:00", "21:00"],
    "delivery_time_preference_order": ["20:00", "21:00"],
    "manual_submit_profile": "manual_minimal",
    "auto_submit_profile": "auto_minimal",
    "submit_profiles": {
        "auto_minimal": {
            "minimal_direct_mode": True,
            "delivery_burst_workers": 1,
            "delivery_rate_limit_workers": 1,
            "delivery_transport_sustain_workers": 2,
            "delivery_burst_window_seconds": 1.0,
            "delivery_transport_round_interval_seconds": 0.25,
            "delivery_rate_limit_backoff_seconds": 1.7,
            "delivery_total_budget_seconds": 600.0,
            "delivery_min_post_interval_seconds": 2.2,
            "delivery_backup_switch_delay_seconds": 2.0,
            "delivery_warmup_max_retries": 200,
            "delivery_warmup_budget_seconds": 300.0,
            "delivery_first_group_from_matrix": False,
            "delivery_target_times": ["20:00", "21:00"],
            "delivery_time_preference_order": ["20:00", "21:00"],
        },
        "manual_minimal": {
            "minimal_direct_mode": True,
            "delivery_burst_workers": 1,
            "delivery_rate_limit_workers": 1,
            "delivery_transport_sustain_workers": 1,
            "delivery_burst_window_seconds": 1.0,
            "delivery_transport_round_interval_seconds": 0.35,
            "delivery_rate_limit_backoff_seconds": 1.8,
            "delivery_total_budget_seconds": 600.0,
            "delivery_min_post_interval_seconds": 2.2,
            "delivery_backup_switch_delay_seconds": 2.0,
            "delivery_warmup_max_retries": 200,
            "delivery_warmup_budget_seconds": 8.0,
            "delivery_first_group_from_matrix": False,
            "delivery_target_times": ["20:00", "21:00"],
            "delivery_time_preference_order": ["20:00", "21:00"],
        },
        "auto_high_concurrency": {
            "fast_lane_enabled": True,
            "fast_lane_seconds": 2.0,
            "batch_min_interval": 0.8,
            "too_fast_skip_refill_in_same_request": True,
            "multi_item_batch_retry_times_cap": 1,
        },
        "manual_stable": {
            "fast_lane_enabled": False,
            "fast_lane_seconds": 0.0,
            "batch_min_interval": 1.1,
            "too_fast_skip_refill_in_same_request": False,
            "too_fast_cooldown_seconds": 2.2,
            "too_fast_force_single_item_on_batch_fail": True,
            "multi_item_batch_retry_times_cap": 2,
            "submit_grouping_mode": "place",
            "submit_strategy_mode": "adaptive",
        },
    },
}


# 执行参数上下限（唯一定义处）：总时长 1800 秒，拉活总时长 900 秒，拉活重试 300 次；超出时限制并提示用户
EXEC_PARAM_LIMITS = {
    "delivery_warmup_max_retries": (1, 9900),
    "delivery_total_budget_seconds": (3.0, 9900.0),
    "delivery_warmup_budget_seconds": (1.0, 9900.0),
    "delivery_min_post_interval_seconds": (0.0, 120.0),
}


def _clamp_exec_param(key, raw_value, default):
    """执行参数限制：返回 (最终值, 是否被限制)。仅处理 EXEC_PARAM_LIMITS 中的 key。"""
    if key not in EXEC_PARAM_LIMITS:
        return (default, False)
    lo, hi = EXEC_PARAM_LIMITS[key]
    try:
        val = int(raw_value) if isinstance(lo, int) else float(raw_value)
    except (TypeError, ValueError):
        return (default, False)
    clamped = max(lo, min(hi, val))
    return (clamped, clamped != val)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_TEMPLATE_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_FILE = CONFIG_TEMPLATE_FILE
CONFIG_SECRET_FILE = os.path.join(BASE_DIR, "config.secret.json")
# 敏感配置：仅从 config.secret.json 读写，不写入 config.json，便于与执行参数分离
SENSITIVE_TOP_LEVEL_KEYS = frozenset({"auth", "notification_phones", "pushplus_tokens", "sms"})
# 已从 CONFIG 默认与 execution API 移除；磁盘上旧 config.json 可能仍含这些键，加载时不再合并进 CONFIG
DEPRECATED_EXEC_PARAM_KEYS = frozenset({
    "pipeline_continuous_window_seconds",
    "pipeline_random_window_seconds",
    "pipeline_refill_interval_seconds",
    "pipeline_stop_when_reached",
    "pipeline_continuous_prefer_adjacent",
    "pipeline_greedy_end_mode",
    "pipeline_greedy_end_before_hours",
    "pipeline_random_window_extension_after_late_start_seconds",
    "pipeline_late_start_threshold_ms",
    "stop_on_none_stage_without_refill",
    "biz_fail_cooldown_seconds",
    "first_submit_delay_seconds",
    "locked_retry_interval",
    "locked_max_seconds",
    "open_retry_seconds",
})
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


def append_task_run_metric(record, keep_last=None, retention_days=None):
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

            keep_last_val = max(50, min(5000, int(keep_last if keep_last is not None else CONFIG.get('metrics_keep_last', 300) or 300)))
            retention_days_val = max(1, min(30, int(retention_days if retention_days is not None else CONFIG.get('metrics_retention_days', 7) or 7)))
            cutoff_ms = int((time.time() - retention_days_val * 24 * 3600) * 1000)

            def _ts_ms(rec):
                if not isinstance(rec, dict):
                    return 0
                for k in ('finished_at', 'started_at', 'ts'):
                    v = rec.get(k)
                    if v is None:
                        continue
                    try:
                        return int(v)
                    except Exception:
                        continue
                return 0

            old = [r for r in old if _ts_ms(r) >= cutoff_ms]
            old = old[-keep_last_val:]
            with open(TASK_RUN_METRICS_FILE, 'w', encoding='utf-8') as f:
                json.dump(old, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 任务指标写入失败: {e}")

def log(msg):
    """记录日志到内存缓冲区、控制台，可选按天落盘便于次日查看"""
    print(msg)
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    LOG_BUFFER.append(line)
    if len(LOG_BUFFER) > MAX_LOG_SIZE:
        LOG_BUFFER.pop(0)
    # 按天落盘，明天仍可查看今天的运行日志；可选保留最近 N 天
    if CONFIG.get("log_to_file"):
        try:
            log_dir = (CONFIG.get("log_file_dir") or "logs").strip() or "logs"
            if not os.path.isabs(log_dir):
                log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_dir)
            os.makedirs(log_dir, exist_ok=True)
            today_str = datetime.now().strftime("%Y%m%d")
            log_file = os.path.join(log_dir, f"run_{today_str}.log")
            # 每天首次写日志时清理超过保留期的旧日志（避免每次 log 都扫目录）
            retention_days = int(CONFIG.get("log_retention_days", 3) or 0)
            if retention_days > 0 and getattr(log, "_last_purge_date", None) != today_str:
                try:
                    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y%m%d")
                    for name in os.listdir(log_dir):
                        if name.startswith("run_") and name.endswith(".log") and len(name) == 15:
                            date_str = name[4:12]
                            if date_str < cutoff:
                                p = os.path.join(log_dir, name)
                                if os.path.isfile(p):
                                    os.remove(p)
                    log._last_purge_date = today_str
                except Exception:
                    pass
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            builtins.print(f"⚠️ 写日志文件失败: {e}")


def is_verbose_logs_enabled():
    return bool(CONFIG.get("verbose_logs", False))


def cfg_get(key, default=None):
    return CONFIG.get(key, default)


def get_submit_profile_settings(profile_name):
    profiles = CONFIG.get("submit_profiles")
    if not isinstance(profiles, dict):
        return {}
    key = str(profile_name or "").strip()
    if not key:
        return {}
    profile = profiles.get(key)
    return dict(profile) if isinstance(profile, dict) else {}


def normalize_booking_items(items):
    normalized = []
    seen = set()
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        place = str(raw.get("place") or "").strip()
        time_str = str(raw.get("time") or "").strip()
        if not place or not re.fullmatch(r"\d+", place):
            continue
        if not re.fullmatch(r"\d{2}:\d{2}", time_str):
            continue
        key = (place, time_str)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"place": place, "time": time_str})
    return normalized


_MINE_MATRIX_STATUSES = ("mine", "self", "my_booked", "mybooked", "booked_by_me")


def _matrix_cell_is_mine(st):
    return isinstance(st, str) and st.lower() in _MINE_MATRIX_STATUSES


def collect_mine_items_from_matrix(matrix_live, places_list, target_times):
    """从余票矩阵中收集「本人已占」格子，用于通知与 success_items（与递送器内缺口计数语义一致）。"""
    raw = []
    for t in target_times:
        for p in places_list:
            cell = (matrix_live.get(str(p)) or {}).get(t)
            if _matrix_cell_is_mine(cell):
                raw.append({"place": str(p), "time": t})
    return normalize_booking_items(raw)


def notify_items_from_submit_result(res, fallback_items):
    """提交结果里 success_items 为 [] 时表示「无场次列表」，不得回退为配置主组/旧 items。"""
    if not isinstance(res, dict):
        return fallback_items
    si = res.get("success_items")
    if si is None:
        return fallback_items
    if isinstance(si, list) and len(si) == 0:
        return []
    return si


def summarize_booking_items(items):
    normalized = normalize_booking_items(items)
    places = sorted(
        {str(it.get("place")) for it in normalized},
        key=lambda x: int(x) if str(x).isdigit() else 999,
    )
    times = sorted({str(it.get("time")) for it in normalized})
    return {
        "items": normalized,
        "places": places,
        "times": times,
        "place_count": len(places),
        "slot_count": len(normalized),
    }


def is_direct_task_config(config):
    if not isinstance(config, dict):
        return False
    mode = str(config.get("mode") or "").strip().lower()
    return mode in ("direct", "minimal")


def get_direct_task_items(config):
    if not is_direct_task_config(config):
        return []
    return normalize_booking_items(config.get("direct_items") or config.get("items") or [])


def normalize_delivery_groups(groups):
    """
    极速递送仅使用单一主组。将任务里多组（含 legacy 备用1/2）按顺序合并去重为一条 primary，
    避免旧配置静默丢项；后续若再支持多组策略，可在此集中改合并规则。
    """
    parsed = []
    for idx, raw in enumerate(groups or []):
        if not isinstance(raw, dict):
            continue
        group_id = str(raw.get("id") or f"group_{idx + 1}").strip() or f"group_{idx + 1}"
        label = str(raw.get("label") or group_id).strip() or group_id
        items = normalize_booking_items(raw.get("items") or [])
        if not items:
            continue
        parsed.append({"id": group_id, "label": label, "items": items})
    if not parsed:
        return []

    def _prio(gid):
        order = {"primary": 0, "backup_1": 1, "backup_2": 2}
        return (order.get(gid, 50), str(gid))

    parsed.sort(key=lambda g: _prio(g.get("id") or ""))
    seen_pairs = set()
    merged_items = []
    primary_label = "主组合"
    for g in parsed:
        gid = g.get("id") or ""
        if gid == "primary":
            primary_label = str(g.get("label") or primary_label)
        for it in g.get("items") or []:
            k = (str(it.get("place")), str(it.get("time")))
            if k in seen_pairs:
                continue
            seen_pairs.add(k)
            merged_items.append(it)
    if not merged_items:
        return []
    return [{"id": "primary", "label": primary_label, "items": merged_items}]


def get_delivery_groups(config):
    if not is_direct_task_config(config):
        return []
    delivery_groups = normalize_delivery_groups(config.get("delivery_groups") or [])
    if delivery_groups:
        return delivery_groups
    direct_items = get_direct_task_items(config)
    if not direct_items:
        return []
    return [{"id": "primary", "label": "主组合", "items": direct_items}]


def _build_downgrade_levels(target_blocks, target_times, time_preference_order):
    """
    根据目标块数、目标时段、时间偏好顺序，生成降级级别列表。
    每个级别为 { "20:00": 2, "21:00": 1 } 形式的时段->块数映射。
    时间偏好：time_preference_order 中越靠前越优先保留；降级时先减靠后的时段。
    """
    if not target_times or not time_preference_order:
        if target_times and target_blocks:
            return [{t: target_blocks for t in target_times}]
        return []
    order = [t for t in time_preference_order if t in target_times]
    if not order:
        order = list(target_times)
    levels = []
    levels.append({t: target_blocks for t in order})
    current = dict(levels[0])
    while current:
        reduced = False
        for t in reversed(order):
            if t not in current:
                continue
            k = current[t]
            if k <= 1:
                cur_copy = dict(current)
                del cur_copy[t]
                if cur_copy and cur_copy not in levels:
                    levels.append(cur_copy)
                    current = cur_copy
                    reduced = True
                break
            else:
                cur_copy = dict(current)
                cur_copy[t] = k - 1
                if cur_copy not in levels:
                    levels.append(cur_copy)
                    current = cur_copy
                    reduced = True
                break
        if not reduced:
            break
    if order and {order[0]: 1} not in levels:
        levels.append({order[0]: 1})
    for t in reversed(order):
        spec_2 = {t: target_blocks}
        if spec_2 not in levels:
            levels.append(spec_2)
        spec_1 = {t: 1}
        if spec_1 not in levels:
            levels.append(spec_1)
    return levels


def compute_first_group_from_matrix(matrix, places, times, config_or_dict):
    """
    从拉活得到的 matrix 计算第一组 group_items（偏好场地 + 块数×时间降级）。
    1-17 号；仅 matrix[p][t]=="available" 可订。
    返回 (group_items, level_index) 或 (None, None)。
    """
    if not matrix or not isinstance(config_or_dict, dict):
        return None, None
    cfg = config_or_dict
    raw_tb = cfg.get("delivery_target_blocks")
    if raw_tb is None:
        return None, None
    try:
        target_blocks = max(1, min(6, int(raw_tb)))
    except (TypeError, ValueError):
        return None, None
    raw_first_times = cfg.get("delivery_first_group_times") or []
    target_times = [str(t).strip() for t in raw_first_times if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())]
    if not target_times:
        return None, None
    raw_first_pref = cfg.get("delivery_first_group_time_preference_order") or []
    time_preference_order = [str(t).strip() for t in raw_first_pref if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())]
    if not time_preference_order:
        time_preference_order = list(target_times)

    intent = {
        "target_blocks": target_blocks,
        "target_times": target_times,
        "time_preference_order": time_preference_order,
        "preferred_place_min": int(cfg.get("delivery_preferred_place_min") or 0),
        "preferred_place_max": int(cfg.get("delivery_preferred_place_max") or 0),
        "require_consecutive": True,
    }
    solved = solve_candidate_from_matrix(
        matrix,
        places or matrix.keys(),
        intent,
        mode="aggressive",
    )
    if not solved:
        return None, None
    return solved.get("items"), solved.get("level_index")


def solve_candidate_from_matrix(matrix, places, intent, mode="strict", state=None):
    """
    统一候选求解器（主任务 refill / 独立 refill 可复用）。
    mode:
      - strict: 仅接受完整目标，不做降级
      - aggressive: 允许按 _build_downgrade_levels 降级
    返回:
      {"items": [...], "level_index": int, "level_spec": {...}, "score": float} 或 None
    """
    if not isinstance(matrix, dict) or not matrix:
        return None
    cfg = dict(intent or {})
    target_blocks = max(1, min(6, int(cfg.get("target_blocks") or 1)))
    raw_target_times = cfg.get("target_times") or []
    target_times = [str(t).strip() for t in raw_target_times if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())]
    if not target_times:
        return None
    raw_pref = cfg.get("time_preference_order") or []
    time_preference_order = [str(t).strip() for t in raw_pref if t in target_times]
    if not time_preference_order:
        time_preference_order = list(target_times)
    prefer_min = int(cfg.get("preferred_place_min") or 0)
    prefer_max = int(cfg.get("preferred_place_max") or 0)
    require_consecutive = bool(cfg.get("require_consecutive", True))
    need_by_time = cfg.get("need_by_time") if isinstance(cfg.get("need_by_time"), dict) else None
    use_prefer = prefer_min > 0 and prefer_max >= prefer_min

    selectable_places = sorted(
        [str(p) for p in (places or matrix.keys()) if str(p).isdigit() and 1 <= int(str(p)) <= 17 and str(p) in matrix],
        key=lambda x: int(x),
    )
    if not selectable_places:
        return None

    if need_by_time:
        level_specs = [{t: max(0, int(need_by_time.get(t) or 0)) for t in time_preference_order}]
        level_specs = [{t: k for t, k in spec.items() if k > 0} for spec in level_specs]
        level_specs = [spec for spec in level_specs if spec]
    elif mode == "aggressive":
        level_specs = _build_downgrade_levels(target_blocks, target_times, time_preference_order)
    else:
        level_specs = [{t: target_blocks for t in time_preference_order}]
    if not level_specs:
        return None

    def _score_items(items, level_spec):
        if not items:
            return -1.0
        unique_times = sorted({str(it.get("time")) for it in items})
        unique_places = sorted({str(it.get("place")) for it in items if str(it.get("place")).isdigit()}, key=lambda x: int(x))
        coverage_ratio = float(len(unique_times)) / float(max(1, len(target_times)))
        block_ratio = float(min(level_spec.values())) / float(max(1, target_blocks))
        consecutive_ratio = 1.0
        if unique_places:
            runs = 1
            for a, b in zip(unique_places, unique_places[1:]):
                if int(b) != int(a) + 1:
                    runs += 1
            consecutive_ratio = 1.0 if runs == 1 else max(0.0, 1.0 - (runs - 1) * 0.3)
        preferred_hit = 0
        if use_prefer:
            for p in unique_places:
                n = int(p)
                if prefer_min <= n <= prefer_max:
                    preferred_hit += 1
            preferred_ratio = float(preferred_hit) / float(max(1, len(unique_places)))
        else:
            preferred_ratio = 1.0
        # 覆盖完整度 > 块数完整度 > 连号质量 > 偏好区命中
        return 100.0 * coverage_ratio + 60.0 * block_ratio + 40.0 * consecutive_ratio + 20.0 * preferred_ratio

    best = None
    for level_index, level_spec in enumerate(level_specs):
        if not level_spec:
            continue
        if require_consecutive:
            K = max(level_spec.values())
            if K > len(selectable_places):
                continue
            starts = list(range(len(selectable_places) - K + 1))
            if use_prefer:
                prefer_starts = [
                    i
                    for i in starts
                    if prefer_min <= int(selectable_places[i]) <= prefer_max
                    and prefer_min <= int(selectable_places[i + K - 1]) <= prefer_max
                ]
                starts = prefer_starts + [i for i in starts if i not in prefer_starts]
            for i in starts:
                s_places = selectable_places[i : i + K]
                ok = True
                for t, k in level_spec.items():
                    for j in range(k):
                        if (matrix.get(s_places[j]) or {}).get(t) != "available":
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
                items = []
                for t in time_preference_order:
                    k = int(level_spec.get(t, 0))
                    for j in range(k):
                        items.append({"place": str(s_places[j]), "time": t})
                items = normalize_booking_items(items)
                score = _score_items(items, level_spec)
                cand = {
                    "items": items,
                    "level_index": level_index,
                    "level_spec": dict(level_spec),
                    "score": score,
                }
                if best is None or score > float(best.get("score") or -1):
                    best = cand
        else:
            items = []
            for t in time_preference_order:
                need = int(level_spec.get(t, 0))
                if need <= 0:
                    continue
                avail = [p for p in selectable_places if (matrix.get(p) or {}).get(t) == "available"]
                if use_prefer:
                    avail = sorted(
                        avail,
                        key=lambda p: (
                            0 if prefer_min <= int(p) <= prefer_max else 1,
                            int(p),
                        ),
                    )
                pick_count = min(max(0, need), len(avail))
                if pick_count <= 0:
                    continue
                for p in avail[:pick_count]:
                    items.append({"place": p, "time": t})
            if not items:
                continue
            items = normalize_booking_items(items)
            score = _score_items(items, level_spec)
            cand = {
                "items": items,
                "level_index": level_index,
                "level_spec": dict(level_spec),
                "score": score,
            }
            if best is None or score > float(best.get("score") or -1):
                best = cand
    return best


def solve_refill_need_tiered(matrix, places, intent_base, need_by_time, allow_scatter=True):
    """
    Refill 分层求解：连号满 → 散号满 → 连号部分(step) → 散号部分 → 每时段1块(去重) → 单格扫描。
    供极速订场 refill 与独立 Refill 共用；不修改 solve_candidate_from_matrix 内部。
    返回 (solved_dict|None, used_need_by_time, tier_label)。
    """
    if not isinstance(matrix, dict) or not matrix:
        return None, {}, ""
    if not isinstance(intent_base, dict):
        return None, {}, ""
    need = {}
    for t, v in (need_by_time or {}).items():
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            need[str(t).strip()] = iv
    if not need:
        return None, {}, ""

    places_list = list(places) if places is not None else []

    def _intent(nd, rc):
        return {**intent_base, "need_by_time": dict(nd), "require_consecutive": bool(rc)}

    def _try(nd, rc):
        return solve_candidate_from_matrix(matrix, places_list, _intent(nd, rc), mode="strict")

    def _partial_key(nd):
        return frozenset((str(t), int(k)) for t, k in sorted(nd.items()))

    tried_partial_keys = set()

    # 1 连号满
    solved = _try(need, True)
    if solved and solved.get("items"):
        return solved, dict(need), "连号满"
    # 2 散号满
    if allow_scatter:
        solved = _try(need, False)
        if solved and solved.get("items"):
            return solved, dict(need), "散号满"

    max_need = max(need.values())
    # 3–4 降 step：连号部分 → 散号部分
    for step in range(max_need - 1, 0, -1):
        partial = {str(t): min(int(v), step) for t, v in need.items() if int(v) > 0}
        if not partial:
            continue
        pk = _partial_key(partial)
        if pk in tried_partial_keys:
            continue
        tried_partial_keys.add(pk)
        solved = _try(partial, True)
        if solved and solved.get("items"):
            return solved, partial, f"连号部分 step={step}"
        if allow_scatter:
            solved = _try(partial, False)
            if solved and solved.get("items"):
                return solved, partial, f"散号部分 step={step}"

    # 5 每时段至多 1 块（与 need 不同形且未试过时再试）
    floor_need = {str(t): 1 for t, v in need.items() if int(v) > 0}
    if floor_need and floor_need != need:
        pk = _partial_key(floor_need)
        if pk not in tried_partial_keys:
            tried_partial_keys.add(pk)
            solved = _try(floor_need, True)
            if solved and solved.get("items"):
                return solved, floor_need, "连号每时段1块"
            if allow_scatter:
                solved = _try(floor_need, False)
                if solved and solved.get("items"):
                    return solved, floor_need, "散号每时段1块"

    # 6 单格扫描：时段顺序用 time_preference_order / target_times
    times_order = intent_base.get("time_preference_order") or intent_base.get("target_times") or []
    if not isinstance(times_order, list):
        times_order = []
    times_order = [str(x).strip() for x in times_order if str(x).strip()]
    if not times_order:
        times_order = sorted(need.keys())
    selectable = sorted(
        [str(p) for p in places_list if str(p).isdigit() and 1 <= int(str(p)) <= 17 and str(p) in matrix],
        key=lambda x: int(x),
    )
    for t in times_order:
        if need.get(t, 0) <= 0:
            continue
        for p in selectable:
            if (matrix.get(str(p)) or {}).get(t) == "available":
                fake = {
                    "items": [{"place": str(p), "time": t}],
                    "level_index": 0,
                    "level_spec": {t: 1},
                    "score": 0.0,
                }
                return fake, {t: 1}, "单格扫描"

    return None, {}, ""


def _delivery_target_blocks_from_items(items):
    """
    按主组场次推导 target_blocks：各时段条数取最大值（至少 1）。
    与 is_goal_satisfied 的「每时段至少 target_blocks 块」语义一致。
    """
    normalized = normalize_booking_items(items or [])
    if not normalized:
        return 1
    counts = {}
    for it in normalized:
        t = str(it.get("time") or "").strip()
        if not t:
            continue
        counts[t] = counts.get(t, 0) + 1
    if not counts:
        return 1
    return max(1, max(int(v) for v in counts.values()))


def _delivery_target_blocks_from_task_config(task_config):
    """
    仅当 task_config 显式包含 delivery_target_blocks 且值非 None 时返回整数（1–6），否则返回 None。
    """
    if not isinstance(task_config, dict) or "delivery_target_blocks" not in task_config:
        return None
    v = task_config.get("delivery_target_blocks")
    if v is None:
        return None
    try:
        return max(1, min(6, int(v)))
    except (TypeError, ValueError):
        return None


def is_goal_satisfied(items, intent):
    """判断候选是否满足目标约束（块数×时段完整覆盖）。"""
    cfg = dict(intent or {})
    target_blocks = max(1, int(cfg.get("target_blocks") or 1))
    target_times = [str(t).strip() for t in (cfg.get("target_times") or []) if str(t).strip()]
    require_consecutive = bool(cfg.get("require_consecutive", True))
    normalized = normalize_booking_items(items or [])
    if not target_times:
        return False
    by_time = {t: [] for t in target_times}
    for it in normalized:
        t = str(it.get("time") or "")
        p = str(it.get("place") or "")
        if t in by_time and p:
            by_time[t].append(p)
    for t in target_times:
        ps = sorted({p for p in by_time.get(t, []) if p.isdigit()}, key=lambda x: int(x))
        if len(ps) < target_blocks:
            return False
        if require_consecutive:
            # 需要至少一个长度>=target_blocks 的连续段
            run = 1
            ok = False
            for a, b in zip(ps, ps[1:]):
                if int(b) == int(a) + 1:
                    run += 1
                    if run >= target_blocks:
                        ok = True
                        break
                else:
                    run = 1
            if target_blocks == 1:
                ok = True
            if not ok and len(ps) >= target_blocks:
                # 可能刚好在首个元素起就满足 1 个块
                ok = (target_blocks == 1)
            if not ok:
                return False
    return True


def group_booking_items_into_legal_batches(items, cfg_get, profile_name=None, batch_limits=None):
    """
    将 (place, time) 拆分为合法批次（与 submit_order 内原逻辑一致），便于 submit_order / 极速递送 refill 共用。
    cfg_get: callable(key, default)；batch_limits 可选 dict 覆盖 max_items_per_batch / max_consecutive_slots_per_place / max_places_per_timeslot。
    """
    manual_profile_name = str(CONFIG.get("manual_submit_profile", "manual_minimal") or "manual_minimal")
    pn = str(profile_name or "").strip()
    is_manual_profile = pn == manual_profile_name

    def _cg(key, default=None):
        if isinstance(batch_limits, dict) and key in batch_limits and batch_limits[key] is not None:
            return batch_limits[key]
        return cfg_get(key, default)

    normalized = [
        {"place": str(it.get("place")), "time": str(it.get("time"))}
        for it in (items or [])
        if isinstance(it, dict) and it.get("place") and it.get("time")
    ]
    if not normalized:
        return []

    try:
        max_items_per_batch = int(_cg("max_items_per_batch", 6) or 6)
    except Exception:
        max_items_per_batch = 6
    max_items_per_batch = max(1, min(12, max_items_per_batch))

    try:
        max_consecutive_slots = int(_cg("max_consecutive_slots_per_place", 3) or 3)
    except Exception:
        max_consecutive_slots = 3
    max_consecutive_slots = max(1, min(6, max_consecutive_slots))

    try:
        max_places_per_timeslot = int(_cg("max_places_per_timeslot", 3) or 3)
    except Exception:
        max_places_per_timeslot = 3
    max_places_per_timeslot = max(1, min(6, max_places_per_timeslot))

    by_place = {}
    place_order = []
    for it in normalized:
        p = it["place"]
        t = it["time"]
        if p not in by_place:
            by_place[p] = []
            place_order.append(p)
        by_place[p].append(t)
    for p in by_place:
        uniq = sorted({tt for tt in by_place[p] if tt})
        by_place[p] = uniq

    segments = []

    def _is_consecutive(prev_s, cur_s):
        try:
            prev_dt = datetime.strptime(prev_s, "%H:%M")
            cur_dt = datetime.strptime(cur_s, "%H:%M")
            return (cur_dt - prev_dt) == timedelta(hours=1)
        except Exception:
            return False

    for p in place_order:
        times = by_place.get(p) or []
        if not times:
            continue
        run = [times[0]]
        for prev, cur in zip(times, times[1:]):
            if _is_consecutive(prev, cur) and len(run) < max_consecutive_slots:
                run.append(cur)
            else:
                if run:
                    segments.append({"place": p, "times": list(run)})
                run = [cur]
        if run:
            segments.append({"place": p, "times": list(run)})

    if not segments:
        return [normalized]

    if is_manual_profile:
        ordered_segments = sorted(
            segments,
            key=lambda s: (
                place_order.index(s["place"]) if s["place"] in place_order else 999,
                min(s["times"] or ["23:59"]),
            ),
        )
        batches = []
        for seg in ordered_segments:
            p = seg["place"]
            times = seg.get("times") or []
            if not times:
                continue
            pairs = [{"place": p, "time": t} for t in times]
            if len(pairs) <= max_items_per_batch:
                batches.append(pairs)
            else:
                for i in range(0, len(pairs), max_items_per_batch):
                    batches.append(pairs[i : i + max_items_per_batch])
        return batches

    batches = []

    def can_add_segment(batch_items, seg_pairs):
        if len(batch_items) + len(seg_pairs) > max_items_per_batch:
            return False
        time_counts = {}
        for it in batch_items:
            t = it["time"]
            time_counts[t] = time_counts.get(t, 0) + 1
        for p, t in seg_pairs:
            if time_counts.get(t, 0) + 1 > max_places_per_timeslot:
                return False
            time_counts[t] = time_counts.get(t, 0) + 1
        existing_places = {it["place"] for it in batch_items}
        seg_places = {p for p, _ in seg_pairs}
        if existing_places & seg_places:
            return False
        return True

    ordered_segments = sorted(
        segments,
        key=lambda s: (
            place_order.index(s["place"]) if s["place"] in place_order else 999,
            min(s["times"] or ["23:59"]),
        ),
    )
    for seg in ordered_segments:
        p = seg["place"]
        seg_pairs = [(p, t) for t in seg["times"]]
        placed = False
        for batch in batches:
            if can_add_segment(batch, seg_pairs):
                for bp, bt in seg_pairs:
                    batch.append({"place": bp, "time": bt})
                placed = True
                break
        if not placed:
            batches.append([{"place": bp, "time": bt} for bp, bt in seg_pairs])
    return batches


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
            if 'submit_strategy_mode' in saved:
                mode = str(saved.get('submit_strategy_mode') or 'adaptive').strip().lower()
                CONFIG['submit_strategy_mode'] = mode if mode in ('adaptive', 'fixed') else 'adaptive'
            if 'submit_adaptive_target_batches' in saved:
                try:
                    CONFIG['submit_adaptive_target_batches'] = max(1, min(6, int(saved['submit_adaptive_target_batches'])))
                except Exception:
                    pass
            if 'submit_adaptive_min_batch_size' in saved:
                try:
                    CONFIG['submit_adaptive_min_batch_size'] = max(1, min(9, int(saved['submit_adaptive_min_batch_size'])))
                except Exception:
                    pass
            if 'submit_adaptive_max_batch_size' in saved:
                try:
                    CONFIG['submit_adaptive_max_batch_size'] = max(1, min(9, int(saved['submit_adaptive_max_batch_size'])))
                except Exception:
                    pass
            if 'submit_adaptive_merge_small_n' in saved:
                try:
                    CONFIG['submit_adaptive_merge_small_n'] = max(1, min(9, int(saved['submit_adaptive_merge_small_n'])))
                except Exception:
                    pass
            if 'submit_adaptive_merge_same_time_only' in saved:
                CONFIG['submit_adaptive_merge_same_time_only'] = bool(saved['submit_adaptive_merge_same_time_only'])
            if 'submit_grouping_mode' in saved:
                mode = str(saved.get('submit_grouping_mode') or 'smart').strip().lower()
                CONFIG['submit_grouping_mode'] = mode if mode in ('smart', 'place', 'timeslot') else 'smart'
            if 'manual_submit_profile' in saved:
                CONFIG['manual_submit_profile'] = str(saved.get('manual_submit_profile') or 'manual_minimal').strip() or 'manual_minimal'
            if 'auto_submit_profile' in saved:
                CONFIG['auto_submit_profile'] = str(saved.get('auto_submit_profile') or 'auto_minimal').strip() or 'auto_minimal'
            if 'submit_profiles' in saved and isinstance(saved.get('submit_profiles'), dict):
                merged_profiles = {}
                default_profiles = CONFIG.get('submit_profiles')
                if isinstance(default_profiles, dict):
                    for k, v in default_profiles.items():
                        if isinstance(v, dict):
                            merged_profiles[str(k)] = dict(v)
                for k, v in (saved.get('submit_profiles') or {}).items():
                    key = str(k).strip()
                    if not key or not isinstance(v, dict):
                        continue
                    base = dict(merged_profiles.get(key) or {})
                    base.update(v)
                    merged_profiles[key] = base
                if merged_profiles:
                    CONFIG['submit_profiles'] = merged_profiles
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
            if 'submit_timeout_backoff_seconds' in saved:
                try:
                    CONFIG['submit_timeout_backoff_seconds'] = max(0.5, float(saved['submit_timeout_backoff_seconds']))
                except Exception:
                    pass
            if 'batch_min_interval' in saved:
                CONFIG['batch_min_interval'] = saved['batch_min_interval']
            if 'fast_lane_enabled' in saved:
                CONFIG['fast_lane_enabled'] = bool(saved['fast_lane_enabled'])
            if 'fast_lane_seconds' in saved:
                try:
                    CONFIG['fast_lane_seconds'] = max(0.0, float(saved['fast_lane_seconds']))
                except Exception:
                    pass
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
            if 'post_submit_verify_matrix_timeout_seconds' in saved:
                try:
                    CONFIG['post_submit_verify_matrix_timeout_seconds'] = max(0.3, float(saved['post_submit_verify_matrix_timeout_seconds']))
                except Exception:
                    pass
            if 'post_submit_verify_matrix_recheck_times' in saved:
                try:
                    CONFIG['post_submit_verify_matrix_recheck_times'] = max(0, min(8, int(saved['post_submit_verify_matrix_recheck_times'])))
                except Exception:
                    pass
            if 'post_submit_verify_orders_on_matrix_partial_only' in saved:
                CONFIG['post_submit_verify_orders_on_matrix_partial_only'] = bool(saved['post_submit_verify_orders_on_matrix_partial_only'])
            if 'post_submit_skip_sync_orders_query' in saved:
                CONFIG['post_submit_skip_sync_orders_query'] = bool(saved['post_submit_skip_sync_orders_query'])
            if 'post_submit_orders_sync_fallback' in saved:
                CONFIG['post_submit_orders_sync_fallback'] = bool(saved['post_submit_orders_sync_fallback'])
            if 'post_submit_verify_pending_retry_seconds' in saved:
                try:
                    CONFIG['post_submit_verify_pending_retry_seconds'] = max(0.05, float(saved['post_submit_verify_pending_retry_seconds']))
                except Exception:
                    pass
            if 'post_submit_verify_pending_matrix_recheck_times' in saved:
                try:
                    CONFIG['post_submit_verify_pending_matrix_recheck_times'] = max(0, min(5, int(saved['post_submit_verify_pending_matrix_recheck_times'])))
                except Exception:
                    pass
            if 'manual_verify_pending_recheck_times' in saved:
                try:
                    CONFIG['manual_verify_pending_recheck_times'] = max(0, min(8, int(saved['manual_verify_pending_recheck_times'])))
                except Exception:
                    pass
            if 'manual_verify_pending_retry_seconds' in saved:
                try:
                    CONFIG['manual_verify_pending_retry_seconds'] = max(0.05, min(2.0, float(saved['manual_verify_pending_retry_seconds'])))
                except Exception:
                    pass
            if 'manual_verify_pending_orders_fallback_enabled' in saved:
                CONFIG['manual_verify_pending_orders_fallback_enabled'] = bool(saved['manual_verify_pending_orders_fallback_enabled'])
            if 'too_fast_skip_refill_in_same_request' in saved:
                CONFIG['too_fast_skip_refill_in_same_request'] = bool(saved['too_fast_skip_refill_in_same_request'])
            if 'multi_item_retry_balance_enabled' in saved:
                CONFIG['multi_item_retry_balance_enabled'] = bool(saved['multi_item_retry_balance_enabled'])
            if 'multi_item_batch_retry_times_cap' in saved:
                try:
                    CONFIG['multi_item_batch_retry_times_cap'] = max(0, min(3, int(saved['multi_item_batch_retry_times_cap'])))
                except Exception:
                    pass
            if 'multi_item_retry_total_budget' in saved:
                try:
                    CONFIG['multi_item_retry_total_budget'] = max(0, min(20, int(saved['multi_item_retry_total_budget'])))
                except Exception:
                    pass
            if 'post_submit_treat_verify_timeout_as_retry' in saved:
                CONFIG['post_submit_treat_verify_timeout_as_retry'] = bool(saved['post_submit_treat_verify_timeout_as_retry'])
            if 'refill_window_seconds' in saved:
                CONFIG['refill_window_seconds'] = saved['refill_window_seconds']
            if 'locked_state_values' in saved and isinstance(saved['locked_state_values'], list):
                parsed_locked_states = []
                for v in saved['locked_state_values']:
                    try:
                        parsed_locked_states.append(int(v))
                    except Exception:
                        continue
                if parsed_locked_states:
                    CONFIG['locked_state_values'] = parsed_locked_states
            if 'matrix_timeout_seconds' in saved:
                try:
                    CONFIG['matrix_timeout_seconds'] = max(0.5, float(saved['matrix_timeout_seconds']))
                except Exception:
                    pass
            if 'delivery_warmup_max_retries' in saved:
                try:
                    val, _ = _clamp_exec_param('delivery_warmup_max_retries', saved['delivery_warmup_max_retries'], CONFIG.get('delivery_warmup_max_retries', 5))
                    CONFIG['delivery_warmup_max_retries'] = val
                except Exception:
                    pass
            if 'delivery_total_budget_seconds' in saved:
                try:
                    val, _ = _clamp_exec_param('delivery_total_budget_seconds', saved['delivery_total_budget_seconds'], CONFIG.get('delivery_total_budget_seconds', 20.0))
                    CONFIG['delivery_total_budget_seconds'] = val
                except Exception:
                    pass
            if 'delivery_warmup_budget_seconds' in saved:
                try:
                    val, _ = _clamp_exec_param('delivery_warmup_budget_seconds', saved['delivery_warmup_budget_seconds'], CONFIG.get('delivery_warmup_budget_seconds', 8.0))
                    CONFIG['delivery_warmup_budget_seconds'] = val
                except Exception:
                    pass
            if 'delivery_first_group_from_matrix' in saved:
                CONFIG['delivery_first_group_from_matrix'] = bool(saved['delivery_first_group_from_matrix'])
            if 'delivery_preferred_place_min' in saved:
                try:
                    CONFIG['delivery_preferred_place_min'] = max(0, min(17, int(saved['delivery_preferred_place_min'])))
                except Exception:
                    pass
            if 'delivery_preferred_place_max' in saved:
                try:
                    CONFIG['delivery_preferred_place_max'] = max(0, min(17, int(saved['delivery_preferred_place_max'])))
                except Exception:
                    pass
            if 'delivery_target_blocks' in saved:
                try:
                    CONFIG['delivery_target_blocks'] = max(1, min(3, int(saved['delivery_target_blocks'])))
                except Exception:
                    pass
            if 'delivery_target_times' in saved and isinstance(saved.get('delivery_target_times'), list):
                CONFIG['delivery_target_times'] = [str(t).strip() for t in saved['delivery_target_times'] if re.fullmatch(r'\d{2}:\d{2}', str(t).strip())]
            if 'delivery_time_preference_order' in saved and isinstance(saved.get('delivery_time_preference_order'), list):
                CONFIG['delivery_time_preference_order'] = [str(t).strip() for t in saved['delivery_time_preference_order'] if re.fullmatch(r'\d{2}:\d{2}', str(t).strip())]
            if 'log_to_file' in saved:
                CONFIG['log_to_file'] = bool(saved['log_to_file'])
            if 'log_file_dir' in saved and isinstance(saved['log_file_dir'], str):
                CONFIG['log_file_dir'] = str(saved['log_file_dir']).strip() or 'logs'
            if 'log_retention_days' in saved:
                try:
                    CONFIG['log_retention_days'] = max(0, min(90, int(saved['log_retention_days'])))
                except Exception:
                    pass
            if 'transient_storm_threshold' in saved:
                try:
                    CONFIG['transient_storm_threshold'] = max(1, min(20, int(saved['transient_storm_threshold'])))
                except Exception:
                    pass
            if 'transient_storm_backoff_seconds' in saved:
                try:
                    CONFIG['transient_storm_backoff_seconds'] = max(0.5, float(saved['transient_storm_backoff_seconds']))
                except Exception:
                    pass
            if 'matrix_timeout_storm_seconds' in saved:
                try:
                    CONFIG['matrix_timeout_storm_seconds'] = max(1.0, float(saved['matrix_timeout_storm_seconds']))
                except Exception:
                    pass
            if 'transient_storm_extend_timeout_after' in saved:
                try:
                    CONFIG['transient_storm_extend_timeout_after'] = max(1, min(10, int(saved['transient_storm_extend_timeout_after'])))
                except Exception:
                    pass
            if 'health_check_enabled' in saved:
                CONFIG['health_check_enabled'] = saved['health_check_enabled']
            if 'health_check_interval_min' in saved:
                CONFIG['health_check_interval_min'] = saved['health_check_interval_min']
            if 'health_check_start_time' in saved:
                CONFIG['health_check_start_time'] = normalize_time_str(saved['health_check_start_time']) or CONFIG['health_check_start_time']
            if 'verbose_logs' in saved:
                CONFIG['verbose_logs'] = bool(saved['verbose_logs'])
            if 'metrics_keep_last' in saved:
                try:
                    CONFIG['metrics_keep_last'] = max(50, min(5000, int(saved['metrics_keep_last'])))
                except Exception:
                    pass
            if 'metrics_retention_days' in saved:
                try:
                    CONFIG['metrics_retention_days'] = max(1, min(30, int(saved['metrics_retention_days'])))
                except Exception:
                    pass
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
            if 'auth' in saved:
                # 覆盖默认的 auth 配置
                CONFIG['auth'].update(saved['auth'])
    except Exception as e:
        print(f"加载配置失败: {e}")

# 敏感配置单独文件：若存在 config.secret.json 则覆盖 CONFIG 中对应项（与执行参数分离）
if os.path.exists(CONFIG_SECRET_FILE):
    try:
        with open(CONFIG_SECRET_FILE, 'r', encoding='utf-8') as f:
            secret_saved = json.load(f)
        if isinstance(secret_saved, dict):
            if 'auth' in secret_saved and isinstance(secret_saved['auth'], dict):
                CONFIG['auth'].update(secret_saved['auth'])
            if 'notification_phones' in secret_saved:
                CONFIG['notification_phones'] = secret_saved['notification_phones'] if isinstance(secret_saved['notification_phones'], list) else []
            if 'pushplus_tokens' in secret_saved:
                CONFIG['pushplus_tokens'] = secret_saved['pushplus_tokens'] if isinstance(secret_saved['pushplus_tokens'], list) else []
            if 'sms' in secret_saved and isinstance(secret_saved['sms'], dict):
                CONFIG['sms'].update(secret_saved['sms'])
    except Exception as e:
        print(f"加载敏感配置失败(将使用 config.json 中的值): {e}")

QUIET_WINDOW_LOCK = threading.RLock()
QUIET_WINDOW_REQUEST_CONTEXT = threading.local()
QUIET_WINDOW_PREQUIET_SECONDS = 60.0
QUIET_WINDOW_RECOVER_SECONDS = 15.0
QUIET_WINDOW_TTL_SECONDS = 300.0
QUIET_WINDOW_STATE = {
    "active": False,
    "state": "idle",
    "owner_task_id": None,
    "account_key": None,
    "shop_num": None,
    "reason": "",
    "entered_at_ts": 0.0,
    "fire_at_ts": 0.0,
    "fire_started_at_ts": 0.0,
    "recover_until_ts": 0.0,
    "ttl_deadline_ts": 0.0,
    "released_reason": "",
}


def build_quiet_window_scope(auth=None):
    auth_cfg = auth if isinstance(auth, dict) else (CONFIG.get("auth") or {})
    token = str(auth_cfg.get("token") or "").strip()
    shop_num = str(auth_cfg.get("shop_num") or "").strip()
    token_digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:12] if token else "no-token"
    return {
        "account_key": f"{shop_num}:{token_digest}",
        "shop_num": shop_num,
    }


def get_runtime_request_context():
    ctx = getattr(QUIET_WINDOW_REQUEST_CONTEXT, "value", None)
    return dict(ctx) if isinstance(ctx, dict) else {}


@contextmanager
def runtime_request_context(kind, task_id=None, owner=False):
    prev = getattr(QUIET_WINDOW_REQUEST_CONTEXT, "value", None)
    QUIET_WINDOW_REQUEST_CONTEXT.value = {
        "kind": str(kind or "").strip() or "unknown",
        "task_id": str(task_id) if task_id is not None else None,
        "owner": bool(owner),
    }
    try:
        yield
    finally:
        QUIET_WINDOW_REQUEST_CONTEXT.value = prev


def quiet_window_snapshot():
    with QUIET_WINDOW_LOCK:
        return copy.deepcopy(QUIET_WINDOW_STATE)


def _set_quiet_window_state(next_state):
    with QUIET_WINDOW_LOCK:
        QUIET_WINDOW_STATE.clear()
        QUIET_WINDOW_STATE.update(copy.deepcopy(next_state))


def _quiet_window_matches_scope(snapshot, scope=None):
    if not isinstance(snapshot, dict) or not snapshot.get("active"):
        return False
    resolved_scope = scope if isinstance(scope, dict) else build_quiet_window_scope()
    account_key = str(resolved_scope.get("account_key") or "")
    shop_num = str(resolved_scope.get("shop_num") or "")
    snap_account_key = str(snapshot.get("account_key") or "")
    snap_shop_num = str(snapshot.get("shop_num") or "")
    if snap_account_key and account_key and snap_account_key != account_key:
        return False
    if snap_shop_num and shop_num and snap_shop_num != shop_num:
        return False
    return True


def _quiet_window_is_expired(snapshot, now_ts=None):
    now_val = float(now_ts if now_ts is not None else time.time())
    ttl_deadline_ts = float(snapshot.get("ttl_deadline_ts") or 0.0)
    return bool(ttl_deadline_ts > 0 and now_val >= ttl_deadline_ts)


def get_quiet_window_status(scope=None):
    snapshot = quiet_window_snapshot()
    now_ts = time.time()
    active = bool(snapshot.get("active")) and _quiet_window_matches_scope(snapshot, scope=scope) and (not _quiet_window_is_expired(snapshot, now_ts=now_ts))
    state = str(snapshot.get("state") or "idle")
    if not active:
        return {
            "active": False,
            "state": "idle",
            "owner_task_id": None,
            "remaining_ms": 0,
            "message": "",
            "reason": str(snapshot.get("released_reason") or ""),
        }

    if state == "pre_quiet":
        deadline_ts = float(snapshot.get("fire_at_ts") or 0.0)
    elif state == "recovering":
        deadline_ts = float(snapshot.get("recover_until_ts") or 0.0)
    else:
        deadline_ts = float(snapshot.get("ttl_deadline_ts") or 0.0)
    remaining_ms = int(max(0.0, deadline_ts - now_ts) * 1000) if deadline_ts > 0 else 0
    message_map = {
        "pre_quiet": "主任务即将开始，系统正在静默清场。",
        "fire_window": "主任务执行中，执行类入口已静默。",
        "recovering": "主任务刚结束，系统正在恢复中。",
    }
    return {
        "active": True,
        "state": state,
        "owner_task_id": snapshot.get("owner_task_id"),
        "remaining_ms": remaining_ms,
        "message": message_map.get(state, "静默窗口中"),
        "reason": str(snapshot.get("reason") or ""),
    }


def enter_quiet_window(owner_task_id, fire_at_ts, reason=""):
    now_ts = time.time()
    scope = build_quiet_window_scope()
    owner_id = str(owner_task_id) if owner_task_id is not None else None
    snapshot = quiet_window_snapshot()
    if snapshot.get("active") and str(snapshot.get("owner_task_id") or "") == str(owner_id or ""):
        return snapshot
    next_state = {
        "active": True,
        "state": "pre_quiet",
        "owner_task_id": owner_id,
        "account_key": scope.get("account_key"),
        "shop_num": scope.get("shop_num"),
        "reason": str(reason or ""),
        "entered_at_ts": now_ts,
        "fire_at_ts": float(fire_at_ts or now_ts),
        "fire_started_at_ts": 0.0,
        "recover_until_ts": 0.0,
        "ttl_deadline_ts": max(float(fire_at_ts or now_ts), now_ts) + QUIET_WINDOW_TTL_SECONDS,
        "released_reason": "",
    }
    _set_quiet_window_state(next_state)
    log(f"🔇 [quiet-window] 进入 pre_quiet，owner={owner_id}，fire_at={datetime.fromtimestamp(next_state['fire_at_ts']).strftime('%H:%M:%S')}")
    return quiet_window_snapshot()


def mark_quiet_window_fire(owner_task_id):
    snapshot = quiet_window_snapshot()
    owner_id = str(owner_task_id) if owner_task_id is not None else None
    if not snapshot.get("active"):
        return snapshot
    if str(snapshot.get("owner_task_id") or "") != str(owner_id or ""):
        return snapshot
    if str(snapshot.get("state") or "") == "fire_window":
        return snapshot
    snapshot["state"] = "fire_window"
    snapshot["fire_started_at_ts"] = time.time()
    snapshot["ttl_deadline_ts"] = max(float(snapshot.get("ttl_deadline_ts") or 0.0), snapshot["fire_started_at_ts"] + QUIET_WINDOW_TTL_SECONDS)
    _set_quiet_window_state(snapshot)
    log(f"🔫 [quiet-window] 进入 fire_window，owner={owner_id}")
    return quiet_window_snapshot()


def mark_quiet_window_recovering(owner_task_id, reason=""):
    snapshot = quiet_window_snapshot()
    owner_id = str(owner_task_id) if owner_task_id is not None else None
    if not snapshot.get("active"):
        return snapshot
    if str(snapshot.get("owner_task_id") or "") != str(owner_id or ""):
        return snapshot
    snapshot["state"] = "recovering"
    snapshot["recover_until_ts"] = time.time() + QUIET_WINDOW_RECOVER_SECONDS
    snapshot["ttl_deadline_ts"] = max(float(snapshot.get("ttl_deadline_ts") or 0.0), snapshot["recover_until_ts"] + 5.0)
    snapshot["released_reason"] = str(reason or "")
    _set_quiet_window_state(snapshot)
    log(f"🔄 [quiet-window] 进入 recovering，owner={owner_id}，reason={reason or 'task-finished'}")
    return quiet_window_snapshot()


def release_quiet_window(reason=""):
    snapshot = quiet_window_snapshot()
    if not snapshot.get("active"):
        return snapshot
    next_state = {
        "active": False,
        "state": "idle",
        "owner_task_id": None,
        "account_key": snapshot.get("account_key"),
        "shop_num": snapshot.get("shop_num"),
        "reason": "",
        "entered_at_ts": 0.0,
        "fire_at_ts": 0.0,
        "fire_started_at_ts": 0.0,
        "recover_until_ts": 0.0,
        "ttl_deadline_ts": 0.0,
        "released_reason": str(reason or snapshot.get("released_reason") or "released"),
    }
    _set_quiet_window_state(next_state)
    log(f"🔓 [quiet-window] 已释放，reason={next_state['released_reason']}")
    return quiet_window_snapshot()


def is_quiet_window_active(scope=None):
    return bool(get_quiet_window_status(scope=scope).get("active"))


def quiet_window_block_info(requester_kind, requester_task_id=None, owner_allowed=False, scope=None):
    status = get_quiet_window_status(scope=scope)
    if not status.get("active"):
        return None
    snapshot = quiet_window_snapshot()
    requester_task_id_str = str(requester_task_id) if requester_task_id is not None else None
    owner_task_id_str = str(snapshot.get("owner_task_id") or "") if snapshot.get("owner_task_id") is not None else None
    if owner_allowed and requester_task_id_str and requester_task_id_str == owner_task_id_str:
        return None
    kind = str(requester_kind or "").strip() or "request"
    kind_label_map = {
        "health_check": "健康检查",
        "booking_probe": "下单链路探测",
        "order_query": "订单查询",
        "matrix_query": "场地矩阵查询",
        "submit_order": "下单请求",
        "refill_scheduler": "Refill 轮询",
        "api_matrix": "矩阵接口",
        "api_mine_overview": "我的订单总览",
        "api_cancel_order": "取消订单",
        "api_book": "手动下单",
        "api_check_token": "凭证探测",
        "run_task_now": "立即运行任务",
        "run_refill_task": "立即执行 Refill",
        "add_refill_task": "创建 Refill",
        "update_refill_task": "启用 Refill",
    }
    return {
        "status": "quiet_window_blocked",
        "msg": f"静默窗口中，已阻止{kind_label_map.get(kind, kind)}访问馆方接口。",
        "quiet_window": status,
    }


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

    def get_date_window_status(self, date_str: str) -> str:
        """
        根据当前时间判断某个日期处于哪个预约窗口：
        - past: 已经过期的日期
        - unlocked: 当前可预约窗口内的日期
        - future: 尚未开放的日期

        规则（基于场馆实际表现抽象）：
        - 中午 12 点前：允许预约「今天 ~ +5 天」
        - 中午 12 点及之后：允许预约「今天 ~ +6 天」
        """
        try:
            target_date = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        except Exception:
            # 解析失败时，保守认为在可预约窗口内，避免误判为 future 而隐藏可抢场次
            return "unlocked"

        now = self.get_aligned_now()
        today = now.date()
        diff_days = (target_date - today).days

        if diff_days < 0:
            return "past"

        unlocked_span = 5 if now.hour < 12 else 6
        if diff_days <= unlocked_span:
            return "unlocked"

        return "future"

    def _build_field_info_list(self, date_str, selected_items):
        field_info_list = []
        total_money = 0
        for item in normalize_booking_items(selected_items):
            p_num = str(item["place"])
            start = str(item["time"])
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

            field_info_list.append(
                {
                    "day": date_str,
                    "oldMoney": price,
                    "startTime": start,
                    "endTime": end,
                    "placeShortName": place_short,
                    "name": place_name,
                    "stageTypeShortName": "ymq",
                    "newMoney": price,
                }
            )
            total_money += price
        return field_info_list, total_money

    def _build_reservation_body(self, date_str, selected_items):
        field_info_list, total_money = self._build_field_info_list(date_str, selected_items)
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
        return body, field_info_list, total_money

    def _build_auth_snapshot(self):
        auth_cfg = CONFIG.get("auth") or {}
        return {
            "token": str(self.token or auth_cfg.get("token") or "").strip(),
            "cookie": str(auth_cfg.get("cookie") or "").strip(),
            "shop_num": str(auth_cfg.get("shop_num") or "").strip(),
            "card_index": str(auth_cfg.get("card_index") or "").strip(),
            "card_st_id": str(auth_cfg.get("card_st_id") or "").strip(),
        }

    def _build_worker_headers(self, auth_snapshot):
        headers = dict(self.headers or {})
        cookie = str((auth_snapshot or {}).get("cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        else:
            headers.pop("Cookie", None)
        return headers

    def _build_reservation_body_with_auth(self, date_str, selected_items, auth_snapshot):
        field_info_list, total_money = self._build_field_info_list(date_str, selected_items)
        info_str = urllib.parse.quote(
            json.dumps(field_info_list, separators=(",", ":"), ensure_ascii=False)
        )
        type_encoded = urllib.parse.quote("羽毛球")
        auth_data = auth_snapshot if isinstance(auth_snapshot, dict) else self._build_auth_snapshot()
        body = (
            f"token={auth_data.get('token', '')}&"
            f"shopNum={auth_data.get('shop_num', '')}&"
            f"fieldinfo={info_str}&"
            f"cardStId={auth_data.get('card_st_id', '')}&"
            f"oldTotal={total_money}.00&"
            f"cardPayType=0&"
            f"type={type_encoded}&"
            f"offerId=&"
            f"offerType=&"
            f"total={total_money}.00&"
            f"premerother=&"
            f"cardIndex={auth_data.get('card_index', '')}"
        )
        return body, field_info_list, total_money

    def _create_delivery_session(self, headers_snapshot, pool_size):
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=max(1, int(pool_size or 1)),
            pool_maxsize=max(1, int(pool_size or 1)),
            max_retries=0,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.clear()
        session.headers.update(headers_snapshot or {})
        return session

    def _extract_submit_message(self, resp_data, text):
        if isinstance(resp_data, dict):
            msg = resp_data.get("data")
            if msg is None or msg == "":
                msg = resp_data.get("msg")
            if msg is not None and msg != "":
                return str(msg).strip()
        return str(text or "").strip()

    def _classify_delivery_response(self, raw_message, resp_data=None, exception_text=None):
        msg_raw = str(raw_message or exception_text or "").strip()
        lower = msg_raw.lower()

        transport_keywords = (
            "404",
            "502",
            "503",
            "504",
            "bad gateway",
            "service unavailable",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "connection reset",
            "max retries exceeded",
            "ssl",
            "eof",
            "non-json",
            "html",
            "nginx",
            "空响应",
        )
        rate_limit_keywords = ("操作过快", "请求过于频繁", "too fast", "频繁")
        auth_fail_keywords = ("token", "session", "登录", "失效", "凭证", "未登录", "返回-1")
        payload_fail_keywords = (
            "无票",
            "已售",
            "售罄",
            "sold",
            "占用",
            "已被预订",
            "不可预约",
            "重新选择日期",
            "数据错误",
            "规则",
            "上限",
            "最多预约",
            "可预约场地的时间跨度",
            "预定失败",
        )

        if isinstance(resp_data, dict) and str(resp_data.get("msg") or "").strip() == "success":
            return {
                "action": "stop_success",
                "normalized_msg": "下单请求已被服务端接受，请稍后手动刷新结果",
                "terminal_reason": "business_success",
                "bucket": "success",
            }

        if exception_text:
            if any(k in lower for k in transport_keywords):
                bucket = "timeout" if ("timeout" in lower or "timed out" in lower or "read" in lower) else "connection_error"
                return {
                    "action": "continue_delivery",
                    "normalized_msg": msg_raw[:200] or "网络异常，请继续递送",
                    "terminal_reason": "",
                    "bucket": bucket,
                }
            return {
                "action": "continue_delivery",
                "normalized_msg": msg_raw[:200] or "网络异常，请继续递送",
                "terminal_reason": "",
                "bucket": "connection_error",
            }

        if any(k in lower for k in auth_fail_keywords):
            return {
                "action": "stop_task_fail",
                "normalized_msg": msg_raw[:200] or "鉴权失败",
                "terminal_reason": "auth_fail",
                "bucket": "auth_fail",
            }
        if any(k in lower for k in rate_limit_keywords):
            return {
                "action": "min_backoff_continue",
                "normalized_msg": msg_raw[:200] or "操作过快,请稍后重试。",
                "terminal_reason": "",
                "bucket": "rate_limited",
            }
        if any(k in lower for k in transport_keywords):
            if "404" in lower:
                bucket = "resp_404"
            elif any(k in lower for k in ("502", "503", "504", "bad gateway", "service unavailable", "nginx")):
                bucket = "resp_5xx"
            elif "timeout" in lower or "timed out" in lower:
                bucket = "timeout"
            else:
                bucket = "non_json"
            return {
                "action": "continue_delivery",
                "normalized_msg": msg_raw[:200] or "网关/传输层异常，请继续递送",
                "terminal_reason": "",
                "bucket": bucket,
            }
        if any(k in lower for k in payload_fail_keywords):
            return {
                "action": "switch_backup",
                "normalized_msg": msg_raw[:200] or "当前组合业务失败",
                "terminal_reason": "payload_terminal_fail",
                "bucket": "payload_fail",
            }
        return {
            "action": "stop_task_fail",
            "normalized_msg": msg_raw[:200] or "业务层未知终局失败",
            "terminal_reason": "unknown_business_fail",
            "bucket": "unknown_business_fail",
        }

    def _post_reservation_once(self, worker_session, headers_snapshot, url, body, timeout_s):
        started_at = time.time()
        # #region agent log
        try:
            _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "debug-c0435f.log")
            _log_path = os.path.normpath(_log_path)
            with open(_log_path, "a", encoding="utf-8") as _f:
                _f.write(json.dumps({"sessionId": "c0435f", "timestamp": int(started_at * 1000), "location": "app.py:_post_reservation_once", "message": "request sent", "data": {"url": url, "body_len": len(body), "send_time_iso": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}, "hypothesisId": "send_time_log"}, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # #endregion
        try:
            resp = worker_session.post(
                url,
                headers=headers_snapshot,
                data=body,
                timeout=timeout_s,
                verify=False,
            )
            ended_at = time.time()
            self._update_server_time_offset(resp, started_at, ended_at)
            text = (resp.text or "").strip()
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = None
            raw_message = self._extract_submit_message(resp_data, text)
            return {
                "ok": True,
                "status_code": int(getattr(resp, "status_code", 0) or 0),
                "resp_data": resp_data,
                "raw_text": text,
                "raw_message": raw_message,
                "elapsed_ms": int(max(0.0, ended_at - started_at) * 1000),
            }
        except Exception as e:
            return {
                "ok": False,
                "exception_text": str(e),
                "elapsed_ms": int(max(0.0, time.time() - started_at) * 1000),
            }

    def submit_delivery_campaign(self, date_str, delivery_groups, submit_profile=None, task_config=None):
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"
        profile_name = str(submit_profile or "").strip()
        profile_settings = get_submit_profile_settings(profile_name)

        def cfg_get_local(key, default=None):
            if key in profile_settings:
                return profile_settings.get(key)
            return CONFIG.get(key, default)

        def get_first_group_cfg(key, default=None):
            """第一组矩阵参数：任务 config 优先，再回退 CONFIG/profile。"""
            if isinstance(task_config, dict) and key in task_config:
                v = task_config.get(key)
                if v is not None:
                    return v
            return cfg_get_local(key, default)

        groups = normalize_delivery_groups(delivery_groups)
        run_metric = {
            "submit_req_count": 0,
            "submit_success_resp_count": 0,
            "submit_retry_count": 0,
            "confirm_matrix_poll_count": 0,
            "confirm_orders_poll_count": 0,
            "verify_exception_count": 0,
            "t_confirm_ms": None,
            "submit_profile": profile_name or "default",
            "request_mode": "delivery_campaign",
            "rate_limited": False,
            "transport_error": False,
            "business_fail_msg": "",
            "server_msg_raw": "",
            "t_first_post_ms": None,
            "t_first_accept_ms": None,
            "attempt_count_total": 0,
            "attempt_count_inflight_peak": 0,
            "dispatch_round_count": 0,
            "delivery_window_ms": None,
            "stopped_by": "",
            "resp_404_count": 0,
            "resp_5xx_count": 0,
            "timeout_count": 0,
            "connection_error_count": 0,
            "rate_limited_count": 0,
            "auth_fail_count": 0,
            "non_json_count": 0,
            "combo_tier": "primary",
            "backup_promoted_count": 0,
            "picked_group_id": "",
            "delivery_status": "retrying",
            "business_status": "unknown",
            "terminal_reason": "",
            "refill_matrix_fetch_count": 0,
            "too_fast_matrix_refresh_count": 0,
            "delivery_backup_groups_ignored": 0,
            "refill_candidate_found_count": 0,
            "refill_no_candidate_count": 0,
            "goal_satisfied": False,
        }
        if not groups:
            return {
                "status": "fail",
                "msg": "终极递送器至少需要 1 组合法 payload",
                "success_items": [],
                "failed_items": [],
                "run_metric": run_metric,
            }

        timeout_s = max(0.5, float(cfg_get_local("submit_timeout_seconds", 4.0) or 4.0))
        transport_round_interval_s = max(0.05, float(cfg_get_local("delivery_transport_round_interval_seconds", 0.25) or 0.25))
        refill_poll_interval_s = max(0.05, float(cfg_get_local("delivery_refill_matrix_poll_seconds", 0.35) or 0.35))
        delivery_min_post_interval_s = max(
            0.0, float(cfg_get_local("delivery_min_post_interval_seconds", CONFIG.get("delivery_min_post_interval_seconds", 2.2)) or 0)
        )
        run_metric["effective_delivery_min_post_interval_seconds"] = float(delivery_min_post_interval_s)
        delivery_total_budget_s, _ = _clamp_exec_param("delivery_total_budget_seconds", cfg_get_local("delivery_total_budget_seconds", 20.0) or 20.0, 20.0)
        headers_snapshot = dict(self.headers or {})
        sessions = [self.session]
        use_main_session = True

        campaign_started_at = time.time()
        deadline_ts = campaign_started_at + delivery_total_budget_s
        post_spacing = {"last_end_mono": None}

        def mark_bucket(bucket_name):
            if bucket_name == "resp_404":
                run_metric["resp_404_count"] += 1
            elif bucket_name == "resp_5xx":
                run_metric["resp_5xx_count"] += 1
            elif bucket_name == "timeout":
                run_metric["timeout_count"] += 1
            elif bucket_name == "connection_error":
                run_metric["connection_error_count"] += 1
            elif bucket_name == "rate_limited":
                run_metric["rate_limited_count"] += 1
            elif bucket_name == "auth_fail":
                run_metric["auth_fail_count"] += 1
            elif bucket_name in ("non_json", "unknown_business_fail"):
                run_metric["non_json_count"] += 1

        log(
            f"[极速订场] 开始 date={date_str} 单路递送+refill round_interval={transport_round_interval_s}s "
            f"refill_poll={refill_poll_interval_s}s min_post_interval={delivery_min_post_interval_s}s "
            f"budget={delivery_total_budget_s}s"
        )
        try:
            # 第一组发送前拉活：直接 get_matrix 并根据返回值判断，可重试错误有限次重试，鉴权错误不重试
            warmup_max_attempts, _ = _clamp_exec_param("delivery_warmup_max_retries", cfg_get_local("delivery_warmup_max_retries", 5) or 5, 5)
            warmup_budget_s, _ = _clamp_exec_param("delivery_warmup_budget_seconds", cfg_get_local("delivery_warmup_budget_seconds", 8.0) or 8.0, 8.0)
            warmup_deadline = time.time() + warmup_budget_s
            warmup_attempt = 0
            warmup_ok = False
            warmup_result = None
            probe_main_group_sent = False
            matrix_timeout_s = max(0.5, float(CONFIG.get("matrix_timeout_seconds", 3.0) or 3.0))
            transient_keywords = (
                "非json格式", "non-json", "404", "502", "503", "504", "无效数据",
                "nginx", "bad gateway", "service unavailable", "timeout", "timed out",
                "connection reset", "max retries exceeded", "temporarily unavailable",
            )
            auth_keywords = ("失效", "凭证", "token", "登录", "未登录", "返回-1")
            while warmup_attempt < warmup_max_attempts and time.time() < warmup_deadline:
                warmup_attempt += 1
                if time.time() >= warmup_deadline:
                    break
                warmup = self.get_matrix(
                    date_str, include_mine_overlay=False, request_timeout=matrix_timeout_s, bypass_cache=True
                )
                if isinstance(warmup, dict) and not warmup.get("error"):
                    # 先分类：已解锁 vs 未解锁（不依赖 12 点，仅 window_status + matrix）
                    window_status = self.get_date_window_status(date_str)
                    matrix = warmup.get("matrix") or {}
                    has_available = any(
                        matrix.get(p, {}).get(t) == "available"
                        for p in matrix
                        for t in (matrix.get(p) or {})
                    )
                    not_yet_unlocked = (window_status == "future" and not has_available)
                    if not not_yet_unlocked:
                        log("[极速订场] 拉活成功，开始递送")
                        warmup_ok = True
                        warmup_result = warmup
                        break
                    # 未解锁：首次发一次主组，成功则 return，失败则继续拉活
                    if not probe_main_group_sent:
                        main_items = normalize_booking_items(groups[0].get("items") or [])
                        if main_items:
                            body, _, _ = self._build_reservation_body(date_str, main_items)
                            result = self._post_reservation_once(sessions[0], headers_snapshot, url, body, timeout_s)
                            if delivery_min_post_interval_s > 0:
                                post_spacing["last_end_mono"] = time.perf_counter()
                            probe_main_group_sent = True
                            log("[极速订场] 未解锁 首次试探主组 1 次")
                            if run_metric.get("t_first_post_ms") is None:
                                run_metric["t_first_post_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
                            run_metric["attempt_count_total"] = (run_metric.get("attempt_count_total") or 0) + 1
                            run_metric["submit_req_count"] = (run_metric.get("submit_req_count") or 0) + 1
                            run_metric.setdefault("submit_latencies_ms", []).append(int(result.get("elapsed_ms") or 0))
                            raw_msg = result.get("raw_message") if result.get("ok") else result.get("exception_text")
                            classified = self._classify_delivery_response(
                                result.get("raw_message"),
                                resp_data=result.get("resp_data"),
                                exception_text=result.get("exception_text") if not result.get("ok") else None,
                            )
                            bucket = str(classified.get("bucket") or "")
                            mark_bucket(bucket)
                            msg_snippet = str(raw_msg or result.get("exception_text") or "")[:200]
                            if msg_snippet:
                                run_metric["server_msg_raw"] = msg_snippet
                            if classified.get("action") == "stop_success":
                                run_metric["submit_success_resp_count"] = (run_metric.get("submit_success_resp_count") or 0) + 1
                                run_metric["delivery_window_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
                                run_metric["stopped_by"] = "business_success"
                                run_metric["delivery_status"] = "accepted"
                                run_metric["business_status"] = "success"
                                run_metric["terminal_reason"] = str(classified.get("terminal_reason") or "business_success")
                                group_id = str(groups[0].get("id") or "primary")
                                group_label = str(groups[0].get("label") or "主组合")
                                log(f"[极速订场] 未解锁试探主组成功 结束 status=success group={group_id}")
                                return {
                                    "status": "success",
                                    "msg": f"{group_label}已被服务端接受，请稍后手动刷新结果",
                                    "success_items": main_items,
                                    "failed_items": [],
                                    "run_metric": run_metric,
                                    "delivery_group_id": group_id,
                                    "manual_followup_required": True,
                                }
                        else:
                            probe_main_group_sent = True
                    else:
                        log("[极速订场] 未解锁 主组已发过 继续拉活")
                    time.sleep(0.8)
                    continue
                err_msg = str(warmup.get("error", "") if isinstance(warmup, dict) else "")
                err_l = err_msg.lower()
                if any(k in err_msg for k in auth_keywords) or any(k in err_l for k in ("token", "登录", "未登录")):
                    log(f"[极速订场] 拉活失败(鉴权/会话)，不重试: {err_msg[:200]}")
                    return {
                        "status": "fail",
                        "msg": "拉活失败：鉴权/会话失效" + (f" ({err_msg[:100]})" if err_msg else ""),
                        "success_items": [],
                        "failed_items": (groups[0].get("items") or []) if groups else [],
                        "run_metric": run_metric,
                    }
                log(f"[极速订场] 拉活失败(可重试) 第{warmup_attempt}次: {err_msg[:150]}")
                time.sleep(0.8)
            if not warmup_ok:
                log("[极速订场] 拉活失败(多次重试后仍不可用)，未发送预订请求")
                return {
                    "status": "fail",
                    "msg": "拉活失败（多次重试后仍不可用）",
                    "success_items": [],
                    "failed_items": (groups[0].get("items") or []) if groups else [],
                    "run_metric": run_metric,
                }

            pre_matrix_primary_items = normalize_booking_items(groups[0].get("items") or [])

            if get_first_group_cfg("delivery_first_group_from_matrix") and warmup_result and warmup_result.get("matrix"):
                first_group_times_raw = get_first_group_cfg("delivery_first_group_times")
                first_group_times_ok = isinstance(first_group_times_raw, list) and len(first_group_times_raw) > 0
                if first_group_times_ok:
                    tb_task = _delivery_target_blocks_from_task_config(task_config)
                    if tb_task is not None:
                        matrix_blocks = tb_task
                        run_metric["matrix_first_group_blocks_source"] = "task_config"
                    elif pre_matrix_primary_items:
                        matrix_blocks = _delivery_target_blocks_from_items(pre_matrix_primary_items)
                        run_metric["matrix_first_group_blocks_source"] = "derived_from_items"
                    else:
                        log("[极速订场] 首组矩阵模式缺少 delivery_target_blocks 且主组无 items，无法求解")
                        return {
                            "status": "fail",
                            "msg": "请配置 delivery_target_blocks 或主组合 items（首组由矩阵算出时）",
                            "success_items": [],
                            "failed_items": (groups[0].get("items") or []) if groups else [],
                            "run_metric": run_metric,
                        }
                    first_group_cfg = {
                        "delivery_target_blocks": matrix_blocks,
                        "delivery_target_times": get_first_group_cfg("delivery_target_times") or ["20:00", "21:00"],
                        "delivery_time_preference_order": get_first_group_cfg("delivery_time_preference_order") or ["20:00", "21:00"],
                        "delivery_first_group_times": get_first_group_cfg("delivery_first_group_times"),
                        "delivery_first_group_time_preference_order": get_first_group_cfg("delivery_first_group_time_preference_order"),
                        "delivery_preferred_place_min": get_first_group_cfg("delivery_preferred_place_min"),
                        "delivery_preferred_place_max": get_first_group_cfg("delivery_preferred_place_max"),
                    }
                    computed_items, combo_level = compute_first_group_from_matrix(
                        warmup_result["matrix"],
                        warmup_result.get("places"),
                        warmup_result.get("times"),
                        first_group_cfg,
                    )
                    if computed_items:
                        groups[0] = {**groups[0], "items": computed_items}
                        run_metric["first_group_combo_level"] = combo_level
                        log(f"[极速订场] 第一组由 matrix 算出 level={combo_level} items={computed_items}")
                    else:
                        log("[极速订场] 第一组无可用组合(矩阵降级全无解)，未发送预订请求")
                        return {
                            "status": "fail",
                            "msg": "第一组无可用组合（矩阵中无满足条件的连号与时段）",
                            "success_items": [],
                            "failed_items": (groups[0].get("items") or []) if groups else [],
                            "run_metric": run_metric,
                        }
                else:
                    log("[极速订场] 未配置首组目标时段，首组按主组原样递送")

            primary = groups[0]
            group_id = str(primary.get("id") or "primary")
            group_label = str(primary.get("label") or "主组合")
            run_metric["picked_group_id"] = group_id
            target_items = normalize_booking_items(primary.get("items") or [])
            if not target_items:
                return {
                    "status": "fail",
                    "msg": "主组合无有效预订项",
                    "success_items": [],
                    "failed_items": [],
                    "run_metric": run_metric,
                    "delivery_group_id": group_id,
                }

            target_times_from_items = sorted({str(it.get("time")) for it in target_items if str(it.get("time") or "")})
            intent_target_times = get_first_group_cfg("delivery_first_group_times") or target_times_from_items
            intent_target_times = [str(t).strip() for t in intent_target_times if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())]
            if not intent_target_times:
                intent_target_times = list(target_times_from_items)
            intent_time_order = get_first_group_cfg("delivery_first_group_time_preference_order") or intent_target_times
            intent_time_order = [str(t).strip() for t in intent_time_order if str(t).strip() in set(intent_target_times)] or list(intent_target_times)
            tb_task = _delivery_target_blocks_from_task_config(task_config)
            if tb_task is not None:
                intent_target_blocks = tb_task
                run_metric["goal_target_blocks_source"] = "task_config"
            else:
                intent_target_blocks = _delivery_target_blocks_from_items(target_items)
                run_metric["goal_target_blocks_source"] = "derived_from_items"
            campaign_intent = {
                "target_blocks": intent_target_blocks,
                "target_times": intent_target_times,
                "time_preference_order": intent_time_order,
                "preferred_place_min": int(get_first_group_cfg("delivery_preferred_place_min", 0) or 0),
                "preferred_place_max": int(get_first_group_cfg("delivery_preferred_place_max", 0) or 0),
                "require_consecutive": True,
            }
            run_metric["goal_target_blocks"] = int(campaign_intent.get("target_blocks") or 1)
            run_metric["goal_target_times"] = list(campaign_intent.get("target_times") or [])

            refill_limits = {}
            for _lim_key in ("max_items_per_batch", "max_consecutive_slots_per_place", "max_places_per_timeslot"):
                _cfg_key = f"delivery_refill_{_lim_key}"
                try:
                    _v = cfg_get_local(_cfg_key, None)
                    if _v is not None:
                        refill_limits[_lim_key] = int(_v)
                except Exception:
                    pass

            def _campaign_post_batch(batch_items, phase_tag):
                """单次提交一批并分类；递增 run_metric 计数。"""
                if not batch_items:
                    return None, None
                body, _, _ = self._build_reservation_body(date_str, batch_items)
                now_wall = time.time()
                if run_metric.get("t_first_post_ms") is None:
                    run_metric["t_first_post_ms"] = int(max(0.0, now_wall - campaign_started_at) * 1000)
                if delivery_min_post_interval_s > 0 and post_spacing["last_end_mono"] is not None:
                    _remain = delivery_min_post_interval_s - (time.perf_counter() - post_spacing["last_end_mono"])
                    if _remain > 0:
                        time.sleep(_remain)
                if phase_tag == "refill":
                    campaign_ms = int(max(0.0, time.time() - campaign_started_at) * 1000)
                    log(
                        f"[极速订场] refill POST发送 wall={datetime.now().strftime('%H:%M:%S.%f')[:-3]} "
                        f"campaign_ms={campaign_ms} items={batch_items}"
                    )
                result = self._post_reservation_once(sessions[0], headers_snapshot, url, body, timeout_s)
                if delivery_min_post_interval_s > 0:
                    post_spacing["last_end_mono"] = time.perf_counter()
                run_metric["attempt_count_total"] = (run_metric.get("attempt_count_total") or 0) + 1
                run_metric["submit_req_count"] = (run_metric.get("submit_req_count") or 0) + 1
                run_metric.setdefault("submit_latencies_ms", []).append(int(result.get("elapsed_ms") or 0))
                run_metric["dispatch_round_count"] = (run_metric.get("dispatch_round_count") or 0) + 1
                run_metric["attempt_count_inflight_peak"] = max(
                    int(run_metric.get("attempt_count_inflight_peak") or 0), 1
                )
                if result.get("ok"):
                    raw_msg = str(result.get("raw_message") or "")[:200]
                    run_metric["server_msg_raw"] = raw_msg or run_metric.get("server_msg_raw") or ""
                    classified = self._classify_delivery_response(
                        result.get("raw_message"),
                        resp_data=result.get("resp_data"),
                        exception_text=None,
                    )
                else:
                    run_metric["transport_error"] = True
                    raw_msg = str(result.get("exception_text") or "")[:200]
                    run_metric["server_msg_raw"] = raw_msg or run_metric.get("server_msg_raw") or ""
                    classified = self._classify_delivery_response(
                        result.get("exception_text"),
                        resp_data=None,
                        exception_text=result.get("exception_text"),
                    )
                bucket = str(classified.get("bucket") or "")
                mark_bucket(bucket)
                action = str(classified.get("action") or "")
                if action == "min_backoff_continue":
                    run_metric["rate_limited"] = True
                    run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
                    log(
                        f"[极速订场] {phase_tag} too_fast: "
                        f"{(result.get('raw_message') or result.get('exception_text') or '')[:200]!r}"
                    )
                elif action == "stop_success":
                    run_metric["submit_success_resp_count"] = (run_metric.get("submit_success_resp_count") or 0) + 1
                elif action == "continue_delivery":
                    run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
                elif action in ("switch_backup", "stop_task_fail"):
                    run_metric["business_fail_msg"] = str(classified.get("normalized_msg") or "")[:200]

                if run_metric.get("t_first_accept_ms") is None and action in (
                    "stop_success",
                    "switch_backup",
                    "stop_task_fail",
                    "min_backoff_continue",
                ):
                    run_metric["t_first_accept_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
                log(
                    f"[极速订场] {phase_tag} 第{run_metric['dispatch_round_count']}次提交 action={action} "
                    f"items={batch_items}"
                )
                return classified, result

            def _return_success(batch_items, goal_satisfied=True):
                run_metric["delivery_window_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
                run_metric["stopped_by"] = "business_success" if goal_satisfied else "business_success_but_goal_not_met"
                run_metric["delivery_status"] = "accepted"
                run_metric["business_status"] = "success" if goal_satisfied else "partial_success"
                run_metric["terminal_reason"] = "business_success" if goal_satisfied else "business_success_but_goal_not_met"
                log(f"[极速订场] 结束 status=success 总请求数={run_metric['submit_req_count']} group={group_id}")
                return {
                    "status": "success" if goal_satisfied else "partial",
                    "msg": f"{group_label}已被服务端接受，请稍后手动刷新结果" if goal_satisfied else f"{group_label}请求被接受，但未满足目标约束，继续补齐前请刷新订单确认",
                    "success_items": batch_items,
                    "failed_items": [],
                    "run_metric": run_metric,
                    "delivery_group_id": group_id,
                    "manual_followup_required": True,
                }

            def _return_task_fail(classified, batch_items):
                run_metric["delivery_window_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
                run_metric["stopped_by"] = "task_fail"
                run_metric["delivery_status"] = "blocked"
                run_metric["business_status"] = "fail"
                run_metric["terminal_reason"] = str(classified.get("terminal_reason") or "task_fail")
                log(f"[极速订场] 结束 status=fail(task_fail) 总请求数={run_metric['submit_req_count']}")
                return {
                    "status": "fail",
                    "msg": str(classified.get("normalized_msg") or "业务层终局失败"),
                    "success_items": [],
                    "failed_items": batch_items,
                    "run_metric": run_metric,
                    "delivery_group_id": group_id,
                }

            batch_limits_arg = refill_limits if refill_limits else None
            legal_batches = group_booking_items_into_legal_batches(
                target_items, cfg_get_local, profile_name=profile_name, batch_limits=batch_limits_arg
            )
            if not legal_batches:
                return {
                    "status": "fail",
                    "msg": "主组合无法拆分为合法批次",
                    "success_items": [],
                    "failed_items": target_items,
                    "run_metric": run_metric,
                    "delivery_group_id": group_id,
                }
            first_batch = legal_batches[0]
            log(f"[极速订场] 首单批次 group={group_id} ({group_label}) items={first_batch}")

            classified, _result = _campaign_post_batch(first_batch, "首单")
            if classified:
                act = classified.get("action")
                if act == "stop_success":
                    if is_goal_satisfied(target_items, campaign_intent):
                        run_metric["goal_satisfied"] = True
                        return _return_success(first_batch, goal_satisfied=True)
                    log("[极速订场] 首单被接受但目标约束未满足，进入refill继续重算")
                    run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
                if act == "stop_task_fail":
                    return _return_task_fail(classified, first_batch)

            # refill：每轮先拉矩阵再装箱再单次提交；too_fast / 业务可重试类均下轮重拉矩阵（不再长退避、不切备用组）
            while time.time() < deadline_ts:
                run_metric["refill_matrix_fetch_count"] = (run_metric.get("refill_matrix_fetch_count") or 0) + 1
                mx_t0 = time.perf_counter()
                mx = self.get_matrix(
                    date_str, include_mine_overlay=False, request_timeout=matrix_timeout_s, bypass_cache=True
                )
                if not isinstance(mx, dict) or mx.get("error"):
                    err = str((mx or {}).get("error", "matrix_fail") if isinstance(mx, dict) else "matrix_fail")[:120]
                    log(f"[极速订场] refill get_matrix 失败: {err}，{refill_poll_interval_s}s 后重试")
                    time.sleep(refill_poll_interval_s)
                    continue
                mx_elapsed_ms = int((time.perf_counter() - mx_t0) * 1000)
                log(
                    f"[极速订场] refill 矩阵完成 fetch_n={run_metric['refill_matrix_fetch_count']} "
                    f"wall={datetime.now().strftime('%H:%M:%S.%f')[:-3]} elapsed_ms={mx_elapsed_ms}"
                )
                matrix_live = mx.get("matrix") or {}
                target_blocks_live = max(1, int(campaign_intent.get("target_blocks") or 1))
                target_times_live = [str(t).strip() for t in (campaign_intent.get("target_times") or []) if str(t).strip()]
                need_by_time = {}
                for t in target_times_live:
                    mine_cnt = 0
                    for p in (mx.get("places") or list(matrix_live.keys())):
                        st = (matrix_live.get(str(p)) or {}).get(t)
                        if _matrix_cell_is_mine(st):
                            mine_cnt += 1
                    need_by_time[t] = max(0, target_blocks_live - mine_cnt)
                if sum(int(v) for v in need_by_time.values()) <= 0:
                    run_metric["goal_satisfied"] = True
                    log("[极速订场] refill 缺口已归零，目标已满足，提前结束")
                    places_for_mine = mx.get("places") or list(matrix_live.keys())
                    mine_items = collect_mine_items_from_matrix(matrix_live, places_for_mine, target_times_live)
                    return _return_success(mine_items, goal_satisfied=True)
                places_list = mx.get("places") or list(matrix_live.keys())
                intent_base = {k: v for k, v in campaign_intent.items() if k != "require_consecutive"}
                intent_base["target_blocks"] = target_blocks_live
                intent_base["target_times"] = list(target_times_live)
                solved, used_need_by_time, tier_label = solve_refill_need_tiered(
                    matrix_live,
                    places_list,
                    intent_base,
                    need_by_time,
                    allow_scatter=True,
                )
                if not solved or not solved.get("items"):
                    run_metric["refill_no_candidate_count"] = (run_metric.get("refill_no_candidate_count") or 0) + 1
                    log(f"[极速订场] refill 本轮无满足约束候选，缺口={need_by_time}，短等待后重拉")
                    time.sleep(refill_poll_interval_s)
                    continue
                log(
                    f"[极速订场] refill 分层求解 tier={tier_label} used_need={used_need_by_time} "
                    f"原缺口={need_by_time}"
                )
                avail_items = normalize_booking_items(solved.get("items") or [])
                run_metric["refill_candidate_found_count"] = (run_metric.get("refill_candidate_found_count") or 0) + 1
                rbatches = group_booking_items_into_legal_batches(
                    avail_items, cfg_get_local, profile_name=profile_name, batch_limits=batch_limits_arg
                )
                if not rbatches:
                    time.sleep(refill_poll_interval_s)
                    continue
                batch = rbatches[0]
                c2, _r2 = _campaign_post_batch(batch, "refill")
                if not c2:
                    time.sleep(refill_poll_interval_s)
                    continue
                act2 = c2.get("action")
                if act2 == "stop_success":
                    if is_goal_satisfied(avail_items, campaign_intent):
                        run_metric["goal_satisfied"] = True
                        return _return_success(batch, goal_satisfied=True)
                    log("[极速订场] refill 请求被接受但目标约束未满足，继续下一轮重算")
                    run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
                    time.sleep(transport_round_interval_s)
                    continue
                if act2 == "stop_task_fail":
                    return _return_task_fail(c2, batch)
                if act2 == "min_backoff_continue":
                    run_metric["too_fast_matrix_refresh_count"] = (
                        run_metric.get("too_fast_matrix_refresh_count") or 0
                    ) + 1
                    continue
                if act2 == "switch_backup":
                    run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
                    pm = str(c2.get("normalized_msg") or "")[:200]
                    run_metric["business_fail_msg"] = pm
                    log(f"[极速订场] refill 业务可重试/冲突类响应，下轮重拉矩阵: {pm!r}")
                    continue
                if act2 == "continue_delivery":
                    time.sleep(transport_round_interval_s)
                    continue

            run_metric["delivery_window_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
            run_metric["stopped_by"] = "delivery_budget_exhausted"
            run_metric["delivery_status"] = "exhausted"
            run_metric["business_status"] = "unknown"
            run_metric["terminal_reason"] = "delivery_budget_exhausted"
            log(f"[极速订场] 结束 status=fail(budget_exhausted) 总请求数={run_metric['submit_req_count']} 轮数={run_metric['dispatch_round_count']}")
            return {
                "status": "fail",
                "msg": "递送窗口已耗尽，未拿到业务层明确终局响应",
                "success_items": [],
                "failed_items": target_items,
                "run_metric": run_metric,
                "delivery_group_id": group_id,
            }
        finally:
            if not use_main_session:
                for session in sessions:
                    try:
                        session.close()
                    except Exception:
                        pass

    def submit_order_minimal(self, date_str, selected_items, submit_profile=None):
        items = normalize_booking_items(selected_items)
        if not items:
            return {
                "status": "fail",
                "msg": "极简直提至少需要 1 个有效的场地时间组合",
                "success_items": [],
                "failed_items": [],
                "run_metric": {
                    "request_mode": "delivery_campaign",
                    "submit_req_count": 0,
                    "submit_success_resp_count": 0,
                    "submit_retry_count": 0,
                    "confirm_matrix_poll_count": 0,
                    "confirm_orders_poll_count": 0,
                    "verify_exception_count": 0,
                },
            }
        return self.submit_delivery_campaign(
            date_str,
            [{"id": "primary", "label": "主组合", "items": items}],
            submit_profile=submit_profile,
        )

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
        ctx = get_runtime_request_context()
        quiet_info = quiet_window_block_info(
            "booking_probe",
            requester_task_id=ctx.get("task_id"),
            owner_allowed=bool(ctx.get("owner")),
        )
        if quiet_info:
            return {"ok": False, "unknown": True, "msg": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")}
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

    def get_place_orders(self, page_size=20, max_pages=4, timeout_s=6):
        """获取我的场地订单列表（用于识别 mine 状态）。

        为了提升「手动预订」与「我的场地」页面的响应速度，这里适当收紧了分页与超时时间：
        - max_pages：从 6 减少到 4，优先关注最近的订单页
        - timeout_s：从 10s 降到 6s，避免远端过慢时长时间卡住请求
        """
        ctx = get_runtime_request_context()
        quiet_info = quiet_window_block_info(
            "order_query",
            requester_task_id=ctx.get("task_id"),
            owner_allowed=bool(ctx.get("owner")),
        )
        if quiet_info:
            return {"error": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")}
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

    def get_use_card_info(self, timeout_s=6):
        """获取用户卡信息（getUseCardInfo），用于展示余额。一用户一卡时取 universal[0].cardcash。"""
        ctx = get_runtime_request_context()
        quiet_info = quiet_window_block_info(
            "order_query",
            requester_task_id=ctx.get("task_id"),
            owner_allowed=bool(ctx.get("owner")),
        )
        if quiet_info:
            return {"error": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")}
        url = f"https://{self.host}/easyserpClient/common/getUseCardInfo"
        # 与 README 抓包一致：projectInfo 为非空数组，至少一条场地/时段，否则接口可能返回空 universal
        project_info_minimal = [{
            "day": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "oldMoney": 100,
            "startTime": "21:00",
            "endTime": "22:00",
            "placeShortName": "ymq1",
            "name": "羽毛球场1",
            "stageTypeShortName": "ymq",
        }]
        project_info_encoded = urllib.parse.quote(json.dumps(project_info_minimal, ensure_ascii=False))
        body = (
            f"token={urllib.parse.quote(str(self.token or ''))}&"
            f"shopNum={urllib.parse.quote(str(CONFIG['auth'].get('shop_num') or ''))}&"
            f"projectType=3&"
            f"projectInfo={project_info_encoded}"
        )
        try:
            resp = self.session.post(
                url,
                headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"},
                data=body,
                timeout=max(0.5, float(timeout_s or 6)),
                verify=False,
            )
            data = resp.json()
        except Exception as e:
            return {"error": f"获取卡信息失败: {e}"}
        if not isinstance(data, dict):
            return {"error": "卡信息接口返回格式错误"}
        if data.get("msg") != "success":
            return {"error": f"卡信息接口返回异常: {data.get('msg', '')}"}
        inner = data.get("data")
        if not isinstance(inner, dict):
            return {"data": None, "universal": []}
        universal = inner.get("universal")
        if not isinstance(universal, list):
            universal = []
        # #region agent log
        try:
            _log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "debug-c0435f.log")
            _log = os.path.normpath(_log)
            first = universal[0] if universal else {}
            with open(_log, "a", encoding="utf-8") as _f:
                _f.write(json.dumps({"sessionId": "c0435f", "location": "app.py:get_use_card_info", "message": "card_info_return", "data": {"universal_len": len(universal), "has_cardcash": "cardcash" in first}, "timestamp": int(time.time() * 1000), "hypothesisId": "projectInfo"}, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # #endregion
        return {"data": inner, "universal": universal}

    def cancel_place_order(self, bill_num, reason="用户取消"):
        """调用取消预约接口 canclePlaceAppointment。"""
        url = f"https://{self.host}/easyserpClient/place/canclePlaceAppointment"
        body = (
            f"outtradeno={urllib.parse.quote(str(bill_num or ''))}&"
            f"token={urllib.parse.quote(str(self.token or ''))}&"
            f"reason={urllib.parse.quote(str(reason or '用户取消'))}"
        )
        try:
            resp = self.session.post(
                url,
                headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"},
                data=body,
                timeout=10,
                verify=False,
            )
            data = resp.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        if not isinstance(data, dict):
            return {"ok": False, "msg": "接口返回格式错误"}
        if data.get("msg") == "success":
            return {"ok": True}
        return {"ok": False, "msg": data.get("msg", "未知错误")}

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

    def extract_mine_slots_by_date_with_bill_num(self, orders):
        """按日期聚合 mine 格子并带上订单号，返回 {date: [{'place','time','billNum'}]}。"""
        grouped = {}
        for order in orders or []:
            if str(order.get("showStatus", "")) != "0":
                continue
            if str(order.get("prestatus", "")).strip() in ("取消", "已取消"):
                continue
            bill_num = str(order.get("billNum", "")).strip()
            if not bill_num:
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
                    grouped.setdefault(date_str, []).append((place, cur.strftime("%H:%M"), bill_num))
                    cur += timedelta(hours=1)

        result = {}
        for d, slots in grouped.items():
            seen = set()
            unique = []
            for p, t, b in slots:
                key = (p, t, b)
                if key in seen:
                    continue
                seen.add(key)
                unique.append((p, t, b))
            result[d] = [
                {"place": p, "time": t, "billNum": b}
                for p, t, b in sorted(unique, key=lambda x: (int(x[0]) if str(x[0]).isdigit() else 999, x[1]))
            ]
        return result

    def get_matrix(self, date_str, include_mine_overlay=True, request_timeout=None, bypass_cache=False):
        ctx = get_runtime_request_context()
        quiet_info = quiet_window_block_info(
            "matrix_query",
            requester_task_id=ctx.get("task_id"),
            owner_allowed=bool(ctx.get("owner")),
        )
        if quiet_info:
            return {"error": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")}
        cache_key = (str(date_str or ''), bool(include_mine_overlay))
        now_ts = time.time()
        with self._matrix_cache_lock:
            cache_hit = self._matrix_cache.get(cache_key)
        if (
            not bypass_cache
            and cache_hit
            and (now_ts - float(cache_hit.get('ts', 0.0))) <= float(self._matrix_cache_window_s)
        ):
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
            matrix_timeout = max(0.5, float(request_timeout if request_timeout is not None else CONFIG.get('matrix_timeout_seconds', 3.0) or 3.0))
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

            # 预约窗口状态：past / unlocked / future
            window_status = self.get_date_window_status(date_str)

            def classify_status(base_label: str) -> str:
                """
                根据基础状态 + 日期窗口，得到语义化状态：
                - available: 当前可下单
                - mine: 本账号已预订（仅在解锁窗口内）
                - booked: 他人已占用/不可用
                - locked: 未开放/系统锁定（对当前账号不可操作）
                """
                s = str(base_label or "").lower()
                if window_status == "unlocked":
                    if s == "available":
                        return "available"
                    if s == "locked":
                        # 解锁窗口内的 locked 更符合“本人已占”的表现（根据当前观测样本）
                        return "mine"
                    if s == "booked":
                        return "booked"
                    return "booked"
                elif window_status == "future":
                    # 未解锁窗口：locked/available 代表尚未开放的坑位，统一视为锁定；
                    # booked 仍表示未来也抢不到的坑（系统/他人长期占用）。
                    if s in ("available", "locked"):
                        return "locked"
                    if s == "booked":
                        return "booked"
                    return "locked"
                else:  # past
                    if s == "locked":
                        # 过去日期上的 locked 多半是本人历史订单，保留为 mine_past 语义；
                        # 对大多数逻辑与 mine 等价，前端可按 mine 渲染。
                        return "mine"
                    if s == "available":
                        return "available"
                    return "booked"

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

                    try:
                        state_int = int(s)
                    except Exception:
                        state_int = -999

                    # 基础标签：仅区分 available / locked / booked
                    if state_int == 1:
                        base_label = "available"
                    elif state_int in locked_state_values:
                        base_label = "locked"
                    else:
                        base_label = "booked"

                    # 结合日期窗口得到语义化状态
                    status_map[t] = classify_status(base_label)

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

    def submit_order(self, date_str, selected_items, submit_profile=None):
        """
        提交预订订单。
        关键修正：不再单纯依赖 reservationPlace 返回的 "msg":"success"，
        而是提交完成后重新拉取矩阵，确认选中场次的状态是否从 available 变为 booked。
        """
        ctx = get_runtime_request_context()
        quiet_info = quiet_window_block_info(
            "submit_order",
            requester_task_id=ctx.get("task_id"),
            owner_allowed=bool(ctx.get("owner")),
        )
        if quiet_info:
            return {
                "status": "quiet_window_blocked",
                "msg": quiet_info.get("msg"),
                "success_items": [],
                "failed_items": normalize_booking_items(selected_items),
                "run_metric": {
                    "request_mode": "quiet_window_blocked",
                    "submit_req_count": 0,
                    "submit_success_resp_count": 0,
                    "submit_retry_count": 0,
                    "confirm_matrix_poll_count": 0,
                    "confirm_orders_poll_count": 0,
                    "verify_exception_count": 0,
                    "rate_limited": False,
                    "transport_error": False,
                    "business_fail_msg": "",
                    "server_msg_raw": "",
                },
                "quiet_window": quiet_info.get("quiet_window"),
            }
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"

        profile_name = str(submit_profile or "").strip()
        profile_settings = get_submit_profile_settings(profile_name)

        if bool(profile_settings.get("minimal_direct_mode", False)):
            return self.submit_order_minimal(date_str, selected_items, submit_profile=profile_name)

        def cfg_get(key, default=None):
            if key in profile_settings:
                return profile_settings.get(key)
            return CONFIG.get(key, default)

        results = []
        try:
            degrade_batch_size = int(cfg_get("submit_batch_size", 3))
        except Exception:
            degrade_batch_size = 3
        degrade_batch_size = max(1, min(9, degrade_batch_size))
        configured_initial_batch_size = int(cfg_get("initial_submit_batch_size", cfg_get("submit_batch_size", 3)) or 3)
        initial_batch_size = max(1, min(9, configured_initial_batch_size))
        submit_strategy_mode = str(cfg_get("submit_strategy_mode", "adaptive") or "adaptive").strip().lower()
        if submit_strategy_mode not in ("adaptive", "fixed"):
            submit_strategy_mode = "adaptive"
        batch_retry_times = int(cfg_get("batch_retry_times", 2))
        batch_retry_interval = float(cfg_get("batch_retry_interval", cfg_get("retry_interval", 0.5)))
        batch_min_interval = float(cfg_get("batch_min_interval", 0.8))
        refill_window_seconds = float(cfg_get("refill_window_seconds", 8.0))
        submit_timeout_seconds = max(0.5, float(cfg_get("submit_timeout_seconds", 4.0) or 4.0))
        submit_split_retry_times = max(0, min(3, int(cfg_get("submit_split_retry_times", 1) or 1)))
        submit_timeout_backoff_seconds = max(0.5, float(cfg_get("submit_timeout_backoff_seconds", 2.5) or 2.5))
        fast_lane_enabled = bool(cfg_get("fast_lane_enabled", True))
        fast_lane_seconds = max(0.0, float(cfg_get("fast_lane_seconds", 2.0) or 2.0))
        too_fast_cooldown_seconds = max(0.3, float(cfg_get("too_fast_cooldown_seconds", 1.4) or 1.4))
        too_fast_force_single_item_on_batch_fail = bool(cfg_get("too_fast_force_single_item_on_batch_fail", False))
        fast_lane_deadline_ts = time.time() + fast_lane_seconds if fast_lane_enabled else 0.0
        run_metric = {
            "submit_req_count": 0,
            "submit_success_resp_count": 0,
            "submit_retry_count": 0,
            "fast_lane_used_seconds": 0,
            "confirm_matrix_poll_count": 0,
            "confirm_orders_poll_count": 0,
            "t_confirm_ms": None,
            "verify_exception_count": 0,
            "effective_initial_batch_size": 0,
            "submit_strategy_mode": submit_strategy_mode,
            "retry_budget_total": 0,
            "retry_budget_used": 0,
            "adaptive_small_n_merge_applied": False,
            "submit_grouping_mode": str(cfg_get("submit_grouping_mode", "smart") or "smart"),
            "place_first_grouping_applied": False,
            "submit_profile": profile_name or "default",
            "effective_fast_lane_enabled": bool(fast_lane_enabled),
            "effective_fast_lane_seconds": float(fast_lane_seconds),
            "effective_batch_min_interval": float(batch_min_interval),
            "effective_too_fast_cooldown_seconds": float(too_fast_cooldown_seconds),
            "request_mode": "legacy_batched",
            "t_first_post_ms": None,
            "rate_limited": False,
            "transport_error": False,
            "business_fail_msg": "",
            "server_msg_raw": "",
        }

        print(
            f"🧭 [批次策略] 首批=按配置 initial_submit_batch_size→{initial_batch_size}；"
            f"降级=按配置 submit_batch_size→{degrade_batch_size}；策略={submit_strategy_mode}；本次选择={len(selected_items)}"
        )
        print(
            f"⏱️ [提交超时] submit_timeout={submit_timeout_seconds}s, split_retry_times={submit_split_retry_times}"
        )
        if profile_name:
            print(f"🧩 [提交模板] 使用 submit_profile={profile_name}")

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
            # 统一将“操作过快 / 频控 / 网关异常 / 暂时不可用 / 数据错误(可重试提示)”视为可重试类错误，
            # 由后续的 batch_retry_times + retry_budget_total 控制重试上限，避免自杀式雪崩。
            keywords = [
                "操作过快", "稍后重试", "请求过于频繁", "too fast", "频繁",
                "404 not found", "nginx", "bad gateway", "service unavailable",
                "502", "503", "504", "timeout", "timed out", "connection reset",
                "max retries exceeded", "temporarily unavailable", "non-json", "非json",
                "暂时不可用", "网关异常", "下单接口暂时不可用", "空响应",
                "数据错误", "请重试",
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

        def is_too_fast_fail(msg):
            text = str(msg or "").lower()
            keywords = ["操作过快", "请求过于频繁", "too fast", "频繁"]
            return any(k in text for k in keywords)

        def maybe_sleep_non_retryable(default_interval):
            if fast_lane_enabled and time.time() < fast_lane_deadline_ts:
                return
            time.sleep(max(default_interval, cfg_get("retry_interval", 0.5)))

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

        effective_initial_batch_size = initial_batch_size
        if submit_strategy_mode == "adaptive":
            n_items = len(submit_items)
            target_batches = max(1, min(6, int(cfg_get("submit_adaptive_target_batches", 2) or 2)))
            adaptive_min = max(1, min(9, int(cfg_get("submit_adaptive_min_batch_size", 1) or 1)))
            adaptive_max = max(adaptive_min, min(9, int(cfg_get("submit_adaptive_max_batch_size", 3) or 3)))
            merge_small_n = max(1, min(9, int(cfg_get("submit_adaptive_merge_small_n", 2) or 2)))
            merge_same_time_only = bool(cfg_get("submit_adaptive_merge_same_time_only", True))
            if n_items > 0:
                computed = (n_items + target_batches - 1) // target_batches
                effective_initial_batch_size = max(adaptive_min, min(adaptive_max, computed))

                can_merge_small_n = n_items <= merge_small_n
                if can_merge_small_n and merge_same_time_only:
                    unique_times = {str(it.get("time")) for it in submit_items if isinstance(it, dict)}
                    can_merge_small_n = len(unique_times) <= 1
                if can_merge_small_n:
                    effective_initial_batch_size = n_items
                    run_metric["adaptive_small_n_merge_applied"] = True

            effective_initial_batch_size = min(effective_initial_batch_size, degrade_batch_size)

        def prioritize_items_by_place_completeness(items):
            """
            以“完整场地优先”重排提交项：
            - 同一场地覆盖目标时段越完整，优先级越高；
            - 完整度相同则保持用户在本次选择中的场地先后；
            - 场地内按用户选择的时间先后提交。
            该规则适用于 2x2、3x3 等“整场优先”场景。
            """
            normalized = [
                {"place": str(it.get("place")), "time": str(it.get("time"))}
                for it in (items or [])
                if isinstance(it, dict) and it.get("place") and it.get("time")
            ]
            if len(normalized) <= 1:
                return normalized

            place_order = []
            time_order = []
            by_place = {}
            for it in normalized:
                p = it["place"]
                t = it["time"]
                if p not in by_place:
                    by_place[p] = set()
                    place_order.append(p)
                by_place[p].add(t)
                if t not in time_order:
                    time_order.append(t)

            if len(place_order) <= 1 or len(time_order) <= 1:
                return normalized

            full_time_set = set(time_order)
            place_rank = []
            for idx, p in enumerate(place_order):
                covered = by_place.get(p, set())
                complete = 1 if covered >= full_time_set else 0
                coverage = len(covered)
                # complete(1) > partial(0), coverage 越多越靠前，最后按用户选择顺序稳定排序
                place_rank.append((p, complete, coverage, idx))

            place_rank.sort(key=lambda x: (-x[1], -x[2], x[3]))
            ranked_places = [x[0] for x in place_rank]
            ranked_place_set = set(ranked_places)

            rebuilt = []
            seen = set()
            for p in ranked_places:
                for t in time_order:
                    key = (p, t)
                    if key in seen:
                        continue
                    if t in by_place.get(p, set()):
                        rebuilt.append({"place": p, "time": t})
                        seen.add(key)

            # 兜底：理论上不会命中，防止异常输入导致条目丢失
            if len(rebuilt) < len(normalized):
                for it in normalized:
                    key = (it["place"], it["time"])
                    if key not in seen:
                        rebuilt.append({"place": it["place"], "time": it["time"]})
                        seen.add(key)

            if is_verbose_logs_enabled() and ranked_places and ranked_place_set:
                print(
                    f"🧩 [完整场优先] 场地优先级: {ranked_places}; "
                    f"目标时段: {time_order}"
                )
            return rebuilt

        submit_items = prioritize_items_by_place_completeness(submit_items)

        submit_grouping_mode = str(cfg_get("submit_grouping_mode", "smart") or "smart").strip().lower()
        if submit_grouping_mode not in ("smart", "place", "timeslot"):
            submit_grouping_mode = "smart"
        place_first_grouping = False
        if len(submit_items) > 1:
            unique_times = {str(it.get("time")) for it in submit_items if isinstance(it, dict)}
            unique_places = {str(it.get("place")) for it in submit_items if isinstance(it, dict)}
            if submit_grouping_mode == "place":
                place_first_grouping = True
            elif submit_grouping_mode == "timeslot":
                place_first_grouping = False
            else:
                place_first_grouping = (len(unique_times) > 1 and len(unique_places) > 1)
        if place_first_grouping:
            submit_items = sorted(
                submit_items,
                key=lambda it: (
                    int(str(it.get("place"))) if str(it.get("place")).isdigit() else 999,
                    str(it.get("time")),
                ),
            )
            run_metric["place_first_grouping_applied"] = True

        preblocked_items = []
        multi_item_retry_balance_enabled = bool(cfg_get("multi_item_retry_balance_enabled", True))
        multi_item_batch_retry_times_cap = max(0, min(3, int(cfg_get("multi_item_batch_retry_times_cap", 1) or 1)))
        effective_batch_retry_times = batch_retry_times
        if multi_item_retry_balance_enabled and len(submit_items) > 1 and batch_retry_times > multi_item_batch_retry_times_cap:
            effective_batch_retry_times = multi_item_batch_retry_times_cap
            print(
                f"⚖️ [重试均衡] 多项目提交({len(submit_items)})，batch_retry_times: {batch_retry_times} -> {effective_batch_retry_times}"
            )
        run_metric["effective_batch_retry_times"] = int(effective_batch_retry_times)
        run_metric["effective_initial_batch_size"] = int(effective_initial_batch_size)

        retry_budget_total = 0
        retry_budget_used = 0
        if multi_item_retry_balance_enabled and len(submit_items) > 1:
            retry_budget_total = max(0, min(20, int(cfg_get("multi_item_retry_total_budget", 3) or 3)))
        run_metric["retry_budget_total"] = int(retry_budget_total)

        def _can_consume_retry_budget():
            nonlocal retry_budget_used
            if retry_budget_total <= 0:
                return False
            if retry_budget_used >= retry_budget_total:
                return False
            retry_budget_used += 1
            run_metric["retry_budget_used"] = int(retry_budget_used)
            return True

        same_time_limit = int(cfg_get("same_time_precheck_limit", 0) or 0)
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

        # 首轮提交：先将选择拆分为“合法批次”，再逐批按原有策略发送
        legal_batches = group_booking_items_into_legal_batches(submit_items, cfg_get, profile_name=profile_name)
        if not legal_batches and submit_items:
            # 理论上不会命中，只是兜底
            legal_batches = [submit_items]

        for batch_index, batch in enumerate(legal_batches, start=1):
            print(f"📦 正在提交分批订单 ({batch_index}): {batch}")

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
            for attempt in range(effective_batch_retry_times + 1):
                try:
                    if run_metric.get("t_first_post_ms") is None:
                        run_metric["t_first_post_ms"] = 0
                    run_metric["submit_req_count"] += 1
                    resp = self.session.post(
                        url, headers=self.headers, data=body, timeout=submit_timeout_seconds, verify=False
                    )

                    try:
                        resp_data = resp.json()
                    except ValueError:
                        resp_data = None

                    if is_verbose_logs_enabled():
                        print(
                                f"📨 [submit_order调试] 批次 {batch_index} 响应: {resp.text}"
                        )

                    if resp_data and resp_data.get("msg") == "success":
                        run_metric["submit_success_resp_count"] += 1
                        final_result = {"status": "success", "batch": batch}
                        break

                    fail_msg = None
                    if isinstance(resp_data, dict):
                        fail_msg = resp_data.get("data") or resp_data.get("msg")
                    if not fail_msg:
                        fail_msg = resp.text
                    fail_msg = normalize_fail_message(fail_msg)
                    run_metric["server_msg_raw"] = str(fail_msg or "")[:200]
                    run_metric["rate_limited"] = bool(run_metric.get("rate_limited") or is_too_fast_fail(fail_msg))
                    if not is_too_fast_fail(fail_msg):
                        run_metric["business_fail_msg"] = str(fail_msg or "")[:200]

                    if is_too_fast_fail(fail_msg) and too_fast_force_single_item_on_batch_fail and len(batch) > 1:
                        print(
                            f"🧯 [too-fast降级] 批次 {batch_index} 命中操作过快，"
                            f"切换单项重提，冷却{round(too_fast_cooldown_seconds, 2)}s"
                        )
                        single_fail = []
                        for idx_item, one in enumerate(batch):
                            if idx_item > 0:
                                time.sleep(too_fast_cooldown_seconds + random.uniform(0.05, 0.35))
                            try:
                                one_field_info = []
                                one_total = 0
                                p_num = one["place"]
                                start = one["time"]
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

                                one_field_info.append({
                                    "day": date_str,
                                    "oldMoney": price,
                                    "startTime": start,
                                    "endTime": end,
                                    "placeShortName": place_short,
                                    "name": place_name,
                                    "stageTypeShortName": "ymq",
                                    "newMoney": price,
                                })
                                one_total += price

                                one_info_str = urllib.parse.quote(json.dumps(one_field_info, separators=(",", ":"), ensure_ascii=False))
                                one_type_encoded = urllib.parse.quote("羽毛球")
                                one_body = (
                                    f"token={self.token}&shopNum={CONFIG['auth']['shop_num']}&fieldinfo={one_info_str}&"
                                    f"cardStId={CONFIG['auth']['card_st_id']}&oldTotal={one_total}.00&cardPayType=0&"
                                    f"type={one_type_encoded}&offerId=&offerType=&total={one_total}.00&premerother=&"
                                    f"cardIndex={CONFIG['auth']['card_index']}"
                                )
                                run_metric["submit_req_count"] += 1
                                one_resp = self.session.post(url, headers=self.headers, data=one_body, timeout=submit_timeout_seconds, verify=False)
                                one_data = one_resp.json() if one_resp.text else None
                                if not (isinstance(one_data, dict) and one_data.get("msg") == "success"):
                                    single_fail.append(one)
                                else:
                                    run_metric["submit_success_resp_count"] += 1
                            except Exception:
                                single_fail.append(one)

                        if not single_fail:
                            final_result = {"status": "success", "batch": batch}
                        elif len(single_fail) < len(batch):
                            final_result = {"status": "partial", "msg": fail_msg, "batch": single_fail}
                        else:
                            final_result = {"status": "fail", "msg": fail_msg, "batch": single_fail}
                        break

                    if attempt < effective_batch_retry_times and is_retryable_fail(fail_msg):
                        can_retry = True
                        if retry_budget_total > 0:
                            can_retry = _can_consume_retry_budget()
                        if can_retry:
                            sleep_s = batch_retry_interval * (2 ** attempt) + random.uniform(0, 0.25)
                            if is_too_fast_fail(fail_msg):
                                sleep_s = max(sleep_s, too_fast_cooldown_seconds + random.uniform(0.05, 0.35))
                        else:
                            sleep_s = None
                        if sleep_s is not None:
                            print(
                                f"⏳ 批次 {batch_index} 命中可重试错误，"
                                f"{round(sleep_s, 2)}s 后重试 ({attempt + 1}/{effective_batch_retry_times})"
                            )
                            run_metric["submit_retry_count"] += 1
                            time.sleep(sleep_s)
                            continue

                    if is_too_fast_fail(fail_msg) and too_fast_force_single_item_on_batch_fail and len(batch) > 1:
                        print(
                            f"🧯 [too-fast降级] 批次 {batch_index} 命中操作过快，"
                            f"切换单项重提，冷却{round(too_fast_cooldown_seconds, 2)}s"
                        )
                        single_fail = []
                        for idx_item, one in enumerate(batch):
                            if idx_item > 0:
                                time.sleep(too_fast_cooldown_seconds + random.uniform(0.05, 0.35))
                            try:
                                one_field_info = []
                                one_total = 0
                                p_num = one["place"]
                                start = one["time"]
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

                                one_field_info.append({
                                    "day": date_str,
                                    "oldMoney": price,
                                    "startTime": start,
                                    "endTime": end,
                                    "placeShortName": place_short,
                                    "name": place_name,
                                    "stageTypeShortName": "ymq",
                                    "newMoney": price,
                                })
                                one_total += price

                                one_info_str = urllib.parse.quote(json.dumps(one_field_info, separators=(",", ":"), ensure_ascii=False))
                                one_type_encoded = urllib.parse.quote("羽毛球")
                                one_body = (
                                    f"token={self.token}&shopNum={CONFIG['auth']['shop_num']}&fieldinfo={one_info_str}&"
                                    f"cardStId={CONFIG['auth']['card_st_id']}&oldTotal={one_total}.00&cardPayType=0&"
                                    f"type={one_type_encoded}&offerId=&offerType=&total={one_total}.00&premerother=&"
                                    f"cardIndex={CONFIG['auth']['card_index']}"
                                )
                                run_metric["submit_req_count"] += 1
                                one_resp = self.session.post(url, headers=self.headers, data=one_body, timeout=submit_timeout_seconds, verify=False)
                                one_data = one_resp.json() if one_resp.text else None
                                if not (isinstance(one_data, dict) and one_data.get("msg") == "success"):
                                    single_fail.append(one)
                                else:
                                    run_metric["submit_success_resp_count"] += 1
                            except Exception:
                                single_fail.append(one)

                        if not single_fail:
                            final_result = {"status": "success", "batch": batch}
                        else:
                            final_result = {"status": "fail", "msg": fail_msg, "batch": single_fail}
                        break

                    # 命中“可重试/规则异常”时，按配置分批降级重提一次
                    if len(batch) > degrade_batch_size and should_degrade(fail_msg):
                        print(f"↘️ 批次 {batch_index} 降级重提: size {len(batch)} -> {degrade_batch_size}")
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
                                maybe_sleep_non_retryable(batch_min_interval)
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
                    run_metric["transport_error"] = True
                    if attempt < effective_batch_retry_times:
                        can_retry = True
                        if retry_budget_total > 0:
                            can_retry = _can_consume_retry_budget()
                        if can_retry:
                            err_str = str(e).lower()
                            is_timeout_err = "timed out" in err_str or "timeout" in err_str or "read" in err_str
                            sleep_s = submit_timeout_backoff_seconds if is_timeout_err else batch_retry_interval
                            if is_timeout_err:
                                print(
                                    f"⏳ 批次 {batch_index} 提交超时，退避 {round(sleep_s, 2)}s 后重试 "
                                    f"({attempt + 1}/{effective_batch_retry_times})，避免触发操作过快"
                                )
                            else:
                                print(
                                    f"⏳ 批次 {batch_index} 异常，{round(sleep_s, 2)}s 后重试 "
                                    f"({attempt + 1}/{effective_batch_retry_times}): {e}"
                                )
                            run_metric["submit_retry_count"] += 1
                            time.sleep(sleep_s)
                            continue
                    final_result = {"status": "error", "msg": str(e), "batch": batch}
                    break

            results.append(final_result or {"status": "error", "msg": "未知错误", "batch": batch})

            # 批次间最小停顿，防止触发“操作过快”
            maybe_sleep_non_retryable(batch_min_interval)

        # 对失败项做补提（窗口内仅补提仍 available 的项）
        try:
            skip_refill_for_too_fast = bool(cfg_get("too_fast_skip_refill_in_same_request", True)) and any(
                r.get("status") == "fail" and is_too_fast_fail(r.get("msg")) for r in results
            )
            if skip_refill_for_too_fast:
                print("⏭️ [补提] 命中操作过快/频繁，按配置跳过同请求内补提，避免触发连续风控")
            refill_deadline = time.time() if skip_refill_for_too_fast else (time.time() + max(0.0, refill_window_seconds))
            while time.time() < refill_deadline:
                failed_items = []
                for r in results:
                    if r.get("status") in ("fail", "error", "partial"):
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
                        run_metric["submit_req_count"] += 1
                        resp = self.session.post(url, headers=self.headers, data=body, timeout=submit_timeout_seconds, verify=False)
                        resp_data = resp.json() if resp.text else None
                        if isinstance(resp_data, dict) and resp_data.get("msg") == "success":
                            run_metric["submit_success_resp_count"] += 1
                            results.append({"status": "success", "batch": batch})
                        else:
                            msg = resp_data.get("data") if isinstance(resp_data, dict) else resp.text
                            results.append({"status": "fail", "msg": msg, "batch": batch})
                    except Exception as e:
                        results.append({"status": "error", "msg": str(e), "batch": batch})
                    maybe_sleep_non_retryable(batch_min_interval)

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
        api_success_count = sum(1 for r in results if r.get("status") in ("success", "partial"))
        verify_success_count = None
        verify_success_items = []
        verify_failed_items = []
        orders_query_ok = False
        orders_query_error = ""
        orders_res = {"error": "按配置跳过"}
        confirm_started_ts = time.time()
        if api_success_count <= 0:
            verify = {"error": "无success响应，跳过提交后验证"}
        else:
            try:
                order_timeout_s = max(0.5, float(cfg_get('order_query_timeout_seconds', 2.5) or 2.5))
                order_max_pages = max(1, min(10, int(cfg_get('order_query_max_pages', 2) or 2)))
                verify_matrix_timeout_s = max(0.3, float(cfg_get('post_submit_verify_matrix_timeout_seconds', 0.8) or 0.8))
                verify_matrix_recheck_times = max(0, min(8, int(cfg_get('post_submit_verify_matrix_recheck_times', 3) or 3)))

                verify = {"error": "未执行"}
                for _ in range(verify_matrix_recheck_times + 1):
                    run_metric["confirm_matrix_poll_count"] += 1
                    verify = self.get_matrix(
                        date_str,
                        include_mine_overlay=False,
                        request_timeout=verify_matrix_timeout_s,
                    )

                    if not (isinstance(verify, dict) and not verify.get("error")):
                        continue

                    v_matrix = verify.get("matrix") or {}
                    current_success = []
                    current_failed = []
                    for item in submit_items:
                        p = str(item.get("place"))
                        t = item.get("time")
                        status = v_matrix.get(p, {}).get(t, "N/A")
                        if status in ("booked", "mine"):
                            current_success.append({"place": p, "time": t})
                        else:
                            current_failed.append({"place": p, "time": t})

                    if not current_failed:
                        verify_success_items = current_success
                        verify_failed_items = []
                        verify_success_count = len(current_success)
                        break

                if verify_success_count is None and isinstance(verify, dict) and not verify.get("error"):
                    v_matrix = verify.get("matrix") or {}
                    verify_states = []
                    matrix_failed_items = []
                    for item in submit_items:
                        p = str(item.get("place"))
                        t = item.get("time")
                        status = v_matrix.get(p, {}).get(t, "N/A")
                        verify_states.append(f"{p}号{t}={status}")
                        if status not in ("booked", "mine"):
                            matrix_failed_items.append({"place": p, "time": t})

                    verify_orders_only_on_partial = bool(cfg_get('post_submit_verify_orders_on_matrix_partial_only', True))
                    needs_orders_query = bool(matrix_failed_items) if verify_orders_only_on_partial else True

                    if needs_orders_query:
                        skip_sync_orders_query = bool(cfg_get('post_submit_skip_sync_orders_query', True))
                        if skip_sync_orders_query:
                            orders_res = {"error": "按配置跳过同步订单查询"}
                        else:
                            orders_res = {"error": "未执行"}

                            def _fetch_orders():
                                nonlocal orders_res
                                orders_res = self.get_place_orders(max_pages=order_max_pages, timeout_s=order_timeout_s)

                            t_orders = threading.Thread(target=_fetch_orders, daemon=True)
                            t_orders.start()
                            join_timeout_s = max(0.1, float(cfg_get('post_submit_orders_join_timeout_seconds', 0.3) or 0.3))
                            t_orders.join(timeout=join_timeout_s)
                            run_metric["confirm_orders_poll_count"] += 1
                            if isinstance(orders_res, dict) and orders_res.get("error") == "未执行":
                                orders_res = {"error": f"订单查询超时(>{join_timeout_s}s)"}

                    mine_slots = set()
                    if "error" not in orders_res:
                        mine_slots = self._extract_mine_slots(orders_res.get("data", []), date_str)
                        orders_query_ok = True
                    else:
                        orders_query_error = str(orders_res.get("error") or "")
                        if orders_query_error not in ("按配置跳过同步订单查询", "按配置跳过"):
                            print(f"🧾 [提交后验证调试] 订单拉取失败，mine校验降级为矩阵状态: {orders_query_error}")

                    verify_success_items = []
                    verify_failed_items = []
                    for item in submit_items:
                        p = str(item.get("place"))
                        t = item.get("time")
                        status = v_matrix.get(p, {}).get(t, "N/A")
                        mine_hit = (p, t) in mine_slots
                        success = (mine_hit or status in ("booked", "mine")) if orders_query_ok else status in ("booked", "mine")
                        if success:
                            verify_success_items.append({"place": p, "time": t})
                        else:
                            verify_failed_items.append({"place": p, "time": t})
                    verify_success_count = len(verify_success_items)
                elif verify_success_count is None:
                    print(
                        f"🧾 [提交后验证调试] 获取矩阵失败: "
                        f"{verify.get('error') if isinstance(verify, dict) else verify}"
                    )

                    if preblocked_items:
                        verify_failed_items.extend(preblocked_items)
            except Exception as e:
                run_metric["verify_exception_count"] = int(run_metric.get("verify_exception_count") or 0) + 1
                print(f"🧾 [提交后验证调试] 异常: {e}")

        run_metric["t_confirm_ms"] = int(max(0.0, time.time() - confirm_started_ts) * 1000)
        if fast_lane_enabled:
            run_metric["fast_lane_used_seconds"] = int(max(0.0, min(fast_lane_seconds, time.time() - (fast_lane_deadline_ts - fast_lane_seconds))) * 1000) / 1000.0

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
            return {"status": "fail", "msg": msg, "run_metric": run_metric}

        cross_instance_suspected = verify_ok and api_success_count == 0 and success_count > 0

        if verify_ok and api_success_count > 0 and success_count == denominator:
            return {
                "status": "success",
                "msg": "全部下单成功",
                "success_items": verify_success_items,
                "failed_items": verify_failed_items,
                "run_metric": run_metric,
            }
        elif verify_ok and api_success_count > 0 and success_count > 0:
            return {
                "status": "partial",
                "msg": f"部分成功 ({success_count}/{denominator})",
                "success_items": verify_success_items,
                "failed_items": verify_failed_items,
                "run_metric": run_metric,
            }
        else:
            # 未收到任何提交成功响应，但校验命中 mine，疑似并发实例下单导致的“归因串扰”
            if cross_instance_suspected:
                msg = "检测到我的订单已占位，但本进程提交未收到 success，可能由并发实例下单导致；本任务按失败处理。"
            elif api_success_count > 0 and (not verify_ok or verify_success_count == 0):
                allow_verify_pending = bool(cfg_get("post_submit_treat_verify_timeout_as_retry", True))
                if allow_verify_pending and ((not orders_query_ok) or (not verify_ok)):
                    pending_reason = orders_query_error or ("矩阵校验超时/失败" if not verify_ok else "订单校验未完成")
                    return {
                        "status": "verify_pending",
                        "msg": f"提交已返回success，但验证尚未收敛({pending_reason})，将快速复核。",
                        "success_items": verify_success_items,
                        "failed_items": verify_failed_items,
                        "run_metric": run_metric,
                    }
                if not verify_ok:
                    msg = "下单接口返回 success，但提交后状态验证失败（网络/服务波动），请以官方系统为准。"
                else:
                    msg = "接口返回 success，但场地状态未变化，请在微信小程序确认或检查参数。"
            else:
                fail_msgs = [str((r.get("msg") or "")).strip() for r in results if r.get("status") in ("fail", "error")]
                fail_msgs = [m for m in fail_msgs if m]
                if fail_msgs:
                    priority_keywords = ["数据错误", "规则", "上限", "操作过快", "频繁", "超时", "timeout", "网关"]
                    def _score(m):
                        m_lower = m.lower()
                        for idx, kw in enumerate(priority_keywords):
                            if kw in m_lower or kw in m:
                                return idx
                        return len(priority_keywords)
                    msg = sorted(fail_msgs, key=lambda x: (_score(x), len(x)))[0]
                else:
                    first_fail = results[0] if results else {"msg": "无数据"}
                    msg = first_fail.get("msg")
            return {
                "status": "fail",
                "msg": msg,
                "success_items": verify_success_items,
                "failed_items": verify_failed_items,
                "run_metric": run_metric,
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

    def _task_should_enter_quiet_window(self, task):
        if not isinstance(task, dict):
            return False
        if not bool(task.get('enabled', True)):
            return False
        cfg = task.get('config') if isinstance(task.get('config'), dict) else {}
        return is_direct_task_config(cfg)

    def _compute_next_run_datetime(self, task, now=None):
        if not isinstance(task, dict):
            return None
        now_dt = now if isinstance(now, datetime) else datetime.now()
        run_time = str(task.get('run_time') or '00:00:00')
        if len(run_time) == 5:
            run_time += ':00'
        try:
            hh, mm, ss = [int(x) for x in run_time.split(':')[:3]]
        except Exception:
            hh, mm, ss = 0, 0, 0
        base = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        t_type = str(task.get('type') or 'daily').strip()
        if t_type in ('daily', 'once'):
            if base <= now_dt:
                base = base + timedelta(days=1)
            return base
        if t_type == 'weekly':
            target_weekday = int(task.get('weekly_day', 0) or 0)
            diff = target_weekday - now_dt.weekday()
            if diff < 0:
                diff += 7
            elif diff == 0 and base <= now_dt:
                diff += 7
            return base + timedelta(days=diff)
        if base <= now_dt:
            base = base + timedelta(days=1)
        return base

    def reset_refill_scheduler_baseline(self):
        now_ts = time.time()
        for t in list(self.refill_tasks):
            if not bool(t.get('enabled', True)):
                continue
            try:
                tid = int(t.get('id', 0))
            except Exception:
                continue
            self._refill_last_run[tid] = now_ts

    def _restore_runtime_services_after_quiet_release(self, reason):
        snapshot = quiet_window_snapshot()
        if not snapshot.get('active'):
            return snapshot
        released = release_quiet_window(reason=reason)
        self.reset_refill_scheduler_baseline()
        self.refresh_schedule()
        schedule_health_check()
        return released

    def process_quiet_window_tick(self):
        now_ts = time.time()
        snapshot = quiet_window_snapshot()
        if snapshot.get('active'):
            if _quiet_window_is_expired(snapshot, now_ts=now_ts):
                self._restore_runtime_services_after_quiet_release("ttl-expired")
                return
            state = str(snapshot.get('state') or '')
            owner_task_id = snapshot.get('owner_task_id')
            if state == 'fire_window' and owner_task_id is not None and not self.is_task_running(owner_task_id):
                mark_quiet_window_recovering(owner_task_id, reason="owner-finished")
                return
            if state == 'recovering':
                recover_until_ts = float(snapshot.get('recover_until_ts') or 0.0)
                if recover_until_ts > 0 and now_ts >= recover_until_ts:
                    self._restore_runtime_services_after_quiet_release("recovering-finished")
                return
            return

        now_dt = datetime.now()
        candidate_task = None
        candidate_fire_at = None
        candidate_delta = None
        for task in self.tasks:
            if not self._task_should_enter_quiet_window(task):
                continue
            next_run_dt = self._compute_next_run_datetime(task, now=now_dt)
            if next_run_dt is None:
                continue
            delta_s = (next_run_dt - now_dt).total_seconds()
            if delta_s < 0 or delta_s > QUIET_WINDOW_PREQUIET_SECONDS:
                continue
            if candidate_delta is None or delta_s < candidate_delta:
                candidate_task = task
                candidate_fire_at = next_run_dt.timestamp()
                candidate_delta = delta_s
        if candidate_task is not None:
            enter_quiet_window(
                candidate_task.get('id'),
                candidate_fire_at,
                reason=f"prepare-task-{candidate_task.get('id')}",
            )

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
        quiet_snapshot = quiet_window_snapshot()
        quiet_active = bool(quiet_snapshot.get('active')) and _quiet_window_matches_scope(quiet_snapshot)
        quiet_owner_id = str(quiet_snapshot.get('owner_task_id') or '') if quiet_snapshot.get('owner_task_id') is not None else ''
        current_task_id = str(task_id) if task_id is not None else ''
        is_owner_task = bool(quiet_active and current_task_id and current_task_id == quiet_owner_id)
        if quiet_active and not is_owner_task:
            log(f"🔇 [quiet-window] 自动任务{task_id}已静默跳过，owner={quiet_owner_id or '-'}")
            return False
        if task_id is not None and not self._try_mark_task_running(task_id):
            log(f"⏭️ [任务锁] 任务{task_id}仍在执行，跳过本次触发")
            return False
        try:
            if self._task_should_enter_quiet_window(task):
                if not quiet_active:
                    enter_quiet_window(task_id, time.time(), reason=f"late-owner-start-{task_id}")
                mark_quiet_window_fire(task_id)
                is_owner_task = True
            with runtime_request_context("task_execute", task_id=task_id, owner=is_owner_task):
                self.execute_task(task)
            return True
        finally:
            if is_owner_task:
                mark_quiet_window_recovering(task_id, reason="task-finally")
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
        # 启动时间：为空表示立即可运行
        task['start_at'] = str(task.get('start_at') or '').strip()
        # 临近开场前的加速轮询配置（可选）
        try:
            # 默认将加速间隔设置为不高于普通间隔，且不高于 10s
            base_fast = min(task['interval_seconds'], 10.0)
            task['fast_interval_seconds'] = max(1.0, float(task.get('fast_interval_seconds', base_fast) or base_fast))
        except Exception:
            task['fast_interval_seconds'] = min(task['interval_seconds'], 10.0)
        try:
            task['fast_window_minutes'] = max(0.0, float(task.get('fast_window_minutes', 0.0) or 0.0))
        except Exception:
            task['fast_window_minutes'] = 0.0
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
                    # 若未单独提供 fast_interval_seconds，则同步更新为新的 interval
                    if 'fast_interval_seconds' not in payload:
                        t['fast_interval_seconds'] = min(t['interval_seconds'], 10.0)
                except Exception:
                    pass
            if 'fast_interval_seconds' in payload:
                try:
                    base = min(float(payload.get('fast_interval_seconds') or t.get('interval_seconds', 10.0) or 10.0), 10.0)
                    t['fast_interval_seconds'] = max(1.0, base)
                except Exception:
                    pass
            if 'fast_window_minutes' in payload:
                try:
                    t['fast_window_minutes'] = max(0.0, float(payload.get('fast_window_minutes') or 0.0))
                except Exception:
                    pass
            if 'target_count' in payload:
                try:
                    t['target_count'] = max(1, min(MAX_TARGET_COUNT, int(payload.get('target_count') or 1)))
                except Exception:
                    pass
            if 'enabled' in payload:
                t['enabled'] = bool(payload.get('enabled'))
            if 'start_at' in payload:
                t['start_at'] = str(payload.get('start_at') or '').strip()
            if 'pushplus_tokens' in payload:
                tokens = payload.get('pushplus_tokens') or []
                if isinstance(tokens, str):
                    tokens = [x.strip() for x in tokens.split(',') if x and str(x).strip()]
                elif isinstance(tokens, list):
                    tokens = [str(x).strip() for x in tokens if str(x).strip()]
                else:
                    tokens = []
                t['pushplus_tokens'] = tokens
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
        with runtime_request_context("refill_execute", task_id=task_id, owner=False):
            date_str = str(refill_task.get('date') or '').strip()
            target_times = [str(t).strip() for t in (refill_task.get('target_times') or []) if str(t).strip()]
            candidate_places = [str(p).strip() for p in (refill_task.get('candidate_places') or []) if str(p).strip()]
            target_count = max(1, min(MAX_TARGET_COUNT, int(refill_task.get('target_count', 1) or 1)))
            tag = f"[refill#{task_id}|{source}]"
            task_tokens = refill_task.get('pushplus_tokens') or None

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

            # 先通过矩阵判断「本人已占用」的场次，减少对订单接口的依赖
            need_res = {'need_by_time': {}}
            # 不需要订单覆盖，直接依赖解锁窗口+state 的语义映射
            matrix_res = client.get_matrix(date_str, include_mine_overlay=False)
            matrix = matrix_res.get('matrix') or {}

            def _is_mine_status(status):
                """统一判断一个矩阵状态是否表示“本账号已预订”."""
                if status is None:
                    return False
                if isinstance(status, str):
                    s = status.lower()
                    return s in ('mine', 'self', 'my_booked', 'mybooked', 'booked_by_me')
                return False

            for t in target_times:
                mine_cnt = 0
                for p in candidate_places:
                    st = matrix.get(str(p), {}).get(str(t))
                    if _is_mine_status(st):
                        mine_cnt += 1
                need_res['need_by_time'][t] = max(0, target_count - mine_cnt)

            if sum(need_res['need_by_time'].values()) <= 0:
                msg = 'refill目标已满足'
                log(f"✅ {tag} {msg}")
                # 目标已满足时自动停用任务并发送一次结束通知
                try:
                    pseudo_items = []
                    for p in candidate_places:
                        for t in target_times:
                            pseudo_items.append({"place": p, "time": t})
                    refill_task['enabled'] = False
                    refill_task['last_result'] = {'status': 'stopped', 'msg': msg}
                    self.append_refill_history(refill_task, refill_task['last_result'])
                    self.save_refill_tasks()
                    if self._should_notify_refill_success(task_id):
                        end_content = f"Refill#{task_id} 目标已满足，任务自动停用。日期: {date_str}，times={target_times}, places={len(candidate_places)}"
                        short_title = self._build_short_title("已停用", date_str, pseudo_items)
                        self.send_notification(end_content)
                        self.send_wechat_notification(end_content, tokens=task_tokens, title=short_title)
                except Exception as e:
                    log(f"⚠️ [refill#{task_id}] 自动停用/通知失败: {e}")
                return {'status': 'success', 'msg': msg, 'success_items': []}

            matrix_res = client.get_matrix(date_str)
            if 'error' in matrix_res:
                msg = f"获取矩阵失败: {matrix_res.get('error')}"
                log(f"❌ {tag} {msg}")
                return {'status': 'error', 'msg': msg}
            matrix = matrix_res.get('matrix') or {}

            intent_base = {
                "target_blocks": target_count,
                "target_times": list(target_times),
                "time_preference_order": list(target_times),
                "preferred_place_min": int(refill_task.get("preferred_place_min") or 0),
                "preferred_place_max": int(refill_task.get("preferred_place_max") or 0),
            }
            allow_scatter = not bool(refill_task.get("require_consecutive"))
            solved, used_need, tier_label = solve_refill_need_tiered(
                matrix,
                candidate_places,
                intent_base,
                dict(need_res.get("need_by_time") or {}),
                allow_scatter=allow_scatter,
            )
            picks = normalize_booking_items((solved or {}).get("items") or [])

            if not picks:
                msg = f"当前无可补订组合，缺口: {need_res['need_by_time']}"
                log(f"🙈 {tag} {msg}")
                return {'status': 'fail', 'msg': msg}

            log(f"📦 {tag} 分层 tier={tier_label} used_need={used_need} 本轮提交: {picks}")
            submit_res = client.submit_order(date_str, picks, submit_profile=CONFIG.get("auto_submit_profile", "auto_minimal"))
            log(f"🧾 {tag} 本轮结果: {submit_res.get('status')} - {submit_res.get('msg')}")
            if submit_res.get('status') in ('success', 'partial') and (submit_res.get('success_items') or []):
                ok_items = submit_res.get('success_items') or []
                item_text = '、'.join([f"{it.get('place')}号{it.get('time')}" for it in ok_items[:6]])
                msg = f"Refill#{task_id}补订成功({len(ok_items)}项): {date_str} {item_text}"
                # 补订成功：每次成功都发送一次通知（频率较低，无需按分钟节流）
                self.send_notification(msg)
                short_title = self._build_short_title("已补订", date_str, ok_items)
                self.send_wechat_notification(msg, tokens=task_tokens, title=short_title)
            return submit_res

    def run_refill_scheduler_tick(self):
        now = time.time()
        for t in list(self.refill_tasks):
            if not bool(t.get('enabled', True)):
                continue
            tid = int(t.get('id', 0))
            # 启动时间：未到启动时间前不执行该 Refill
            start_raw = str(t.get('start_at') or '').strip()
            if start_raw:
                try:
                    s = start_raw.replace('T', ' ')
                    if len(s) == 16:
                        s = f"{s}:00"
                    start_dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() < start_dt:
                        continue
                except Exception:
                    pass
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
                    task_tokens = t.get('pushplus_tokens') or None
                    pseudo_items = []
                    times = [str(x).strip() for x in (t.get('target_times') or []) if str(x).strip()]
                    places = [str(x).strip() for x in (t.get('candidate_places') or []) if str(x).strip()]
                    for p in places:
                        for tm in times:
                            pseudo_items.append({"place": p, "time": tm})
                    short_title = self._build_short_title("已停用", date_str, pseudo_items)
                    self.send_wechat_notification(content, tokens=task_tokens, title=short_title)
                except Exception as e:
                    log(f"⚠️ [refill#{t.get('id')}] 截止停用通知发送失败: {e}")
                continue
            interval = max(1.0, float(t.get('interval_seconds', 10.0) or 10.0))
            # 接近开场时间时自动使用更快的轮询间隔（例如 10s 一次）
            try:
                fast_interval = max(1.0, float(t.get('fast_interval_seconds', interval) or interval))
            except Exception:
                fast_interval = interval
            try:
                fast_window_min = max(0.0, float(t.get('fast_window_minutes', 0.0) or 0.0))
            except Exception:
                fast_window_min = 0.0
            # 计算最早开场时间，用于“开场前 N 分钟加速”
            if fast_window_min > 0:
                try:
                    date_str = str(t.get('date') or '').strip()
                    times = sorted([str(x).strip() for x in (t.get('target_times') or []) if str(x).strip()])
                    if date_str and times:
                        time0 = times[0]
                        start_dt = datetime.strptime(
                            f"{date_str} {time0}:00" if len(time0) == 5 else f"{date_str} {time0}",
                            "%Y-%m-%d %H:%M:%S",
                        )
                        remaining_min = (start_dt - datetime.now()).total_seconds() / 60.0
                        if 0.0 <= remaining_min <= fast_window_min:
                            interval = min(interval, fast_interval)
                except Exception:
                    pass
            last = float(self._refill_last_run.get(tid, 0.0))
            if now - last < interval:
                continue
            quiet_info = quiet_window_block_info("refill_scheduler", owner_allowed=False)
            if quiet_info:
                self._refill_last_run[tid] = now
                t['last_result'] = {'status': 'quiet_skipped', 'msg': quiet_info.get('msg')}
                log(f"🔇 [refill#{tid}] 已跳过：{quiet_info.get('msg')}")
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

    def _normalize_task_payload(self, task):
        task = dict(task or {})
        cfg = task.get('config') if isinstance(task, dict) else None
        if not isinstance(cfg, dict):
            return task

        cfg = dict(cfg)
        if is_direct_task_config(cfg):
            delivery_groups = get_delivery_groups(cfg)
            if not delivery_groups:
                raise ValueError("极简任务至少需要 1 组合法 payload")
            primary_items = normalize_booking_items((delivery_groups[0] or {}).get('items') or [])
            if not primary_items:
                raise ValueError("极简任务的主组合不能为空")
            primary_summary = summarize_booking_items(primary_items)
            union_items = []
            seen_union = set()
            for group in delivery_groups:
                for item in normalize_booking_items(group.get("items") or []):
                    key = (str(item.get("place")), str(item.get("time")))
                    if key in seen_union:
                        continue
                    seen_union.add(key)
                    union_items.append({"place": key[0], "time": key[1]})
            union_summary = summarize_booking_items(union_items)
            cfg['mode'] = 'direct'
            cfg['delivery_groups'] = delivery_groups
            cfg['direct_items'] = primary_summary['items']
            cfg['candidate_places'] = union_summary['places']
            cfg['target_times'] = union_summary['times']
            cfg['target_count'] = max(1, int(primary_summary['place_count'] or 1))
            cfg.pop('pipeline', None)
            # 主组相关键：缺省时从主组推导
            if 'delivery_target_times' not in cfg or not cfg.get('delivery_target_times'):
                cfg['delivery_target_times'] = list(union_summary.get('times') or [])
            if 'delivery_time_preference_order' not in cfg or not cfg.get('delivery_time_preference_order'):
                cfg['delivery_time_preference_order'] = list(cfg.get('delivery_target_times') or [])
            # 第一组策略：块数无全局默认；缺省时由主组 items 推导（各时段条数取最大）
            if 'delivery_target_blocks' not in cfg or cfg.get('delivery_target_blocks') is None:
                cfg['delivery_target_blocks'] = _delivery_target_blocks_from_items(primary_items)
        elif 'target_count' in cfg:
            try:
                cfg['target_count'] = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
            except Exception:
                cfg['target_count'] = 2

        task['config'] = cfg
        return task

    def save_tasks(self):
        with open(TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)
            
    def add_task(self, task):
        # task: {id, type='daily'|'weekly', run_time='08:00', target_day_offset=2, items=[...]}
        task = self._normalize_task_payload(task)
        task['id'] = int(time.time() * 1000)
        # 默认启用自动任务；如需停用，可在前端通过“启用/停用”开关控制
        if 'enabled' not in task:
            task['enabled'] = True
        self.tasks.append(task)
        self.save_tasks()
        self.refresh_schedule()

    def update_task(self, task_id, task):
        task_id = int(task_id)
        for i, old in enumerate(self.tasks):
            if int(old.get('id', -1)) == task_id:
                task = self._normalize_task_payload(task)
                task['id'] = task_id
                task['last_run_at'] = old.get('last_run_at')
                # 若未显式传入 enabled，则沿用原任务的启用状态（默认启用）
                if 'enabled' not in task:
                    task['enabled'] = old.get('enabled', True)
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

    def _build_short_title(self, prefix: str, date_str: str | None, items: list[dict] | None):
        """
        构造类似 “已预订3.8周日6#18/7#19” 的短标题。
        """
        prefix = str(prefix or "").strip() or ""
        date_part = ""
        if date_str:
            try:
                dt = datetime.strptime(str(date_str), "%Y-%m-%d")
                weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
                wk = weekday_map[dt.weekday()]
                date_part = f"{dt.month}.{dt.day}周{wk}"
            except Exception:
                date_part = str(date_str)
        pair_text = ""
        if items:
            seen = set()
            pairs = []
            for it in items:
                p = it.get("place")
                t = it.get("time")
                if p is None or not t:
                    continue
                hour = str(t).split(":")[0]
                key = f"{p}|{hour}"
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(f"{p}#{hour}")
                if len(pairs) >= 6:
                    break
            if pairs:
                pair_text = "/".join(pairs)
        base = prefix + (date_part or "")
        if pair_text:
            return f"{base}{pair_text}"
        return base or None

    def send_wechat_notification(self, content, tokens=None, title=None):
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
            # 默认标题：从内容截取一段，供未显式传入短标题的场景使用
            short = str(content or "").replace("\n", " ").strip()
            if len(short) > 40:
                short = short[:40] + "..."
            effective_title = title or short or "场地预订通知"
            payload = {
                "title": effective_title,
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
            "source": "auto",
            "started_at": int(run_started_ts * 1000),
            "active_started_at": int(run_started_ts * 1000),
            "prestart_wait_ms": 0,
            "attempt_count": 0,
            "first_matrix_ok_ms": None,
            "first_submit_ms": None,
            "t_first_post_ms": None,
            "submit_latencies_ms": [],
            "submit_req_count": 0,
            "submit_success_resp_count": 0,
            "submit_retry_count": 0,
            "fast_lane_used_seconds": 0.0,
            "confirm_matrix_poll_count": 0,
            "confirm_orders_poll_count": 0,
            "confirm_latencies_ms": [],
            "verify_exception_count": 0,
            "first_success_ms": None,
            "result_status": None,
            "result_msg": None,
            "target_date": None,
            "request_mode": "legacy",
            "rate_limited": False,
            "transport_error": False,
            "business_fail_msg": None,
            "server_msg_raw": None,
            "t_first_accept_ms": None,
            "attempt_count_total": 0,
            "attempt_count_inflight_peak": 0,
            "dispatch_round_count": 0,
            "delivery_window_ms": None,
            "stopped_by": None,
            "resp_404_count": 0,
            "resp_5xx_count": 0,
            "timeout_count": 0,
            "connection_error_count": 0,
            "rate_limited_count": 0,
            "auth_fail_count": 0,
            "non_json_count": 0,
            "combo_tier": None,
            "backup_promoted_count": 0,
            "refill_matrix_fetch_count": 0,
            "too_fast_matrix_refresh_count": 0,
            "refill_candidate_found_count": 0,
            "refill_no_candidate_count": 0,
            "goal_satisfied": False,
            "picked_group_id": None,
            "delivery_status": None,
            "business_status": None,
            "terminal_reason": None,
            "saw_locked": False,
            "unlocked_after_locked": False,
            "config_snapshot": {
                "retry_interval": float(CONFIG.get("retry_interval", 1.0) or 1.0),
                "aggressive_retry_interval": float(CONFIG.get("aggressive_retry_interval", 0.3) or 0.3),
                "batch_retry_times": int(cfg_get("batch_retry_times", 2) or 2),
                "batch_retry_interval": float(CONFIG.get("batch_retry_interval", 0.5) or 0.5),
                "submit_batch_size": int(CONFIG.get("submit_batch_size", 3) or 3),
                "initial_submit_batch_size": int(cfg_get("initial_submit_batch_size", cfg_get("submit_batch_size", 3)) or 3),
                "submit_timeout_seconds": float(cfg_get("submit_timeout_seconds", 4.0) or 4.0),
                "submit_split_retry_times": int(cfg_get("submit_split_retry_times", 1) or 1),
                "submit_timeout_backoff_seconds": float(cfg_get("submit_timeout_backoff_seconds", 2.5) or 2.5),
                "batch_min_interval": float(cfg_get("batch_min_interval", 0.8) or 0.8),
                "fast_lane_enabled": bool(cfg_get("fast_lane_enabled", True)),
                "fast_lane_seconds": float(cfg_get("fast_lane_seconds", 2.0) or 2.0),
                "order_query_timeout_seconds": float(CONFIG.get("order_query_timeout_seconds", 2.5) or 2.5),
                "order_query_max_pages": int(CONFIG.get("order_query_max_pages", 2) or 2),
                "post_submit_orders_join_timeout_seconds": float(CONFIG.get("post_submit_orders_join_timeout_seconds", 0.3) or 0.3),
                "post_submit_verify_matrix_timeout_seconds": float(CONFIG.get("post_submit_verify_matrix_timeout_seconds", 0.8) or 0.8),
                "post_submit_verify_matrix_recheck_times": int(CONFIG.get("post_submit_verify_matrix_recheck_times", 3) or 3),
                "matrix_timeout_seconds": float(CONFIG.get("matrix_timeout_seconds", 3.0) or 3.0),
                "log_to_file": bool(CONFIG.get("log_to_file", True)),
                "log_file_dir": str(CONFIG.get("log_file_dir", "logs") or "logs"),
                "log_retention_days": int(CONFIG.get("log_retention_days", 3) or 3),
                "transient_storm_threshold": int(CONFIG.get("transient_storm_threshold", 5) or 5),
                "transient_storm_backoff_seconds": float(CONFIG.get("transient_storm_backoff_seconds", 2.5) or 2.5),
                "matrix_timeout_storm_seconds": float(CONFIG.get("matrix_timeout_storm_seconds", 5.0) or 5.0),
                "transient_storm_extend_timeout_after": int(CONFIG.get("transient_storm_extend_timeout_after", 3) or 3),
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
        task_config_hint = task.get('config') if isinstance(task.get('config'), dict) else {}
        direct_mode_enabled = is_direct_task_config(task_config_hint)

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
            title_prefix = "已预订" if (success or partial) else "预订失败"
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
            short_title = self._build_short_title(title_prefix, date_str, items or [])
            self.send_wechat_notification(content, tokens=task_pushplus_tokens, title=short_title)

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

        def merge_submit_metric(res):
            submit_metric = res.get("run_metric") if isinstance(res, dict) else None
            if not isinstance(submit_metric, dict):
                return
            run_metrics["submit_req_count"] += int(submit_metric.get("submit_req_count") or 0)
            run_metrics["submit_success_resp_count"] += int(submit_metric.get("submit_success_resp_count") or 0)
            run_metrics["submit_retry_count"] += int(submit_metric.get("submit_retry_count") or 0)
            run_metrics["confirm_matrix_poll_count"] += int(submit_metric.get("confirm_matrix_poll_count") or 0)
            run_metrics["confirm_orders_poll_count"] += int(submit_metric.get("confirm_orders_poll_count") or 0)
            run_metrics["verify_exception_count"] += int(submit_metric.get("verify_exception_count") or 0)
            run_metrics["fast_lane_used_seconds"] = max(
                float(run_metrics.get("fast_lane_used_seconds") or 0.0),
                float(submit_metric.get("fast_lane_used_seconds") or 0.0),
            )
            confirm_ms = submit_metric.get("t_confirm_ms")
            if confirm_ms is not None:
                run_metrics.setdefault("confirm_latencies_ms", []).append(int(confirm_ms))
            req_mode = str(submit_metric.get("request_mode") or "").strip()
            if req_mode:
                run_metrics["request_mode"] = req_mode
            if run_metrics.get("t_first_post_ms") is None and submit_metric.get("t_first_post_ms") is not None:
                run_metrics["t_first_post_ms"] = int(submit_metric.get("t_first_post_ms") or 0)
            run_metrics["rate_limited"] = bool(run_metrics.get("rate_limited") or submit_metric.get("rate_limited"))
            run_metrics["transport_error"] = bool(run_metrics.get("transport_error") or submit_metric.get("transport_error"))
            business_fail_msg = str(submit_metric.get("business_fail_msg") or "").strip()
            if business_fail_msg:
                run_metrics["business_fail_msg"] = business_fail_msg[:200]
            server_msg_raw = str(submit_metric.get("server_msg_raw") or "").strip()
            if server_msg_raw:
                run_metrics["server_msg_raw"] = server_msg_raw[:200]
            if run_metrics.get("t_first_accept_ms") is None and submit_metric.get("t_first_accept_ms") is not None:
                run_metrics["t_first_accept_ms"] = int(submit_metric.get("t_first_accept_ms") or 0)
            for key in (
                "attempt_count_total",
                "dispatch_round_count",
                "resp_404_count",
                "resp_5xx_count",
                "timeout_count",
                "connection_error_count",
                "rate_limited_count",
                "auth_fail_count",
                "non_json_count",
                "backup_promoted_count",
                "refill_matrix_fetch_count",
                "too_fast_matrix_refresh_count",
                "refill_candidate_found_count",
                "refill_no_candidate_count",
            ):
                run_metrics[key] = int(run_metrics.get(key) or 0) + int(submit_metric.get(key) or 0)
            run_metrics["goal_satisfied"] = bool(run_metrics.get("goal_satisfied") or submit_metric.get("goal_satisfied"))
            run_metrics["attempt_count_inflight_peak"] = max(
                int(run_metrics.get("attempt_count_inflight_peak") or 0),
                int(submit_metric.get("attempt_count_inflight_peak") or 0),
            )
            delivery_window_ms = submit_metric.get("delivery_window_ms")
            if delivery_window_ms is not None:
                existing_delivery_ms = run_metrics.get("delivery_window_ms")
                run_metrics["delivery_window_ms"] = max(
                    int(existing_delivery_ms or 0),
                    int(delivery_window_ms or 0),
                )
            for key in ("stopped_by", "combo_tier", "picked_group_id", "delivery_status", "business_status", "terminal_reason"):
                val = submit_metric.get(key)
                if val not in (None, ""):
                    run_metrics[key] = val

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
                confirm_samples = sorted(int(x) for x in (run_metrics.get("confirm_latencies_ms") or []) if x is not None)
                run_metrics["confirm_latency_p50_ms"] = int(_percentile(confirm_samples, 0.5)) if confirm_samples else None
                run_metrics["confirm_latency_p95_ms"] = int(_percentile(confirm_samples, 0.95)) if confirm_samples else None
                run_metrics["success_within_60s"] = bool(
                    run_metrics.get("first_success_ms") is not None and int(run_metrics.get("first_success_ms") or 0) <= 60000
                )
                append_task_run_metric(run_metrics)
                log(
                    f"📊 [run-metric] task={run_metrics.get('task_id')} attempts={run_metrics.get('attempt_count')} "
                    f"first_matrix={run_metrics.get('first_matrix_ok_ms')}ms first_submit={run_metrics.get('first_submit_ms')}ms "
                    f"first_post={run_metrics.get('t_first_post_ms')}ms "
                    f"first_success={run_metrics.get('first_success_ms')}ms p95={run_metrics.get('submit_latency_p95_ms')}ms"
                )
            except Exception as e:
                log(f"⚠️ [run-metric] 汇总失败: {e}")

        # 0. legacy 模式下先检查 token；极简直提模式跳过该额外请求，缩短首个有效 POST 路径。
        if not direct_mode_enabled:
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

            # 预热阶段：在正式触发前 20~30 秒内，用少量“空包弹”探测下单链路，
            # 预热 TCP/TLS/HTTP 连接，顺便提前发现明显的鉴权失效。
            try:
                preheat_enabled = (not direct_mode_enabled) and bool(CONFIG.get('auto_preheat_enabled', True))
                if preheat_enabled:
                    preheat_window = max(5.0, float(CONFIG.get('auto_preheat_window_seconds', 30.0) or 30.0))
                    min_gap = max(3.0, float(CONFIG.get('auto_preheat_min_gap_seconds', 5.0) or 5.0))
                    max_probes = max(1, min(6, int(CONFIG.get('auto_preheat_max_probes', 4) or 4)))

                    probes_done = 0
                    while probes_done < max_probes:
                        now_aligned = client.get_aligned_now()
                        seconds_to_base = (base_run - now_aligned).total_seconds()
                        if seconds_to_base <= 5.0:
                            break  # 离正式抢票太近了，停止预热

                        # 只在距离 base_run 不超过预热窗口时才发起探测
                        if seconds_to_base > preheat_window:
                            # 还早，短暂休眠后重算
                            sleep_s = min(seconds_to_base - preheat_window, 1.0)
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                            continue

                        probes_done += 1
                        probe = client.check_booking_auth_probe()
                        msg = str(probe.get('msg') or '')[:80]
                        if probe.get('ok'):
                            if probe.get('unknown'):
                                log(f"🔥 [预热探针] 下单链路可达(未知业务状态)：{msg}")
                            else:
                                log(f"🔥 [预热探针] 下单鉴权链路正常：{msg}")
                        else:
                            # 明确鉴权异常时仅记录日志；真正的报警仍以矩阵获取失败为准
                            log(f"⚠️ [预热探针] 下单链路疑似鉴权异常/故障：{msg}")

                        # 控制探针频率，避免在开售前就触发频控
                        now_aligned = client.get_aligned_now()
                        seconds_to_base = (base_run - now_aligned).total_seconds()
                        if seconds_to_base <= 5.0:
                            break
                        # 保证至少 min_gap 秒间隔，同时不跨过 base_run-3s
                        sleep_s = min(min_gap, max(0.5, seconds_to_base - 3.0))
                        if sleep_s > 0:
                            time.sleep(sleep_s)
            except Exception as e:
                log(f"⚠️ [预热探针] 执行异常，跳过预热: {e}")

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
        else:
            try:
                config = self._normalize_task_payload({"config": config}).get("config") or {}
            except ValueError as e:
                notify_task_result(False, f"任务配置错误：{e}", date_str=target_date)
                finalize_run_metrics(target_date)
                return

        # 3. 旧版兼容：没有新配置时走最早的 items 逻辑
        if not config and 'items' in task:
            res = client.submit_order(target_date, task['items'], submit_profile=CONFIG.get("auto_submit_profile", "auto_minimal"))
            merge_submit_metric(res)
            status = res.get("status")
            if status == "success":
                notify_task_result(True, "已预订", items=notify_items_from_submit_result(res, task['items']), date_str=target_date)
            elif status == "partial":
                notify_task_result(False, "部分成功", items=notify_items_from_submit_result(res, task['items']), date_str=target_date, partial=True)
            else:
                notify_task_result(False, f"下单失败：{res.get('msg')}", items=task['items'], date_str=target_date)
            finalize_run_metrics(target_date)
            return

        delivery_groups = get_delivery_groups(config)
        if delivery_groups:
            primary_items = normalize_booking_items((delivery_groups[0] or {}).get("items") or [])
            run_metrics["request_mode"] = "delivery_campaign"

            if bool(CONFIG.get("minimal_pre_submit_matrix_once", False)):
                run_metrics["attempt_count"] += 1
                snapshot_started_at = time.time()
                snapshot_res = client.get_matrix(target_date, include_mine_overlay=False)
                if run_metrics.get("first_matrix_ok_ms") is None and isinstance(snapshot_res, dict) and not snapshot_res.get("error"):
                    run_metrics["first_matrix_ok_ms"] = int(max(0.0, snapshot_started_at - active_started_ts) * 1000)
                elif isinstance(snapshot_res, dict) and snapshot_res.get("error"):
                    log(f"⚠️ [极简直提] 预提交矩阵快照失败，继续直提: {snapshot_res.get('error')}")

            run_metrics["attempt_count"] += 1
            submit_started_at = time.time()
            if run_metrics.get("first_submit_ms") is None:
                first_post_ms = int(max(0.0, submit_started_at - active_started_ts) * 1000)
                run_metrics["first_submit_ms"] = first_post_ms
                run_metrics["t_first_post_ms"] = first_post_ms
            run_metrics["target_date"] = str(target_date)
            log(f"🚀 [终极递送器] 启动主组合递送: {primary_items}")
            res = client.submit_delivery_campaign(target_date, delivery_groups, submit_profile="auto_minimal", task_config=config)
            merge_submit_metric(res)
            run_metrics.setdefault("submit_latencies_ms", []).append(int(max(0.0, time.time() - submit_started_at) * 1000))

            status = str(res.get("status") or "")
            if status == "success":
                msg = str(res.get("msg") or "下单请求已提交成功，请稍后手动刷新结果")
                notify_task_result(True, msg, items=notify_items_from_submit_result(res, primary_items), date_str=target_date)
            else:
                fail_msg = str(res.get("msg") or "下单失败")
                notify_task_result(False, f"终极递送器失败：{fail_msg}", items=primary_items, date_str=target_date)
            finalize_run_metrics(target_date)
            return

        # === DEPRECATED: normal/pipeline 分支（已遮蔽，后期统一清理）===
        else:
            log("当前仅支持极简直提，跳过 normal/pipeline")
            notify_task_result(False, "当前仅支持极简直提任务，请为任务配置递送组合(delivery_groups)。", date_str=target_date)
            finalize_run_metrics(target_date)
            return

        # 以下为 DEPRECATED 分支，因上文 else 已 return，仅保留供后期删除
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
            mode = str(CONFIG.get('pipeline_greedy_end_mode', 'absolute') or 'absolute').strip()

            # 全局参数化：任务级不再携带 pipeline 截止配置
            if mode == 'before_start':
                hours_raw = CONFIG.get('pipeline_greedy_end_before_hours', 24)
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
            mode = str(cfg.get('mode', 'normal') or 'normal').strip()
            pipe = cfg.get('pipeline') if isinstance(cfg.get('pipeline'), dict) else {}
            stages_raw = pipe.get('stages') if isinstance(pipe.get('stages'), list) else []
            enabled_map = {}
            for st in stages_raw:
                if isinstance(st, dict) and st.get('type'):
                    enabled_map[str(st.get('type'))] = bool(st.get('enabled', True))

            # 兼容旧任务：normal/smart_continuous 统一映射到 pipeline 核心。
            # - normal: continuous-only（保持“整场优先”语义）
            # - smart_continuous: 仅影响 continuous 连号偏好
            if mode == 'normal':
                enabled_map = {
                    'continuous': True,
                    'random': False,
                    'refill': False,
                }
                continuous_prefer_adjacent = bool(cfg.get('smart_continuous', False))
            else:
                continuous_prefer_adjacent = bool(CONFIG.get('pipeline_continuous_prefer_adjacent', True))

            stages = [
                {"type": "continuous", "enabled": enabled_map.get('continuous', True), "window_seconds": max(1, int(CONFIG.get('pipeline_continuous_window_seconds', 8) or 8))},
                {"type": "random", "enabled": enabled_map.get('random', True), "window_seconds": max(1, int(CONFIG.get('pipeline_random_window_seconds', 12) or 12))},
                {"type": "refill", "enabled": enabled_map.get('refill', False), "interval_seconds": max(1, int(CONFIG.get('pipeline_refill_interval_seconds', 15) or 15))},
            ]
            return {
                "stages": stages,
                "stop_when_reached": bool(CONFIG.get('pipeline_stop_when_reached', True)),
                "continuous_prefer_adjacent": continuous_prefer_adjacent,
                "no_progress_switch_rounds": max(1, int(pipe.get('no_progress_switch_rounds', 2) or 2)),
            }

        def calc_pipeline_need(cfg, date_str):
            nonlocal pipeline_last_known_mine_slots
            target_times = [str(t) for t in (cfg.get('target_times') or [])]
            candidate_places = [str(p) for p in (cfg.get('candidate_places') or [])]
            target_count = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))

            task_scope = {(p, t) for p in candidate_places for t in target_times}

            # 通过矩阵判断「本人已预订」的场次，减少对订单接口的依赖
            mine_slots = set()
            # 不需要订单覆盖，直接依赖矩阵语义中的 'mine'
            matrix_res = client.get_matrix(date_str, include_mine_overlay=False)
            matrix = matrix_res.get("matrix") or {}

            def _is_mine_status(status):
                if status is None:
                    return False
                if isinstance(status, str):
                    s = status.lower()
                    return s in ('mine', 'self', 'my_booked', 'mybooked', 'booked_by_me')
                return False

            for p, row in matrix.items():
                for t, st in (row or {}).items():
                    if _is_mine_status(st):
                        mine_slots.add((str(p), str(t)))

            if mine_slots:
                pipeline_last_known_mine_slots = set(mine_slots)
            elif isinstance(pipeline_last_known_mine_slots, set) and pipeline_last_known_mine_slots:
                # 如果本次矩阵中没有 mine 标记，但历史上有快照，则沿用快照作为保守估计
                mine_slots = set(pipeline_last_known_mine_slots)

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
        consecutive_transient_failures = 0  # 连续 404/超时/非JSON 次数，用于风暴退避与延长 matrix 超时

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

            # 1. 获取最新场地状态（风暴期使用略长超时，便于在服务器卡顿时仍能拿到矩阵）
            include_mine_overlay = attempt > 1
            if not include_mine_overlay:
                log("⚡ [加速] 首轮跳过mine覆盖，优先抢占可用库存")
            storm_extend_after = int(CONFIG.get('transient_storm_extend_timeout_after', 3) or 3)
            matrix_timeout_override = None
            if consecutive_transient_failures >= storm_extend_after:
                matrix_timeout_override = max(1.0, float(CONFIG.get('matrix_timeout_storm_seconds', 5.0) or 5.0))
            matrix_res = client.get_matrix(target_date, include_mine_overlay=include_mine_overlay, request_timeout=matrix_timeout_override)

            # 1.1 错误处理（服务器崩了 / token 失效等）
            if "error" in matrix_res:
                err_msg = matrix_res["error"]
                log(f"获取状态失败: {err_msg} 喵")

                # 服务器短时异常（404/5xx/网关/超时/非JSON等）—— 死磕模式 + 风暴退避
                err_l = str(err_msg or "").lower()
                transient_keywords = [
                    "非json格式", "non-json", "404", "502", "503", "504", "无效数据",
                    "nginx", "bad gateway", "service unavailable", "timeout", "timed out",
                    "connection reset", "max retries exceeded", "temporarily unavailable",
                ]
                if any(k in err_l for k in transient_keywords):
                    consecutive_transient_failures += 1
                    storm_threshold = int(CONFIG.get('transient_storm_threshold', 5) or 5)
                    storm_backoff = max(0.5, float(CONFIG.get('transient_storm_backoff_seconds', 2.5) or 2.5))
                    if consecutive_transient_failures >= storm_threshold:
                        log(f"⚠️ 连续 {consecutive_transient_failures} 次服务器异常，退避 {storm_backoff}s 后继续")
                        time.sleep(storm_backoff)
                        consecutive_transient_failures = 0
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

            # 1.2 正常拿到矩阵（成功一次即重置风暴计数）
            consecutive_transient_failures = 0
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

                    # --- 统一核心模式：pipeline(continuous/random/refill)
                    # normal/smart_continuous 会在 build_pipeline_cfg 内映射为 continuous-only pipeline。 ---
                    if mode in ('pipeline', 'normal'):
                        pipeline_cfg_for_retry = cfg
                        now_ts = time.time()
                        if pipeline_started_at is None:
                            pipeline_started_at = now_ts

                        need_res = calc_pipeline_need(cfg, target_date)
                        pipe_cfg = build_pipeline_cfg(cfg)
                        current_need_total = sum(int(v) for v in (need_res.get('need_by_time') or {}).values())

                        if sum(need_res['need_by_time'].values()) == 0 and pipe_cfg['stop_when_reached']:
                            achieved_slots = list(need_res.get("task_mine") or [])
                            achieved_count = len(achieved_slots)
                            run_metrics["success_item_count"] = max(int(run_metrics.get("success_item_count") or 0), achieved_count)
                            run_metrics["failed_item_count"] = 0
                            achieved_items = [
                                {"place": str(p), "time": str(t)}
                                for (p, t) in sorted(
                                    achieved_slots,
                                    key=lambda x: (
                                        str(x[1]),
                                        int(str(x[0])) if str(x[0]).isdigit() else 999,
                                        str(x[0]),
                                    ),
                                )
                            ]
                            notify_task_result(
                                True,
                                "已达任务目标，无需补齐",
                                items=achieved_items,
                                date_str=target_date,
                            )
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
                        late_start_ext = 0.0
                        if (run_metrics.get('first_matrix_ok_ms') or 0) > int(CONFIG.get('pipeline_late_start_threshold_ms', 20000) or 20000):
                            late_start_ext = float(CONFIG.get('pipeline_random_window_extension_after_late_start_seconds', 45) or 45)
                        for st in stages:
                            if not isinstance(st, dict) or not st.get('enabled', True):
                                continue
                            stype = str(st.get('type') or '').strip()
                            if stype == 'refill':
                                # 统一关闭 pipeline 内置 refill 阶段，改由独立 Refill 任务负责补订
                                continue
                            win = float(st.get('window_seconds', 0) or 0)
                            if stype == 'random' and late_start_ext > 0:
                                win += late_start_ext
                            if win <= 0:
                                continue
                            if elapsed < consumed + win:
                                active_stage = st
                                break
                            consumed += win

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

                    else:
                        log(f"❌ 任务配置错误: 不支持的模式 {mode}")
                        notify_task_result(False, f"任务配置错误：不支持的模式 {mode}", date_str=target_date)
                        finalize_run_metrics(target_date)
                        return

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
                # 首矩阵晚到时，第一次提交前短停顿，避免“刚挤进矩阵就立刻打提交”被限流判为操作过快
                if not has_submitted_once and (run_metrics.get("first_matrix_ok_ms") or 0) > 15000:
                    delay_s = max(0.0, float(CONFIG.get("first_submit_delay_seconds", 1.5) or 1.5))
                    if delay_s > 0:
                        log(f"⏳ 首矩阵晚到({run_metrics.get('first_matrix_ok_ms')}ms)，提交前停顿 {round(delay_s, 2)}s 降低限流风险")
                        time.sleep(delay_s)
                submit_started_at = time.time()
                if run_metrics.get("first_submit_ms") is None:
                    first_post_ms = int(max(0.0, submit_started_at - active_started_ts) * 1000)
                    run_metrics["first_submit_ms"] = first_post_ms
                    run_metrics["t_first_post_ms"] = first_post_ms
                log(f"正在提交分批订单: {final_items}")
                res = client.submit_order(target_date, final_items, submit_profile=CONFIG.get("auto_submit_profile", "auto_minimal"))
                merge_submit_metric(res)
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
                    recheck_times = max(0, min(5, int(CONFIG.get("post_submit_verify_pending_matrix_recheck_times", 4) or 4)))
                    pending_items = list(res.get("failed_items") or final_items or [])
                    recovered_items = []
                    if recheck_times > 0 and pending_items:
                        log(f"⏳ 提交成功但验证未收敛，先做矩阵快速复核({recheck_times}次，每次{round(fast_retry_s, 2)}s): {res.get('msg')}")
                    for idx in range(recheck_times):
                        if not pending_items:
                            break
                        time.sleep(fast_retry_s)
                        verify_res = client.get_matrix(target_date, include_mine_overlay=False)
                        if not isinstance(verify_res, dict) or verify_res.get("error"):
                            continue
                        v_matrix = verify_res.get("matrix") or {}
                        still_pending = []
                        for it in pending_items:
                            p = str(it.get("place"))
                            t = str(it.get("time"))
                            state = v_matrix.get(p, {}).get(t)
                            if state in ("booked", "mine"):
                                recovered_items.append({"place": p, "time": t})
                            else:
                                still_pending.append({"place": p, "time": t})
                        pending_items = still_pending
                        if not pending_items:
                            break
                    if final_items and len(recovered_items) >= len(final_items):
                        run_metrics["success_item_count"] = max(int(run_metrics.get("success_item_count") or 0), len(recovered_items))
                        run_metrics["failed_item_count"] = 0
                        run_metrics["goal_achieved"] = True
                        log("✅ verify_pending 经矩阵快速复核后收敛为成功，跳过重复提交")
                        try:
                            notify_task_result(
                                True,
                                "已预订",
                                items=recovered_items,
                                date_str=target_date,
                            )
                        except Exception as e:
                            log(f"构建短信内容失败: {e}")
                        finalize_run_metrics(target_date)
                        return

                    log(f"⏳ verify_pending 矩阵复核后仍未收敛，继续进入下一轮: remain={len(pending_items) if pending_items else len(final_items or [])}")
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
            # 仅对“启用”的任务建立定时调度；停用任务仍保留在列表中，但不会被自动触发
            if not bool(task.get('enabled', True)):
                continue
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
            task_manager.process_quiet_window_tick()
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


@app.route('/mine')
@app.route('/mine/')
def mine_page():
    return render_main_page('mine')


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
    quiet_info = quiet_window_block_info("api_matrix", owner_allowed=False)
    if quiet_info:
        return jsonify({"error": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")})
    date = request.args.get('date')
    include_mine_raw = request.args.get('include_mine', '1')
    include_mine_overlay = str(include_mine_raw).lower() not in ('0', 'false', 'no')
    with runtime_request_context("api_matrix", owner=False):
        return jsonify(client.get_matrix(date, include_mine_overlay=include_mine_overlay))

@app.route('/api/mine-overview')
def api_mine_overview():
    quiet_info = quiet_window_block_info("api_mine_overview", owner_allowed=False)
    if quiet_info:
        return jsonify({'error': quiet_info.get("msg"), 'quiet_window_blocked': True, 'quiet_window': quiet_info.get("quiet_window")})
    with runtime_request_context("api_mine_overview", owner=False):
        orders_res = client.get_place_orders()
        balance = None
        try:
            card_res = client.get_use_card_info()
            if 'error' not in card_res and card_res.get('universal'):
                first = card_res['universal'][0]
                raw = first.get('cardcash')
                if raw is not None:
                    try:
                        balance = float(raw)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
    if 'error' in orders_res:
        return jsonify({'error': orders_res.get('error')})
    grouped = client.extract_mine_slots_by_date_with_bill_num(orders_res.get('data') or [])
    return jsonify({'records': grouped, 'balance': balance})


@app.route('/api/cancel-order', methods=['POST'])
def api_cancel_order():
    quiet_info = quiet_window_block_info("api_cancel_order", owner_allowed=False)
    if quiet_info:
        return jsonify({'ok': False, 'msg': quiet_info.get("msg"), 'quiet_window_blocked': True, 'quiet_window': quiet_info.get("quiet_window")})
    data = request.get_json() or {}
    bill_num = (data.get('billNum') or '').strip()
    if not bill_num:
        return jsonify({'ok': False, 'msg': '缺少 billNum'})
    reason = (data.get('reason') or '用户取消').strip() or '用户取消'
    with runtime_request_context("api_cancel_order", owner=False):
        res = client.cancel_place_order(bill_num, reason=reason)
    if res.get('ok'):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': res.get('msg', '取消失败')})


@app.route('/api/time')
def api_time():
    return jsonify({"timestamp": datetime.now().timestamp()})

@app.route('/api/quiet-window')
def api_quiet_window():
    return jsonify(get_quiet_window_status())

@app.route('/api/book', methods=['POST'])
def api_book():
    quiet_info = quiet_window_block_info("api_book", owner_allowed=False)
    if quiet_info:
        return jsonify({
            "status": "quiet_window_blocked",
            "msg": quiet_info.get("msg"),
            "quiet_window": quiet_info.get("quiet_window"),
        })
    data = request.json
    date = data.get('date')
    items = data.get('items')
    submit_mode = str(data.get('submit_mode') or '').strip().lower()
    manual_submit_profile = "manual_minimal" if submit_mode in ('minimal', 'direct') else CONFIG.get("manual_submit_profile", "manual_minimal")
    with runtime_request_context("api_book", owner=False):
        res = client.submit_order(date, items, submit_profile=manual_submit_profile)

    # 手动预订场景：对 verify_pending 做轻量复核，先看矩阵，必要时做一次订单兜底。
    if isinstance(res, dict) and res.get('status') == 'verify_pending':
        run_metric = dict(res.get('run_metric') or {})
        pending_items = list(res.get('failed_items') or items or [])
        retry_s = max(0.05, float(CONFIG.get('manual_verify_pending_retry_seconds', 0.25) or 0.25))
        recheck_times = max(0, min(8, int(CONFIG.get('manual_verify_pending_recheck_times', 3) or 3)))
        verify_timeout_s = max(0.5, float(cfg_get('post_submit_verify_matrix_timeout_seconds', 0.8) or 0.8))
        orders_fallback_enabled = bool(CONFIG.get('manual_verify_pending_orders_fallback_enabled', True))

        reconcile_rounds = 0
        reconcile_matrix_error_count = 0
        reconcile_orders_fallback_used = False
        reconcile_orders_fallback_hit_count = 0
        recovered_items = []

        for idx in range(recheck_times):
            if not pending_items:
                break
            if idx > 0:
                time.sleep(retry_s)
            reconcile_rounds += 1
            verify_res = client.get_matrix(date, include_mine_overlay=False, request_timeout=verify_timeout_s)
            if not isinstance(verify_res, dict) or verify_res.get('error'):
                reconcile_matrix_error_count += 1
                continue
            v_matrix = verify_res.get('matrix') or {}
            still_pending = []
            for it in pending_items:
                p = str(it.get('place'))
                t = str(it.get('time'))
                state = v_matrix.get(p, {}).get(t)
                if state in ('booked', 'mine'):
                    recovered_items.append({'place': p, 'time': t})
                else:
                    still_pending.append({'place': p, 'time': t})
            pending_items = still_pending

        if pending_items and orders_fallback_enabled:
            reconcile_orders_fallback_used = True
            try:
                order_timeout_s = max(0.5, float(cfg_get('order_query_timeout_seconds', 2.5) or 2.5))
                order_max_pages = max(1, min(3, int(cfg_get('order_query_max_pages', 2) or 2)))
                orders_res = client.get_place_orders(max_pages=order_max_pages, timeout_s=order_timeout_s)
                if isinstance(orders_res, dict) and not orders_res.get('error'):
                    grouped = client.extract_mine_slots_by_date(orders_res.get('data') or [])
                    mine_slots = {
                        (str(it.get('place')), str(it.get('time')))
                        for it in (grouped.get(str(date)) or [])
                        if isinstance(it, dict)
                    }
                    still_pending = []
                    for it in pending_items:
                        p = str(it.get('place'))
                        t = str(it.get('time'))
                        if (p, t) in mine_slots:
                            recovered_items.append({'place': p, 'time': t})
                            reconcile_orders_fallback_hit_count += 1
                        else:
                            still_pending.append({'place': p, 'time': t})
                    pending_items = still_pending
            except Exception:
                pass

        # 在 verify_pending 场景下，如果原始场次未完全收敛，尝试在同一时间段做一次“小规模补订”，
        # 以本次点击为原子事务：优先补齐同一时间的其他可用场地，而不是直接宣告失败。
        manual_auto_refill_enabled = bool(CONFIG.get('manual_auto_refill_enabled', True))
        if manual_auto_refill_enabled and pending_items:
            try:
                refill_matrix_res = client.get_matrix(date, include_mine_overlay=True, request_timeout=verify_timeout_s)
                if isinstance(refill_matrix_res, dict) and not refill_matrix_res.get('error'):
                    refill_matrix = refill_matrix_res.get('matrix') or {}

                    # 原始选择 & 已收敛成功的 (place, time) 集合，避免重复下单
                    original_pairs = {
                        (str(it.get('place')), str(it.get('time')))
                        for it in (items or [])
                        if isinstance(it, dict)
                    }
                    recovered_pairs = {
                        (str(it.get('place')), str(it.get('time')))
                        for it in (recovered_items or [])
                        if isinstance(it, dict)
                    }

                    # 统计每个时间段的缺口数（按 pending_items 维度）
                    need_by_time = {}
                    for it in pending_items:
                        t = str(it.get('time'))
                        if t:
                            need_by_time[t] = need_by_time.get(t, 0) + 1

                    refill_candidates = []
                    for t, need in need_by_time.items():
                        if need <= 0:
                            continue
                        available_slots = []
                        for p in sorted(refill_matrix.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
                            state = (refill_matrix.get(p) or {}).get(t)
                            key = (str(p), t)
                            if state == 'available' and key not in original_pairs and key not in recovered_pairs:
                                available_slots.append({'place': str(p), 'time': t})
                        # 按缺口数量截断，防止超买
                        refill_candidates.extend(available_slots[:max(0, need)])

                    if refill_candidates:
                        refill_res = client.submit_order(
                            date,
                            refill_candidates,
                            submit_profile=manual_submit_profile,
                        )
                        if isinstance(refill_res, dict):
                            # 合并补订阶段的指标，便于统一观测一次半自动事务
                            refill_metric = refill_res.get('run_metric') or {}
                            if isinstance(refill_metric, dict):
                                for k in (
                                    'submit_req_count',
                                    'submit_success_resp_count',
                                    'submit_retry_count',
                                    'confirm_matrix_poll_count',
                                    'confirm_orders_poll_count',
                                    'verify_exception_count',
                                ):
                                    if k in refill_metric:
                                        run_metric[k] = int(run_metric.get(k) or 0) + int(refill_metric.get(k) or 0)

                            refill_success = [
                                {'place': str(it.get('place')), 'time': str(it.get('time'))}
                                for it in (refill_res.get('success_items') or [])
                                if isinstance(it, dict) and it.get('place') and it.get('time')
                            ]
                            if refill_success:
                                refill_pairs = {
                                    (it['place'], it['time'])
                                    for it in refill_success
                                }
                                # 将补订成功的场次并入 recovered_items
                                recovered_items.extend(refill_success)

                                # 从 pending_items 中按时间/数量消耗缺口
                                updated_pending = []
                                for it in pending_items:
                                    key = (str(it.get('place')), str(it.get('time')))
                                    t = str(it.get('time'))
                                    if key in refill_pairs and need_by_time.get(t, 0) > 0:
                                        need_by_time[t] -= 1
                                    else:
                                        updated_pending.append(it)
                                pending_items = updated_pending
            except Exception:
                # 补订异常不影响原有 verify_pending 流程
                pass

        run_metric['manual_reconcile_rounds'] = int(reconcile_rounds)
        run_metric['manual_reconcile_matrix_error_count'] = int(reconcile_matrix_error_count)
        run_metric['manual_reconcile_orders_fallback_used'] = bool(reconcile_orders_fallback_used)
        run_metric['manual_reconcile_orders_fallback_hit_count'] = int(reconcile_orders_fallback_hit_count)

        if items and len(recovered_items) >= len(items):
            res = {
                'status': 'success',
                'msg': 'verify_pending 经半自动复核收敛为成功',
                'success_items': recovered_items,
                'failed_items': [],
                'run_metric': run_metric,
            }
        elif recovered_items:
            res = {
                'status': 'partial',
                'msg': f"verify_pending 复核后部分收敛({len(recovered_items)}/{len(items or [])})",
                'success_items': recovered_items,
                'failed_items': pending_items,
                'run_metric': run_metric,
            }
        else:
            res['run_metric'] = run_metric

    try:
        run_metric = res.get('run_metric') if isinstance(res, dict) else {}
        if not isinstance(run_metric, dict):
            run_metric = {}
        manual_record = {
            'source': 'manual',
            'task_id': None,
            'task_type': 'manual',
            'started_at': int(time.time() * 1000),
            'finished_at': int(time.time() * 1000),
            'date': str(date or ''),
            'status': str(res.get('status') if isinstance(res, dict) else 'unknown'),
            'msg': str(res.get('msg') if isinstance(res, dict) else '')[:200],
            'items_count': len(items or []),
            'items': list(items or [])[:10],
            'submit_req_count': int(run_metric.get('submit_req_count') or 0),
            'submit_success_resp_count': int(run_metric.get('submit_success_resp_count') or 0),
            'submit_retry_count': int(run_metric.get('submit_retry_count') or 0),
            'effective_batch_retry_times': int(run_metric.get('effective_batch_retry_times') or 0),
            'effective_initial_batch_size': int(run_metric.get('effective_initial_batch_size') or 0),
            'submit_strategy_mode': str(run_metric.get('submit_strategy_mode') or ''),
            'retry_budget_total': int(run_metric.get('retry_budget_total') or 0),
            'retry_budget_used': int(run_metric.get('retry_budget_used') or 0),
            'adaptive_small_n_merge_applied': bool(run_metric.get('adaptive_small_n_merge_applied', False)),
            'submit_grouping_mode': str(run_metric.get('submit_grouping_mode') or ''),
            'place_first_grouping_applied': bool(run_metric.get('place_first_grouping_applied', False)),
            'confirm_matrix_poll_count': int(run_metric.get('confirm_matrix_poll_count') or 0),
            'confirm_orders_poll_count': int(run_metric.get('confirm_orders_poll_count') or 0),
            't_first_post_ms': run_metric.get('t_first_post_ms'),
            't_first_accept_ms': run_metric.get('t_first_accept_ms'),
            't_confirm_ms': run_metric.get('t_confirm_ms'),
            'verify_exception_count': int(run_metric.get('verify_exception_count') or 0),
            'request_mode': str(run_metric.get('request_mode') or ''),
            'rate_limited': bool(run_metric.get('rate_limited', False)),
            'transport_error': bool(run_metric.get('transport_error', False)),
            'business_fail_msg': str(run_metric.get('business_fail_msg') or '')[:200],
            'server_msg_raw': str(run_metric.get('server_msg_raw') or '')[:200],
            'attempt_count_total': int(run_metric.get('attempt_count_total') or 0),
            'attempt_count_inflight_peak': int(run_metric.get('attempt_count_inflight_peak') or 0),
            'dispatch_round_count': int(run_metric.get('dispatch_round_count') or 0),
            'delivery_window_ms': run_metric.get('delivery_window_ms'),
            'stopped_by': str(run_metric.get('stopped_by') or ''),
            'resp_404_count': int(run_metric.get('resp_404_count') or 0),
            'resp_5xx_count': int(run_metric.get('resp_5xx_count') or 0),
            'timeout_count': int(run_metric.get('timeout_count') or 0),
            'connection_error_count': int(run_metric.get('connection_error_count') or 0),
            'rate_limited_count': int(run_metric.get('rate_limited_count') or 0),
            'auth_fail_count': int(run_metric.get('auth_fail_count') or 0),
            'non_json_count': int(run_metric.get('non_json_count') or 0),
            'combo_tier': str(run_metric.get('combo_tier') or ''),
            'backup_promoted_count': int(run_metric.get('backup_promoted_count') or 0),
            'refill_matrix_fetch_count': int(run_metric.get('refill_matrix_fetch_count') or 0),
            'too_fast_matrix_refresh_count': int(run_metric.get('too_fast_matrix_refresh_count') or 0),
            'effective_delivery_min_post_interval_seconds': float(
                run_metric.get('effective_delivery_min_post_interval_seconds') or 0.0
            ),
            'refill_candidate_found_count': int(run_metric.get('refill_candidate_found_count') or 0),
            'refill_no_candidate_count': int(run_metric.get('refill_no_candidate_count') or 0),
            'goal_satisfied': bool(run_metric.get('goal_satisfied', False)),
            'picked_group_id': str(run_metric.get('picked_group_id') or ''),
            'delivery_status': str(run_metric.get('delivery_status') or ''),
            'business_status': str(run_metric.get('business_status') or ''),
            'terminal_reason': str(run_metric.get('terminal_reason') or ''),
            'manual_reconcile_rounds': int(run_metric.get('manual_reconcile_rounds') or 0),
            'manual_reconcile_matrix_error_count': int(run_metric.get('manual_reconcile_matrix_error_count') or 0),
            'manual_reconcile_orders_fallback_used': bool(run_metric.get('manual_reconcile_orders_fallback_used', False)),
            'manual_reconcile_orders_fallback_hit_count': int(run_metric.get('manual_reconcile_orders_fallback_hit_count') or 0),
            'submit_profile': str(run_metric.get('submit_profile') or manual_submit_profile),
            'config_snapshot': {
                'submit_timeout_seconds': float(cfg_get('submit_timeout_seconds', CONFIG.get('submit_timeout_seconds', 4.0)) or 4.0),
                'initial_submit_batch_size': int(run_metric.get('effective_initial_batch_size') or cfg_get('initial_submit_batch_size', CONFIG.get('initial_submit_batch_size', 1)) or 1),
                'submit_batch_size': int(cfg_get('submit_batch_size', CONFIG.get('submit_batch_size', 3)) or 3),
                'batch_retry_times': int(run_metric.get('effective_batch_retry_times') or cfg_get('batch_retry_times', CONFIG.get('batch_retry_times', 2)) or 2),
                'batch_retry_interval': float(cfg_get('batch_retry_interval', CONFIG.get('batch_retry_interval', 0.5)) or 0.5),
                'fast_lane_enabled': bool(run_metric.get('effective_fast_lane_enabled', CONFIG.get('fast_lane_enabled', True))),
                'fast_lane_seconds': float(run_metric.get('effective_fast_lane_seconds', CONFIG.get('fast_lane_seconds', 2.0)) or 0.0),
                'manual_verify_pending_orders_fallback_enabled': bool(CONFIG.get('manual_verify_pending_orders_fallback_enabled', True)),
                'multi_item_retry_balance_enabled': bool(CONFIG.get('multi_item_retry_balance_enabled', True)),
                'multi_item_batch_retry_times_cap': int(CONFIG.get('multi_item_batch_retry_times_cap', 1) or 1),
                'multi_item_retry_total_budget': int(CONFIG.get('multi_item_retry_total_budget', 3) or 3),
                'submit_strategy_mode': str(run_metric.get('submit_strategy_mode') or cfg_get('submit_strategy_mode', CONFIG.get('submit_strategy_mode', 'adaptive')) or 'adaptive'),
                'submit_adaptive_target_batches': int(CONFIG.get('submit_adaptive_target_batches', 2) or 2),
                'submit_adaptive_min_batch_size': int(CONFIG.get('submit_adaptive_min_batch_size', 1) or 1),
                'submit_adaptive_max_batch_size': int(CONFIG.get('submit_adaptive_max_batch_size', 3) or 3),
                'submit_adaptive_merge_small_n': int(CONFIG.get('submit_adaptive_merge_small_n', 2) or 2),
                'submit_adaptive_merge_same_time_only': bool(CONFIG.get('submit_adaptive_merge_same_time_only', True)),
                'submit_grouping_mode': str(run_metric.get('submit_grouping_mode') or CONFIG.get('submit_grouping_mode', 'smart') or 'smart'),
                'batch_min_interval': float(run_metric.get('effective_batch_min_interval', CONFIG.get('batch_min_interval', 0.8)) or 0.8),
                'too_fast_cooldown_seconds': float(run_metric.get('effective_too_fast_cooldown_seconds', 1.4) or 1.4),
            },
        }
        append_task_run_metric(manual_record)
    except Exception as e:
        print(f"⚠️ [manual-metric] 写入失败: {e}")

    # 手动预订：无论成功/部分成功/失败/待确认，都发送一次回执通知，告知本次任务已执行及结果。
    try:
        status = str(res.get('status') if isinstance(res, dict) else 'unknown')
        msg = str(res.get('msg') if isinstance(res, dict) else '')
        status_label = {
            'success': '手动预订成功',
            'partial': '手动预订部分成功',
            'verify_pending': '手动预订待确认',
            'fail': '手动预订失败',
        }.get(status, '手动预订结果未知')

        base_detail = f"日期 {date}，{msg or '请登录小程序确认实际订单状态。'}"
        success_items_text = []
        success_items_struct = []
        if isinstance(res, dict):
            for item in res.get('success_items') or []:
                p = item.get('place')
                t = item.get('time')
                if p is not None and t:
                    success_items_text.append(f"{p}号{t}")
                    success_items_struct.append({"place": p, "time": t})
        if success_items_text:
            base_detail += f" 成功场次：{'，'.join(success_items_text)}"

        full_text = f"{status_label}。{base_detail}"

        phones = CONFIG.get('notification_phones') or []
        pushplus_tokens = CONFIG.get('pushplus_tokens') or []

        if phones:
            task_manager.send_notification(full_text, phones=phones)
        if pushplus_tokens:
            # 微信通知：在成功/部分成功场景下，使用简化短标题，例如 “已预订3.10周二2#12”
            short_title = None
            if status in ('success', 'partial') and success_items_struct:
                short_title = task_manager._build_short_title("已预订", date, success_items_struct)
            task_manager.send_wechat_notification(full_text, tokens=pushplus_tokens, title=short_title)
    except Exception as e:
        print(f"⚠️ [manual-notify] 发送通知失败: {e}")

    return jsonify(res)


def _load_config_from_disk():
    """从磁盘读取 config.json（并合并 config.secret.json 敏感键），更新 CONFIG，使内存与文件一致。"""
    saved = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f) or {}
            if not isinstance(saved, dict):
                saved = {}
        except Exception as e:
            print(f"从磁盘加载配置失败: {e}")
            return
    if os.path.exists(CONFIG_SECRET_FILE):
        try:
            with open(CONFIG_SECRET_FILE, 'r', encoding='utf-8') as f:
                secret_saved = json.load(f)
            if isinstance(secret_saved, dict):
                for k in SENSITIVE_TOP_LEVEL_KEYS:
                    if k in secret_saved:
                        saved[k] = copy.deepcopy(secret_saved[k])
        except Exception as e:
            print(f"加载敏感配置失败: {e}")
    for _k in DEPRECATED_EXEC_PARAM_KEYS:
        saved.pop(_k, None)
    CONFIG.update(saved)
    for _k in DEPRECATED_EXEC_PARAM_KEYS:
        CONFIG.pop(_k, None)


@app.route('/api/config', methods=['GET'])
def get_config():
    _load_config_from_disk()
    return jsonify(CONFIG)


@app.route('/api/config/export', methods=['GET'])
def export_config():
    """导出执行参数（不含敏感项），用于备份或迁移。"""
    scope = (request.args.get('scope') or '').strip().lower()
    if scope != 'execution':
        return jsonify({"status": "error", "msg": "仅支持 scope=execution"}), 400
    _load_config_from_disk()
    out = {k: copy.deepcopy(CONFIG[k]) for k in CONFIG if k not in SENSITIVE_TOP_LEVEL_KEYS}
    return jsonify(out)


@app.route('/api/config/import', methods=['POST'])
def import_config():
    """导入执行参数 JSON，仅更新非敏感键并只写 config.json。"""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"status": "error", "msg": "请求体须为 JSON 对象"}), 400
        saved_public = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved_public = json.load(f) or {}
            except Exception:
                saved_public = {}
        if not isinstance(saved_public, dict):
            saved_public = {}
        for k in list(data.keys()):
            if k in SENSITIVE_TOP_LEVEL_KEYS:
                continue
            try:
                saved_public[k] = copy.deepcopy(data[k])
                CONFIG[k] = copy.deepcopy(data[k])
            except Exception:
                pass
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(saved_public, f, ensure_ascii=False, indent=2)
        schedule_health_check()
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"导入执行参数失败: {e}")
        return jsonify({"status": "error", "msg": str(e)})


def _update_config_impl(data, scope=None):
    """内部：按 scope 更新配置并写盘。scope=None 全量，'execution' 仅执行参数写 config.json，'basic' 仅基础参数写 config.secret.json。"""
    # 读取：按 scope 决定读哪些文件
    saved = {}
    if scope != 'basic':
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f) or {}
            except Exception as e:
                if scope == 'execution':
                    print(f"加载配置失败: {e}")
                saved = {}
    if scope is None and os.path.exists(CONFIG_SECRET_FILE):
        try:
            with open(CONFIG_SECRET_FILE, 'r', encoding='utf-8') as f:
                secret_saved = json.load(f)
            if isinstance(secret_saved, dict):
                for k in SENSITIVE_TOP_LEVEL_KEYS:
                    if k in secret_saved:
                        saved[k] = copy.deepcopy(secret_saved[k])
        except Exception as e:
            print(f"加载敏感配置失败: {e}")
    if scope == 'basic':
        if os.path.exists(CONFIG_SECRET_FILE):
            try:
                with open(CONFIG_SECRET_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f) or {}
            except Exception:
                saved = {}
        if not isinstance(saved, dict):
            saved = {}

    # 执行参数：直接以请求体覆盖 config.json 非敏感部分，写回文件并重载 CONFIG；对限值参数做限制并返回 clamped 供前端提示
    if scope == 'execution':
        clamped = []
        for k in data:
            if k in SENSITIVE_TOP_LEVEL_KEYS:
                continue
            try:
                saved[k] = copy.deepcopy(data[k])
            except Exception:
                pass
        for key in EXEC_PARAM_LIMITS:
            if key not in saved:
                continue
            default = CONFIG.get(
                key,
                5
                if key == "delivery_warmup_max_retries"
                else (
                    8.0
                    if key == "delivery_warmup_budget_seconds"
                    else (2.2 if key == "delivery_min_post_interval_seconds" else 20.0)
                ),
            )
            raw_val = saved.get(key)
            val, was_clamped = _clamp_exec_param(key, raw_val, default)
            saved[key] = val
            if was_clamped:
                clamped.append({"key": key, "requested": raw_val, "saved": val})
        saved_public = {k: v for k, v in saved.items() if k not in SENSITIVE_TOP_LEVEL_KEYS}
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(saved_public, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入配置文件失败: {e}")
            return jsonify({"status": "error", "msg": str(e)})
        _load_config_from_disk()
        schedule_health_check()
        return jsonify({"status": "success", "clamped": clamped})

    if scope is None:
        if 'auth' not in saved:
            saved['auth'] = CONFIG.get('auth', {}).copy()
        if 'sms' not in saved:
            saved['sms'] = CONFIG.get('sms', {}).copy()

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

    # 1) 基础参数：手机号、PushPlus、sms
    if scope in (None, 'basic'):
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
        if 'sms' in data and isinstance(data['sms'], dict):
            CONFIG['sms'].update(data['sms'])
            if 'sms' not in saved:
                saved['sms'] = CONFIG.get('sms', {}).copy()
            else:
                saved['sms'].update(data['sms'])

    # 2) 执行参数：重试、超时、pipeline 等
    if scope in (None, 'execution'):
        _update_float_field('retry_interval', 0.1, CONFIG.get('retry_interval', 1.0))
        _update_float_field('aggressive_retry_interval', 0.1, CONFIG.get('aggressive_retry_interval', 0.3))
        _update_float_field('batch_retry_interval', 0.1, CONFIG.get('batch_retry_interval', 0.5))
        _update_float_field('submit_timeout_seconds', 0.5, CONFIG.get('submit_timeout_seconds', 4.0))
        _update_float_field('batch_min_interval', 0.1, CONFIG.get('batch_min_interval', 0.8))
        _update_float_field('fast_lane_seconds', 0.0, CONFIG.get('fast_lane_seconds', 2.0))
        _update_float_field('refill_window_seconds', 0.0, CONFIG.get('refill_window_seconds', 8.0))
        _update_float_field('matrix_timeout_seconds', 0.5, CONFIG.get('matrix_timeout_seconds', 3.0))
        _update_float_field('order_query_timeout_seconds', 0.5, cfg_get('order_query_timeout_seconds', 2.5))
        _update_float_field('post_submit_orders_join_timeout_seconds', 0.1, cfg_get('post_submit_orders_join_timeout_seconds', 0.3))
        _update_float_field('post_submit_verify_matrix_timeout_seconds', 0.3, cfg_get('post_submit_verify_matrix_timeout_seconds', 0.8))
        _update_float_field('post_submit_verify_pending_retry_seconds', 0.05, CONFIG.get('post_submit_verify_pending_retry_seconds', 0.35))
        _update_float_field('manual_verify_pending_retry_seconds', 0.05, CONFIG.get('manual_verify_pending_retry_seconds', 0.25))
        _update_float_field('health_check_interval_min', 1.0, CONFIG.get('health_check_interval_min', 30.0))
        _update_float_field('preselect_ttl_seconds', 0.2, CONFIG.get('preselect_ttl_seconds', 2.0))
        _update_float_field('delivery_backup_switch_delay_seconds', 0.0, CONFIG.get('delivery_backup_switch_delay_seconds', 2.0))
        if 'delivery_warmup_budget_seconds' in data:
            try:
                val, _ = _clamp_exec_param('delivery_warmup_budget_seconds', data['delivery_warmup_budget_seconds'], CONFIG.get('delivery_warmup_budget_seconds', 8.0))
                CONFIG['delivery_warmup_budget_seconds'] = val
                saved['delivery_warmup_budget_seconds'] = val
            except (TypeError, ValueError):
                pass
        if 'delivery_warmup_max_retries' in data:
            try:
                val, _ = _clamp_exec_param('delivery_warmup_max_retries', data['delivery_warmup_max_retries'], CONFIG.get('delivery_warmup_max_retries', 5))
                CONFIG['delivery_warmup_max_retries'] = val
                saved['delivery_warmup_max_retries'] = val
            except (TypeError, ValueError):
                pass
        if 'delivery_first_group_from_matrix' in data:
            CONFIG['delivery_first_group_from_matrix'] = bool(data['delivery_first_group_from_matrix'])
            saved['delivery_first_group_from_matrix'] = CONFIG['delivery_first_group_from_matrix']
        if 'delivery_preferred_place_min' in data:
            try:
                val = max(0, min(17, int(data['delivery_preferred_place_min'])))
                CONFIG['delivery_preferred_place_min'] = val
                saved['delivery_preferred_place_min'] = val
            except (TypeError, ValueError):
                pass
        if 'delivery_preferred_place_max' in data:
            try:
                val = max(0, min(17, int(data['delivery_preferred_place_max'])))
                CONFIG['delivery_preferred_place_max'] = val
                saved['delivery_preferred_place_max'] = val
            except (TypeError, ValueError):
                pass
        if 'delivery_target_blocks' in data:
            try:
                val = max(1, min(3, int(data['delivery_target_blocks'])))
                CONFIG['delivery_target_blocks'] = val
                saved['delivery_target_blocks'] = val
            except (TypeError, ValueError):
                pass
        if 'delivery_target_times' in data and isinstance(data.get('delivery_target_times'), list):
            CONFIG['delivery_target_times'] = [str(t).strip() for t in data['delivery_target_times'] if re.fullmatch(r'\d{2}:\d{2}', str(t).strip())]
            saved['delivery_target_times'] = copy.deepcopy(CONFIG['delivery_target_times'])
        if 'delivery_time_preference_order' in data and isinstance(data.get('delivery_time_preference_order'), list):
            CONFIG['delivery_time_preference_order'] = [str(t).strip() for t in data['delivery_time_preference_order'] if re.fullmatch(r'\d{2}:\d{2}', str(t).strip())]
            saved['delivery_time_preference_order'] = copy.deepcopy(CONFIG['delivery_time_preference_order'])

        for key in (
            'post_submit_verify_orders_on_matrix_partial_only', 'post_submit_skip_sync_orders_query',
            'post_submit_orders_sync_fallback', 'post_submit_treat_verify_timeout_as_retry',
            'manual_verify_pending_orders_fallback_enabled', 'too_fast_skip_refill_in_same_request',
            'multi_item_retry_balance_enabled', 'submit_adaptive_merge_same_time_only',
            'preselect_enabled', 'preselect_only_before_first_submit', 'health_check_enabled',
            'fast_lane_enabled', 'verbose_logs', 'log_to_file'
        ):
            if key not in data:
                continue
            val = data[key]
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG[key] = enabled
            saved[key] = enabled

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
                val = int(cfg_get('order_query_max_pages', 2))
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
        if 'submit_strategy_mode' in data:
            mode = str(data.get('submit_strategy_mode') or 'adaptive').strip().lower()
            if mode not in ('adaptive', 'fixed'):
                mode = 'adaptive'
            CONFIG['submit_strategy_mode'] = mode
            saved['submit_strategy_mode'] = mode
        for key, default, lo, hi in (
            ('submit_adaptive_target_batches', 2, 1, 6),
            ('submit_adaptive_min_batch_size', 1, 1, 9),
            ('submit_adaptive_max_batch_size', 3, 1, 9),
            ('submit_adaptive_merge_small_n', 2, 1, 9),
            ('post_submit_verify_matrix_recheck_times', 3, 0, 8),
            ('multi_item_batch_retry_times_cap', 1, 0, 3),
            ('multi_item_retry_total_budget', 3, 0, 20),
            ('log_retention_days', 3, 0, 90),
            ('transient_storm_threshold', 8, 1, 20),
            ('transient_storm_extend_timeout_after', 3, 1, 10),
            ('post_submit_verify_pending_matrix_recheck_times', 4, 0, 5),
            ('manual_verify_pending_recheck_times', 3, 0, 8),
            ('metrics_keep_last', 300, 50, 5000),
            ('metrics_retention_days', 7, 1, 30),
            ('same_time_precheck_limit', 0, 0, 9),
        ):
            if key not in data:
                continue
            try:
                val = int(data[key])
            except (TypeError, ValueError):
                val = int(CONFIG.get(key, default))
            val = max(lo, min(hi, val))
            CONFIG[key] = val
            saved[key] = val
        if 'submit_grouping_mode' in data:
            mode = str(data.get('submit_grouping_mode') or 'smart').strip().lower()
            if mode not in ('smart', 'place', 'timeslot'):
                mode = 'smart'
            CONFIG['submit_grouping_mode'] = mode
            saved['submit_grouping_mode'] = mode
        if 'health_check_start_time' in data:
            time_str = normalize_time_str(data['health_check_start_time'])
            if time_str:
                CONFIG['health_check_start_time'] = time_str
                saved['health_check_start_time'] = time_str
        for key in (
            'submit_timeout_backoff_seconds', 'transient_storm_backoff_seconds',
            'matrix_timeout_storm_seconds'
        ):
            if key not in data:
                continue
            try:
                val = float(data[key])
                if 'storm' in key or 'timeout' in key or 'backoff' in key:
                    val = max(0.5 if 'backoff' in key else 1.0, val)
                CONFIG[key] = val
                saved[key] = val
            except (TypeError, ValueError):
                pass
        if 'log_file_dir' in data and isinstance(data['log_file_dir'], str):
            CONFIG['log_file_dir'] = str(data['log_file_dir']).strip() or 'logs'
            saved['log_file_dir'] = CONFIG['log_file_dir']
        if 'manual_submit_profile' in data:
            val = str(data.get('manual_submit_profile') or 'manual_minimal').strip() or 'manual_minimal'
            CONFIG['manual_submit_profile'] = val
            saved['manual_submit_profile'] = val
        if 'auto_submit_profile' in data:
            val = str(data.get('auto_submit_profile') or 'auto_minimal').strip() or 'auto_minimal'
            CONFIG['auto_submit_profile'] = val
            saved['auto_submit_profile'] = val
        if 'submit_profiles' in data and isinstance(data.get('submit_profiles'), dict):
            merged_profiles = {}
            default_profiles = CONFIG.get('submit_profiles')
            if isinstance(default_profiles, dict):
                for k, v in default_profiles.items():
                    if isinstance(v, dict):
                        merged_profiles[str(k)] = dict(v)
            for k, v in (data.get('submit_profiles') or {}).items():
                key = str(k).strip()
                if not key or not isinstance(v, dict):
                    continue
                base = dict(merged_profiles.get(key) or {})
                base.update(v)
                merged_profiles[key] = base
            if merged_profiles:
                CONFIG['submit_profiles'] = merged_profiles
                saved['submit_profiles'] = copy.deepcopy(merged_profiles)

    # 写回
    if scope == 'execution':
        try:
            saved_public = {k: v for k, v in saved.items() if k not in SENSITIVE_TOP_LEVEL_KEYS}
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(saved_public, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入配置文件失败: {e}")
    elif scope == 'basic':
        try:
            saved_secret = {k: copy.deepcopy(saved[k]) for k in SENSITIVE_TOP_LEVEL_KEYS if k in saved}
            if not saved_secret:
                saved_secret = {k: copy.deepcopy(CONFIG.get(k)) for k in SENSITIVE_TOP_LEVEL_KEYS if CONFIG.get(k) is not None}
            if saved_secret:
                with open(CONFIG_SECRET_FILE, 'w', encoding='utf-8') as f:
                    json.dump(saved_secret, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入敏感配置文件失败: {e}")
    else:
        try:
            saved_public = {k: v for k, v in saved.items() if k not in SENSITIVE_TOP_LEVEL_KEYS}
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(saved_public, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入配置文件失败: {e}")
        try:
            saved_secret = {k: copy.deepcopy(saved[k]) for k in SENSITIVE_TOP_LEVEL_KEYS if k in saved}
            if saved_secret:
                with open(CONFIG_SECRET_FILE, 'w', encoding='utf-8') as f:
                    json.dump(saved_secret, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入敏感配置文件失败: {e}")
    schedule_health_check()
    return jsonify({"status": "success"})


@app.route('/api/config/execution', methods=['POST'])
def update_config_execution():
    """仅保存执行参数 → config.json。"""
    try:
        data = request.get_json() or {}
        return _update_config_impl(data, 'execution')
    except Exception as e:
        print(f"更新执行参数时异常: {e}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/config/basic', methods=['POST'])
def update_config_basic():
    """仅保存基础参数（通知手机号、PushPlus、sms）→ config.secret.json。"""
    try:
        data = request.get_json() or {}
        return _update_config_impl(data, 'basic')
    except Exception as e:
        print(f"更新基础参数时异常: {e}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/config', methods=['POST'])
def update_config():
    """
    更新全局配置（全量）。也可通过 /api/config/execution 或 /api/config/basic 分别只保存执行参数或基础参数。
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
    - submit_strategy_mode：首轮提交策略（adaptive/fixed）
    - submit_adaptive_target_batches：自适应策略目标批次数
    - submit_adaptive_min_batch_size：自适应策略最小首批大小
    - submit_adaptive_max_batch_size：自适应策略最大首批大小
    - submit_adaptive_merge_small_n：小目标数量时合批首击阈值（<=N 时可合批）
    - submit_adaptive_merge_same_time_only：仅同时间目标是否允许小N合批
    - submit_grouping_mode：提交分组模式（smart/place/timeslot）
    - batch_min_interval：批次间最小间隔
    - fast_lane_enabled：开抢快车道（仅必要时sleep）
    - fast_lane_seconds：快车道持续时间(秒)
    - refill_window_seconds：失败后补提窗口
    - matrix_timeout_seconds：查询矩阵超时(秒)，建议高峰期使用短超时
    - order_query_timeout_seconds：订单查询超时(秒)
    - order_query_max_pages：订单查询最大页数
    - post_submit_orders_join_timeout_seconds：提交后订单查询线程等待上限(秒)
    - post_submit_verify_matrix_timeout_seconds：提交后矩阵验证超时(秒)
    - post_submit_verify_matrix_recheck_times：提交后矩阵快速复核次数
    - post_submit_verify_orders_on_matrix_partial_only：仅在矩阵校验存在缺口时再查订单
    - post_submit_skip_sync_orders_query：提交后是否跳过同步订单查询(用矩阵快速确认)
    - post_submit_orders_sync_fallback：订单线程超时后是否同步兜底
    - post_submit_verify_pending_retry_seconds：验证未收敛时快速复核间隔(秒)
    - post_submit_verify_pending_matrix_recheck_times：verify_pending后仅做矩阵复核次数
    - manual_verify_pending_recheck_times：半自动verify_pending矩阵复核次数
    - manual_verify_pending_retry_seconds：半自动verify_pending复核间隔(秒)
    - manual_verify_pending_orders_fallback_enabled：半自动verify_pending是否启用一次订单兜底复核
    - too_fast_skip_refill_in_same_request：命中“操作过快/频繁”时是否跳过同请求内补提
    - multi_item_retry_balance_enabled：多项目提交时是否启用重试次数均衡
    - multi_item_batch_retry_times_cap：多项目提交时每批最大重试次数上限
    - multi_item_retry_total_budget：多项目提交时本次请求可消耗的总重试预算
    - post_submit_treat_verify_timeout_as_retry：验证超时是否走快速复核而非直接失败
    - log_to_file：是否将运行日志按天写入文件(便于次日查看)
    - log_file_dir：日志文件目录
    - log_retention_days：日志保留天数，0=不清理
    - submit_timeout_backoff_seconds：提交超时后重试前退避(秒)，减轻触发操作过快
    - transient_storm_threshold：连续 N 次 404/超时/非JSON 后触发退避
    - transient_storm_backoff_seconds：退避时长(秒)
    - matrix_timeout_storm_seconds：风暴期 get_matrix 使用的略长超时(秒)
    - transient_storm_extend_timeout_after：连续失败>=此数时使用风暴超时
    - health_check_enabled: 健康检查是否开启
    - health_check_interval_min: 健康检查间隔（分钟）
    - health_check_start_time: 健康检查起始时间（HH:MM）
    - verbose_logs: 是否输出高频调试日志
    - same_time_precheck_limit: 同时段预检上限（<=0 关闭）
    - preselect_enabled：是否启用解锁前预选快照
    - preselect_ttl_seconds：预选快照有效期(秒)
    - preselect_only_before_first_submit：仅首提前启用预选快照
    - metrics_keep_last：统一观测文件最大保留条数
    - metrics_retention_days：统一观测文件保留天数
    """
    try:
        data = request.json or {}

        # 读取旧配置：先 config.json，再以 config.secret.json 中敏感键覆盖（保证 auth / sms 等不丢）
        saved = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f) or {}
            except Exception as e:
                print(f"加载配置失败: {e}")
                saved = {}
        if os.path.exists(CONFIG_SECRET_FILE):
            try:
                with open(CONFIG_SECRET_FILE, 'r', encoding='utf-8') as f:
                    secret_saved = json.load(f)
                if isinstance(secret_saved, dict):
                    for k in SENSITIVE_TOP_LEVEL_KEYS:
                        if k in secret_saved:
                            saved[k] = copy.deepcopy(secret_saved[k])
            except Exception as e:
                print(f"加载敏感配置失败: {e}")

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
        _update_float_field('fast_lane_seconds', 0.0, CONFIG.get('fast_lane_seconds', 2.0))
        _update_float_field('refill_window_seconds', 0.0, CONFIG.get('refill_window_seconds', 8.0))
        _update_float_field('matrix_timeout_seconds', 0.5, CONFIG.get('matrix_timeout_seconds', 3.0))
        _update_float_field('order_query_timeout_seconds', 0.5, cfg_get('order_query_timeout_seconds', 2.5))
        _update_float_field('post_submit_orders_join_timeout_seconds', 0.1, cfg_get('post_submit_orders_join_timeout_seconds', 0.3))
        _update_float_field('post_submit_verify_matrix_timeout_seconds', 0.3, cfg_get('post_submit_verify_matrix_timeout_seconds', 0.8))
        _update_float_field('post_submit_verify_pending_retry_seconds', 0.05, CONFIG.get('post_submit_verify_pending_retry_seconds', 0.35))
        _update_float_field('manual_verify_pending_retry_seconds', 0.05, CONFIG.get('manual_verify_pending_retry_seconds', 0.25))
        _update_float_field('health_check_interval_min', 1.0, CONFIG.get('health_check_interval_min', 30.0))
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

        if 'post_submit_skip_sync_orders_query' in data:
            val = data['post_submit_skip_sync_orders_query']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['post_submit_skip_sync_orders_query'] = enabled
            saved['post_submit_skip_sync_orders_query'] = enabled

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

        if 'manual_verify_pending_orders_fallback_enabled' in data:
            val = data['manual_verify_pending_orders_fallback_enabled']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['manual_verify_pending_orders_fallback_enabled'] = enabled
            saved['manual_verify_pending_orders_fallback_enabled'] = enabled

        if 'too_fast_skip_refill_in_same_request' in data:
            val = data['too_fast_skip_refill_in_same_request']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['too_fast_skip_refill_in_same_request'] = enabled
            saved['too_fast_skip_refill_in_same_request'] = enabled

        if 'multi_item_retry_balance_enabled' in data:
            val = data['multi_item_retry_balance_enabled']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['multi_item_retry_balance_enabled'] = enabled
            saved['multi_item_retry_balance_enabled'] = enabled

        if 'multi_item_batch_retry_times_cap' in data:
            try:
                val = int(data['multi_item_batch_retry_times_cap'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('multi_item_batch_retry_times_cap', 1))
            val = max(0, min(3, val))
            CONFIG['multi_item_batch_retry_times_cap'] = val
            saved['multi_item_batch_retry_times_cap'] = val

        if 'multi_item_retry_total_budget' in data:
            try:
                val = int(data['multi_item_retry_total_budget'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('multi_item_retry_total_budget', 3))
            val = max(0, min(20, val))
            CONFIG['multi_item_retry_total_budget'] = val
            saved['multi_item_retry_total_budget'] = val

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
                val = int(cfg_get('order_query_max_pages', 2))
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

        if 'submit_strategy_mode' in data:
            mode = str(data.get('submit_strategy_mode') or 'adaptive').strip().lower()
            if mode not in ('adaptive', 'fixed'):
                mode = str(CONFIG.get('submit_strategy_mode', 'adaptive') or 'adaptive').strip().lower()
                if mode not in ('adaptive', 'fixed'):
                    mode = 'adaptive'
            CONFIG['submit_strategy_mode'] = mode
            saved['submit_strategy_mode'] = mode

        if 'submit_adaptive_target_batches' in data:
            try:
                val = int(data['submit_adaptive_target_batches'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('submit_adaptive_target_batches', 2))
            val = max(1, min(6, val))
            CONFIG['submit_adaptive_target_batches'] = val
            saved['submit_adaptive_target_batches'] = val

        if 'submit_adaptive_min_batch_size' in data:
            try:
                val = int(data['submit_adaptive_min_batch_size'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('submit_adaptive_min_batch_size', 1))
            val = max(1, min(9, val))
            CONFIG['submit_adaptive_min_batch_size'] = val
            saved['submit_adaptive_min_batch_size'] = val

        if 'submit_adaptive_max_batch_size' in data:
            try:
                val = int(data['submit_adaptive_max_batch_size'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('submit_adaptive_max_batch_size', 3))
            val = max(1, min(9, val))
            CONFIG['submit_adaptive_max_batch_size'] = val
            saved['submit_adaptive_max_batch_size'] = val

        if 'submit_adaptive_merge_small_n' in data:
            try:
                val = int(data['submit_adaptive_merge_small_n'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('submit_adaptive_merge_small_n', 2))
            val = max(1, min(9, val))
            CONFIG['submit_adaptive_merge_small_n'] = val
            saved['submit_adaptive_merge_small_n'] = val

        if 'submit_adaptive_merge_same_time_only' in data:
            val = data['submit_adaptive_merge_same_time_only']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['submit_adaptive_merge_same_time_only'] = enabled
            saved['submit_adaptive_merge_same_time_only'] = enabled

        if 'submit_grouping_mode' in data:
            mode = str(data.get('submit_grouping_mode') or 'smart').strip().lower()
            if mode not in ('smart', 'place', 'timeslot'):
                mode = str(CONFIG.get('submit_grouping_mode', 'smart') or 'smart').strip().lower()
                if mode not in ('smart', 'place', 'timeslot'):
                    mode = 'smart'
            CONFIG['submit_grouping_mode'] = mode
            saved['submit_grouping_mode'] = mode

        if 'pipeline_continuous_window_seconds' in data:
            try:
                val = int(data['pipeline_continuous_window_seconds'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('pipeline_continuous_window_seconds', 8))
            val = max(1, min(120, val))
            CONFIG['pipeline_continuous_window_seconds'] = val
            saved['pipeline_continuous_window_seconds'] = val

        if 'pipeline_random_window_seconds' in data:
            try:
                val = int(data['pipeline_random_window_seconds'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('pipeline_random_window_seconds', 12))
            val = max(1, min(180, val))
            CONFIG['pipeline_random_window_seconds'] = val
            saved['pipeline_random_window_seconds'] = val

        if 'pipeline_refill_interval_seconds' in data:
            try:
                val = int(data['pipeline_refill_interval_seconds'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('pipeline_refill_interval_seconds', 15))
            val = max(1, min(300, val))
            CONFIG['pipeline_refill_interval_seconds'] = val
            saved['pipeline_refill_interval_seconds'] = val

        if 'pipeline_greedy_end_before_hours' in data:
            try:
                val = float(data['pipeline_greedy_end_before_hours'])
            except (TypeError, ValueError):
                val = float(CONFIG.get('pipeline_greedy_end_before_hours', 24.0))
            val = max(0.0, val)
            CONFIG['pipeline_greedy_end_before_hours'] = val
            saved['pipeline_greedy_end_before_hours'] = val

        if 'log_to_file' in data:
            val = data['log_to_file']
            enabled = val if isinstance(val, bool) else str(val).lower() in ('1', 'true', 'yes', 'on')
            CONFIG['log_to_file'] = enabled
            saved['log_to_file'] = enabled
        if 'log_file_dir' in data and isinstance(data['log_file_dir'], str):
            CONFIG['log_file_dir'] = str(data['log_file_dir']).strip() or 'logs'
            saved['log_file_dir'] = CONFIG['log_file_dir']
        if 'log_retention_days' in data:
            try:
                val = max(0, min(90, int(data['log_retention_days'])))
                CONFIG['log_retention_days'] = val
                saved['log_retention_days'] = val
            except (TypeError, ValueError):
                pass
        if 'submit_timeout_backoff_seconds' in data:
            try:
                val = max(0.5, float(data['submit_timeout_backoff_seconds']))
                CONFIG['submit_timeout_backoff_seconds'] = val
                saved['submit_timeout_backoff_seconds'] = val
            except (TypeError, ValueError):
                pass
        if 'transient_storm_threshold' in data:
            try:
                val = max(1, min(20, int(data['transient_storm_threshold'])))
                CONFIG['transient_storm_threshold'] = val
                saved['transient_storm_threshold'] = val
            except (TypeError, ValueError):
                pass
        if 'transient_storm_backoff_seconds' in data:
            try:
                val = max(0.5, float(data['transient_storm_backoff_seconds']))
                CONFIG['transient_storm_backoff_seconds'] = val
                saved['transient_storm_backoff_seconds'] = val
            except (TypeError, ValueError):
                pass
        if 'matrix_timeout_storm_seconds' in data:
            try:
                val = max(1.0, float(data['matrix_timeout_storm_seconds']))
                CONFIG['matrix_timeout_storm_seconds'] = val
                saved['matrix_timeout_storm_seconds'] = val
            except (TypeError, ValueError):
                pass
        if 'transient_storm_extend_timeout_after' in data:
            try:
                val = max(1, min(10, int(data['transient_storm_extend_timeout_after'])))
                CONFIG['transient_storm_extend_timeout_after'] = val
                saved['transient_storm_extend_timeout_after'] = val
            except (TypeError, ValueError):
                pass
        if 'post_submit_verify_matrix_recheck_times' in data:
            try:
                val = int(data['post_submit_verify_matrix_recheck_times'])
            except (TypeError, ValueError):
                val = int(cfg_get('post_submit_verify_matrix_recheck_times', 3))
            val = max(0, min(8, val))
            CONFIG['post_submit_verify_matrix_recheck_times'] = val
            saved['post_submit_verify_matrix_recheck_times'] = val

        if 'metrics_keep_last' in data:
            try:
                val = int(data['metrics_keep_last'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('metrics_keep_last', 300))
            val = max(50, min(5000, val))
            CONFIG['metrics_keep_last'] = val
            saved['metrics_keep_last'] = val

        if 'metrics_retention_days' in data:
            try:
                val = int(data['metrics_retention_days'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('metrics_retention_days', 7))
            val = max(1, min(30, val))
            CONFIG['metrics_retention_days'] = val
            saved['metrics_retention_days'] = val

        if 'manual_verify_pending_recheck_times' in data:
            try:
                val = int(data['manual_verify_pending_recheck_times'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('manual_verify_pending_recheck_times', 3))
            val = max(0, min(8, val))
            CONFIG['manual_verify_pending_recheck_times'] = val
            saved['manual_verify_pending_recheck_times'] = val

        if 'post_submit_verify_pending_matrix_recheck_times' in data:
            try:
                val = int(data['post_submit_verify_pending_matrix_recheck_times'])
            except (TypeError, ValueError):
                val = int(CONFIG.get('post_submit_verify_pending_matrix_recheck_times', 4))
            val = max(0, min(5, val))
            CONFIG['post_submit_verify_pending_matrix_recheck_times'] = val
            saved['post_submit_verify_pending_matrix_recheck_times'] = val

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
        if 'fast_lane_enabled' in data:
            val = data['fast_lane_enabled']
            if isinstance(val, bool):
                enabled = val
            elif isinstance(val, str):
                enabled = val.lower() in ('1', 'true', 'yes', 'on')
            else:
                enabled = bool(val)
            CONFIG['fast_lane_enabled'] = enabled
            saved['fast_lane_enabled'] = enabled

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

        # 4) 写回：非敏感 → config.json，敏感 → config.secret.json
        try:
            saved_public = {k: v for k, v in saved.items() if k not in SENSITIVE_TOP_LEVEL_KEYS}
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(saved_public, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入配置文件失败: {e}")
        try:
            saved_secret = {k: copy.deepcopy(saved[k]) for k in SENSITIVE_TOP_LEVEL_KEYS if k in saved}
            if saved_secret:
                with open(CONFIG_SECRET_FILE, 'w', encoding='utf-8') as f:
                    json.dump(saved_secret, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入敏感配置文件失败: {e}")
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
        data = request.json or {}
        token = str(data.get('token') or '').strip()
        if not token:
            return jsonify({"status": "error", "msg": "Token缺失"})

        # cookie 允许为空（表示清空或保持当前值），但始终按表单内容写回
        cookie = str(data.get('cookie', '') or '').strip()

        # 更新内存中的 CONFIG 与 client
        CONFIG['auth']['token'] = token
        CONFIG['auth']['cookie'] = cookie

        client.token = token
        client.headers['Cookie'] = cookie if cookie else ''

        # 持久化保存：优先写入 config.secret.json，无则写回 config.json
        try:
            save_file = CONFIG_SECRET_FILE if os.path.exists(CONFIG_SECRET_FILE) else CONFIG_FILE
            saved = {}
            if os.path.exists(save_file):
                try:
                    with open(save_file, 'r', encoding='utf-8') as f:
                        saved = json.load(f) or {}
                except Exception:
                    saved = {}
            if not isinstance(saved, dict):
                saved = {}

            if 'auth' not in saved:
                saved['auth'] = {}
            saved['auth']['token'] = token
            saved['auth']['cookie'] = cookie
            saved['auth']['card_index'] = CONFIG['auth'].get('card_index', '')
            saved['auth']['card_st_id'] = CONFIG['auth'].get('card_st_id', '')
            saved['auth']['shop_num'] = CONFIG['auth'].get('shop_num', '')

            with open(save_file, 'w', encoding='utf-8') as f:
                json.dump(saved, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存Auth配置失败: {e}")
            # 即使保存失败，内存更新成功也算成功，但记录日志

        msg = "Token/Cookie 已更新" if cookie else "Token 已更新，Cookie 为空"
        return jsonify({"status": "success", "msg": msg})
    except Exception as e:
        print(f"Update Auth Error: {e}")
        return jsonify({"status": "error", "msg": f"服务器内部错误: {str(e)}"})

@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    return jsonify(task_manager.tasks)

@app.route('/api/tasks', methods=['POST'])
def add_task():
    data = request.json or {}
    try:
        task_manager.add_task(data)
        return jsonify({"status": "success"})
    except ValueError as e:
        return jsonify({"status": "error", "msg": str(e)}), 400

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
def del_task(task_id):
    task_manager.delete_task(task_id)
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>', methods=['PUT'])
def update_task(task_id):
    data = request.json or {}
    try:
        ok = task_manager.update_task(task_id, data)
    except ValueError as e:
        return jsonify({"status": "error", "msg": str(e)}), 400
    if not ok:
        return jsonify({"status": "error", "msg": "Task not found"}), 404
    return jsonify({"status": "success"})

@app.route('/api/tasks/<task_id>/run', methods=['POST'])
def run_task_now(task_id):
    # Find task
    task = next((t for t in task_manager.tasks if str(t['id']) == str(task_id)), None)
    if task:
        quiet_info = quiet_window_block_info(
            "run_task_now",
            requester_task_id=str(task_id),
            owner_allowed=True,
        )
        if quiet_info and str((quiet_info.get("quiet_window") or {}).get("owner_task_id") or "") != str(task_id):
            return jsonify({"status": "quiet_window_blocked", "msg": quiet_info.get("msg"), "quiet_window": quiet_info.get("quiet_window")})
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
    if is_quiet_window_active():
        if 'enabled' in data and bool(data.get('enabled')):
            quiet_info = quiet_window_block_info("add_refill_task", owner_allowed=False) or {}
            return jsonify({'status': 'quiet_window_blocked', 'msg': quiet_info.get('msg', '静默窗口中，暂不允许启用 Refill 任务。'), 'quiet_window': quiet_info.get('quiet_window')})
        if 'enabled' not in data:
            data['enabled'] = False
    task = task_manager.add_refill_task(data)
    return jsonify({'status': 'success', 'task': task})




@app.route('/api/refill-tasks/<task_id>', methods=['PUT'])
def update_refill_task_api(task_id):
    data = request.json or {}
    if bool(data.get('enabled', False)) and is_quiet_window_active():
        quiet_info = quiet_window_block_info("update_refill_task", owner_allowed=False) or {}
        return jsonify({'status': 'quiet_window_blocked', 'msg': quiet_info.get('msg', '静默窗口中，暂不允许启用 Refill 任务。'), 'quiet_window': quiet_info.get('quiet_window')})
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
    quiet_info = quiet_window_block_info("run_refill_task", owner_allowed=False)
    if quiet_info:
        return jsonify({'status': 'quiet_window_blocked', 'msg': quiet_info.get('msg'), 'quiet_window': quiet_info.get('quiet_window')})
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
    quiet_info = quiet_window_block_info("api_check_token", owner_allowed=False)
    if quiet_info:
        return jsonify({
            "status": "quiet_window_blocked",
            "msg": quiet_info.get("msg"),
            "quiet_window": quiet_info.get("quiet_window"),
        })
    with runtime_request_context("api_check_token", owner=False):
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


@app.route('/api/logs/file', methods=['GET'])
def get_logs_file():
    """返回运行日志文件内容（按天）或内存缓冲区，供前端弹窗查看/复制。"""
    date_str = (request.args.get('date') or '').strip()
    if not date_str:
        date_str = datetime.now().strftime('%Y%m%d')
    if len(date_str) != 8 or not date_str.isdigit():
        return jsonify({"error": "参数 date 需为 YYYYMMDD"}), 400
    lines = []
    if CONFIG.get('log_to_file'):
        log_dir = (CONFIG.get('log_file_dir') or 'logs').strip() or 'logs'
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_dir)
        log_path = os.path.join(log_dir, f'run_{date_str}.log')
        if os.path.isfile(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
            except Exception as e:
                return jsonify({"error": f"读取日志文件失败: {e}"}), 500
    if not lines:
        # 无文件或未开启落盘：返回当天内存缓冲区（与 /api/logs 同源）
        now_str = datetime.now().strftime('%Y%m%d')
        for line in LOG_BUFFER:
            if isinstance(line, str):
                lines.append(line + '\n')
    text = ''.join(lines) if lines else f'（{date_str} 暂无日志）'
    from flask import Response
    return Response(text, mimetype='text/plain; charset=utf-8')


def _ms_to_readable(ms):
    """将毫秒时间戳转为可读字符串 YYYY-MM-DD HH:MM:SS。"""
    if ms is None:
        return None
    try:
        t = int(ms) / 1000.0
        return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


@app.route('/api/run-metrics/export', methods=['GET'])
def export_run_metrics():
    """返回最近任务执行数据，每条附带可读时间，供前端查看/复制。"""
    limit = request.args.get('limit', default=100, type=int)
    limit = max(1, min(500, int(limit or 100)))
    source = str(request.args.get('source', 'all') or 'all').strip().lower()
    records = []
    if os.path.exists(TASK_RUN_METRICS_FILE):
        try:
            with open(TASK_RUN_METRICS_FILE, 'r', encoding='utf-8') as f:
                records = json.load(f) or []
        except Exception:
            records = []
    if not isinstance(records, list):
        records = []
    if source in ('auto', 'manual'):
        records = [r for r in records if str(r.get('source') or 'auto').lower() == source]
    records = records[-limit:]
    for r in records:
        r['started_at_readable'] = _ms_to_readable(r.get('started_at'))
        r['finished_at_readable'] = _ms_to_readable(r.get('finished_at'))
    return jsonify({'records': records, 'count': len(records)})


@app.route('/api/diagnostic/export', methods=['GET'])
def export_diagnostic():
    """
    一键导出诊断包：导出时间 + 最近任务执行数据(50条,可读时间) + 最近系统日志(1000行) + 执行参数(脱敏)。
    供用户下载后发给他方分析，不含 token/手机号等敏感信息。
    """
    from flask import Response
    now = datetime.now()
    export_time = now.strftime('%Y-%m-%d %H:%M:%S')
    filename = f'diagnostic_{now.strftime("%Y%m%d_%H%M%S")}.txt'
    sections = []

    # 1. 导出时间
    sections.append('=== 导出时间 ===')
    sections.append(export_time)
    sections.append('')

    # 2. 任务执行数据（最近 50 条，带可读时间）
    sections.append('=== 任务执行数据（最近50条）===')
    records = []
    if os.path.exists(TASK_RUN_METRICS_FILE):
        try:
            with open(TASK_RUN_METRICS_FILE, 'r', encoding='utf-8') as f:
                records = json.load(f) or []
        except Exception:
            records = []
    if not isinstance(records, list):
        records = []
    records = [dict(r) for r in records[-50:]]
    for r in records:
        r['started_at_readable'] = _ms_to_readable(r.get('started_at'))
        r['finished_at_readable'] = _ms_to_readable(r.get('finished_at'))
    sections.append(json.dumps(records, ensure_ascii=False, indent=2))
    sections.append('')

    # 3. 系统运行日志（最近 1000 行）
    sections.append('=== 系统运行日志（最近1000行）===')
    log_lines = []
    if CONFIG.get('log_to_file'):
        log_dir = (CONFIG.get('log_file_dir') or 'logs').strip() or 'logs'
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_dir)
        today_str = now.strftime('%Y%m%d')
        log_path = os.path.join(log_dir, f'run_{today_str}.log')
        if os.path.isfile(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    log_lines = f.readlines()
            except Exception:
                pass
    if not log_lines:
        for line in LOG_BUFFER:
            if isinstance(line, str):
                log_lines.append(line + '\n')
    log_lines = log_lines[-1000:]
    sections.append(''.join(log_lines) if log_lines else '（暂无日志）')
    sections.append('')

    # 4. 执行参数（脱敏）
    sections.append('=== 执行参数(脱敏，不含账号/通知等) ===')
    config_snapshot = {k: copy.deepcopy(CONFIG[k]) for k in CONFIG if k not in SENSITIVE_TOP_LEVEL_KEYS}
    sections.append(json.dumps(config_snapshot, ensure_ascii=False, indent=2))

    body = '\n'.join(sections)
    return Response(
        body,
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/run-metrics', methods=['GET'])
def get_run_metrics():
    task_id = request.args.get('task_id')
    source = str(request.args.get('source', 'all') or 'all').strip().lower()
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
    if source in ('auto', 'manual'):
        records = [r for r in records if str(r.get('source') or 'auto').lower() == source]
    if unlock_only and source != 'manual':
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
        'source': source,
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
