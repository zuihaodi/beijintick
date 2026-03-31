"""
变更记录（手动维护）:
- 2026-03-29 定时任务触发改为后台线程执行，同 run_time 的多任务可与 run_pending 同迭代内并行启动（不再串行阻塞）
- 2026-03-25 极速递送 warmup：矩阵无 locked 为主开约信号、meta 暴露 last_day_open_time、未开约先睡至开约时刻再主组 POST 一次、首组矩阵无解回退主组 items
- 2026-03-21 delivery_target_blocks 取消全局/profile 默认；由任务 config 显式值或主组 items 推导
- 2026-02-09 03:29 保留健康检查调度并统一任务通知/结果上报
- 2026-02-09 04:10 健康检查增加起始时间并在前端显示预计下次检查
- 2026-02-09 04:40 接入 PushPlus 并增加微信通知配置入口
"""

from flask import Flask, render_template, request, jsonify
from flask_httpauth import HTTPBasicAuth
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    按账号执行：某账号处于静默窗口时跳过该账号，不影响其他账号探测。
    """
    phones = CONFIG.get('notification_phones') or []
    pushplus_tokens = CONFIG.get('pushplus_tokens') or []
    today = datetime.now().strftime("%Y-%m-%d")
    accounts = ensure_accounts_config()
    any_fail = False
    last_err = ""
    for idx, acc in enumerate(accounts):
        label = str(acc.get("name") or acc.get("id") or f"账号{idx + 1}")
        scope_h = build_quiet_window_scope(auth=acc)
        if quiet_window_block_info("health_check", owner_allowed=False, scope=scope_h):
            log(f"🔇 [health_check] 已跳过账号 {label}（静默窗口中）")
            continue
        c = build_client_for_account(acc)
        with runtime_request_context("health_check", owner=False):
            matrix_res = c.get_matrix(today)
        if "error" in matrix_res:
            err_msg = matrix_res["error"]
            any_fail = True
            last_err = err_msg
            log(f"❌ 健康检查失败({label}): 获取场地状态异常: {err_msg}")
            continue
        with runtime_request_context("health_check", owner=False):
            booking_probe = c.check_booking_auth_probe()
        if booking_probe.get('ok') and booking_probe.get('unknown'):
            log(f"✅ 健康检查通过({label})：场地状态获取正常；⚠️ 下单链路仅完成探测，结果未确认( {booking_probe.get('msg')} )")
        elif booking_probe.get('ok'):
            log(f"✅ 健康检查通过({label})：场地状态获取正常；下单鉴权探测未见明显异常")
        else:
            if booking_probe.get('unknown'):
                log(f"✅ 健康检查通过({label})：场地状态获取正常；⚠️ 下单链路探测异常/未知( {booking_probe.get('msg')} )")
            else:
                log(f"⚠️ 健康检查({label})：查询正常，但下单链路疑似鉴权异常( {booking_probe.get('msg')} )")
    if any_fail and last_err:
        if phones:
            task_manager.send_notification(f"⚠️ 健康检查失败：获取场地状态异常({last_err})", phones=phones)
        if pushplus_tokens:
            task_manager.send_wechat_notification(
                f"⚠️ 健康检查失败：获取场地状态异常({last_err})",
                tokens=pushplus_tokens,
            )

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
    "accounts": [
        {
            "id": "acc_1",
            "name": "账号1",
            "token": "oy9Aj1fKpR3Yxwd6iV7VIlg3Vo-A",
            "cookie": "JSESSIONID=FFE6C0633F33D9CE71354D0D1110AC0D",
            "card_index": "0873612446",
            "card_st_id": "289",
            "shop_num": "1001",
        }
    ],
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
    "manual_auto_refill_enabled": True,
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
    # Web 管理界面 HTTP Basic（可选，建议在 config.secret.json 中开启并填写）
    "web_ui_auth": {"enabled": False, "username": "", "password": ""},
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
    "delivery_refill_matrix_poll_seconds": 0.35,
    "delivery_refill_no_candidate_streak_limit": 0,
    "delivery_total_budget_seconds": 600.0,
    "delivery_min_post_interval_seconds": 2.2,
    "delivery_backup_switch_delay_seconds": 2.0,
    "delivery_warmup_max_retries": 200,
    "delivery_warmup_budget_seconds": 300.0,
    "max_items_per_batch": 6,
    "max_consecutive_slots_per_place": 3,
    "delivery_submit_granularity": "per_legal_batch",
    "manual_submit_profile": "manual_minimal",
    "auto_submit_profile": "auto_minimal",
    "submit_profiles": {
        "auto_minimal": {
            "minimal_direct_mode": True,
        },
        "manual_minimal": {
            "minimal_direct_mode": True,
        },
    },
    # 馆方 API 探测：保存在 config.json，不含 token（发请求时从所选账号注入）
    "gym_api_probe_presets": [],
}


# 执行参数上下限（唯一定义处）：总时长 1800 秒，拉活总时长 900 秒，拉活重试 300 次；超出时限制并提示用户
EXEC_PARAM_LIMITS = {
    "delivery_warmup_max_retries": (1, 9900),
    "delivery_total_budget_seconds": (3.0, 9900.0),
    "delivery_warmup_budget_seconds": (1.0, 9900.0),
    "delivery_min_post_interval_seconds": (0.0, 120.0),
    "delivery_refill_no_candidate_streak_limit": (0, 5000),
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


def strip_delivery_keys_from_profiles(cfg):
    """递送相关键仅允许出现在 config 顶层；从各 submit_profiles 子字典中剥离 delivery_*。"""
    profiles = cfg.get("submit_profiles") if isinstance(cfg, dict) else None
    if not isinstance(profiles, dict):
        return
    stripped = False
    for _pname, pdict in list(profiles.items()):
        if not isinstance(pdict, dict):
            continue
        for _k in list(pdict.keys()):
            if isinstance(_k, str) and _k.startswith("delivery_"):
                pdict.pop(_k, None)
                stripped = True
    if stripped:
        print("[config] stripped delivery_* from submit_profiles (场地策略在任务 config，不在 profile)")


# 场地/时段策略：按任务 config 为准；无任务键时由主组 items 推导。不写入全局 CONFIG（加载磁盘后会剔除）。
TASK_VENUE_STRATEGY_DELIVERY_KEYS = frozenset(
    {
        "delivery_first_group_from_matrix",
        "delivery_first_group_times",
        "delivery_first_group_time_preference_order",
        "delivery_target_blocks",
        "delivery_target_times",
        "delivery_time_preference_order",
        "delivery_preferred_place_min",
        "delivery_preferred_place_max",
        "delivery_matrix_place_min",
        "delivery_matrix_place_max",
    }
)

ALLOWED_SUBMIT_PROFILE_NAMES = frozenset({"auto_minimal", "manual_minimal"})

# ---------- 馆方 API 探测（仅 easyserpClient，服务端代发）----------
GYM_API_PROBE_MAX_PRESETS = 30
GYM_API_PROBE_MAX_LABEL_LEN = 80
GYM_API_PROBE_MAX_ID_LEN = 64
GYM_API_PROBE_MAX_QUERY_KEYS = 48
GYM_API_PROBE_MAX_BODY_BYTES = 16384
GYM_API_PROBE_MAX_RESPONSE_BYTES = 262144
GYM_API_PROBE_WRITE_SUBSTRINGS = frozenset(
    (
        "reservationplace",
        "cancleplaceappointment",
    )
)
GYM_API_PROBE_RESP_HEADER_ALLOW = frozenset(
    {"content-type", "date", "server", "content-encoding"}
)


def normalize_gym_api_probe_path(path_raw):
    p = str(path_raw or "").strip().replace("\\", "/").lstrip("/")
    if not p or ".." in p or "://" in p.lower() or p.startswith("//"):
        raise ValueError("path 非法")
    seg0, _, rest = p.partition("/")
    if seg0.lower() == "easyserpclient":
        inner = rest.lstrip("/")
    else:
        inner = p
    if not inner:
        raise ValueError("path 须包含 easyserpClient/ 下具体接口路径")
    path_norm = "easyserpClient/" + inner
    return path_norm


def gym_api_probe_path_needs_confirm(path_norm):
    low = str(path_norm or "").lower()
    return any(fragment in low for fragment in GYM_API_PROBE_WRITE_SUBSTRINGS)


def _strip_keys_from_mapping_case_insensitive(m, keys_lower):
    if not isinstance(m, dict) or not keys_lower:
        return
    to_del = []
    for k in list(m.keys()):
        lk = str(k).lower()
        if lk in keys_lower:
            to_del.append(k)
    for k in to_del:
        m.pop(k, None)


def sanitize_gym_api_probe_presets_for_persist(raw):
    """校验并清洗预设列表以便写入 config.json；返回 (list, 错误信息)。"""
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return None, "gym_api_probe_presets 须为数组"
    if len(raw) > GYM_API_PROBE_MAX_PRESETS:
        return None, f"gym_api_probe_presets 最多 {GYM_API_PROBE_MAX_PRESETS} 条"
    out = []
    seen_ids = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"预案 #{idx + 1} 须为对象"
        pid = str(item.get("id") or "").strip()
        if not pid or len(pid) > GYM_API_PROBE_MAX_ID_LEN:
            return None, f"预案 #{idx + 1} id 无效"
        if pid in seen_ids:
            return None, f"预案 id 重复: {pid}"
        seen_ids.add(pid)
        label = str(item.get("label") or "").strip()
        if not label:
            label = pid
        if len(label) > GYM_API_PROBE_MAX_LABEL_LEN:
            return None, f"预案 #{idx + 1} 名称过长"
        method = str(item.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            return None, f"预案 #{idx + 1} method 仅能为 GET/POST"
        try:
            path_norm = normalize_gym_api_probe_path(item.get("path"))
        except ValueError as e:
            return None, f"预案 #{idx + 1} path: {e}"
        query = item.get("query")
        if query is None:
            query = {}
        if not isinstance(query, dict):
            return None, f"预案 #{idx + 1} query 须为对象"
        if len(query) > GYM_API_PROBE_MAX_QUERY_KEYS:
            return None, f"预案 #{idx + 1} query 键过多"
        query_out = {}
        for k, v in query.items():
            ks = str(k).strip()
            if not ks:
                continue
            query_out[ks] = "" if v is None else str(v)
        inject_auth = item.get("inject_auth")
        if inject_auth is None:
            inject_auth = True
        inject_auth = bool(inject_auth)
        inject_card = bool(item.get("inject_card_fields"))
        if inject_auth:
            _strip_keys_from_mapping_case_insensitive(query_out, frozenset({"token", "shopnum"}))
        body_mode = str(item.get("body_mode") or "form").strip().lower()
        if body_mode not in ("form", "json"):
            body_mode = "form"
        body = "" if item.get("body") is None else str(item.get("body"))
        if len(body.encode("utf-8")) > GYM_API_PROBE_MAX_BODY_BYTES:
            return None, f"预案 #{idx + 1} body 过长"
        out.append(
            {
                "id": pid,
                "label": label,
                "method": method,
                "path": path_norm,
                "query": query_out,
                "body": body,
                "body_mode": body_mode,
                "inject_auth": inject_auth,
                "inject_card_fields": inject_card,
            }
        )
    return out, ""


def sanitize_submit_profiles(cfg):
    """仅保留极简 profile；修正 auto/manual_submit_profile 引用。"""
    if not isinstance(cfg, dict):
        return
    profiles = cfg.get("submit_profiles")
    if not isinstance(profiles, dict):
        return
    removed = []
    for pname in list(profiles.keys()):
        pk = str(pname).strip()
        if pk not in ALLOWED_SUBMIT_PROFILE_NAMES:
            profiles.pop(pname, None)
            removed.append(pk)
    for key, fallback in (("auto_submit_profile", "auto_minimal"), ("manual_submit_profile", "manual_minimal")):
        pn = str(cfg.get(key) or "").strip()
        if not pn or pn not in profiles:
            cfg[key] = fallback
    for name in ALLOWED_SUBMIT_PROFILE_NAMES:
        if name not in profiles:
            profiles[name] = {"minimal_direct_mode": True}
    if removed:
        print("[config] removed legacy submit_profiles: " + ", ".join(removed))


def _derive_venue_strategy_from_primary_items(items):
    """
    无任务级场地配置时，由主组 items 推导时段集合（去重后按 HH:MM 排序）、块数、矩阵场地号范围。
    时段优先顺序与集合一致（字典序）；首组按矩阵默认 False。
    """
    normalized = normalize_booking_items(items or [])
    if not normalized:
        return {
            "delivery_first_group_from_matrix": False,
            "delivery_first_group_times": [],
            "delivery_first_group_time_preference_order": [],
            "delivery_target_blocks": 1,
            "delivery_target_times": [],
            "delivery_time_preference_order": [],
            "delivery_preferred_place_min": 0,
            "delivery_preferred_place_max": 0,
            "delivery_matrix_place_min": 1,
            "delivery_matrix_place_max": 14,
        }
    times_sorted = sorted(
        {
            str(it.get("time") or "").strip()
            for it in normalized
            if re.fullmatch(r"\d{2}:\d{2}", str(it.get("time") or "").strip())
        }
    )
    places = []
    for it in normalized:
        p = str(it.get("place") or "").strip()
        if p.isdigit():
            places.append(int(p))
    mlo, mhi = 1, 14
    if places:
        mlo = max(1, min(50, min(places)))
        mhi = max(1, min(50, max(places)))
        if mlo > mhi:
            mlo, mhi = mhi, mlo
    blocks = max(1, min(3, int(_delivery_target_blocks_from_items(normalized))))
    return {
        "delivery_first_group_from_matrix": False,
        "delivery_first_group_times": list(times_sorted),
        "delivery_first_group_time_preference_order": list(times_sorted),
        "delivery_target_blocks": blocks,
        "delivery_target_times": list(times_sorted),
        "delivery_time_preference_order": list(times_sorted),
        "delivery_preferred_place_min": 0,
        "delivery_preferred_place_max": 0,
        "delivery_matrix_place_min": mlo,
        "delivery_matrix_place_max": mhi,
    }


def _merge_task_venue_strategy(task_config, primary_items):
    """任务 config 覆盖推导值；列表类仅当非空时覆盖。"""
    base = _derive_venue_strategy_from_primary_items(primary_items)
    if not isinstance(task_config, dict):
        return base
    out = dict(base)
    if "delivery_first_group_from_matrix" in task_config:
        out["delivery_first_group_from_matrix"] = bool(task_config["delivery_first_group_from_matrix"])
    if "delivery_target_blocks" in task_config and task_config["delivery_target_blocks"] is not None:
        try:
            out["delivery_target_blocks"] = max(1, min(3, int(task_config["delivery_target_blocks"])))
        except (TypeError, ValueError):
            pass
    for lk in (
        "delivery_first_group_times",
        "delivery_first_group_time_preference_order",
        "delivery_target_times",
        "delivery_time_preference_order",
    ):
        if lk not in task_config:
            continue
        raw = task_config[lk]
        if not isinstance(raw, list):
            continue
        cleaned = [str(t).strip() for t in raw if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())]
        if cleaned:
            out[lk] = cleaned
    for pk in ("delivery_preferred_place_min", "delivery_preferred_place_max"):
        if pk not in task_config:
            continue
        try:
            out[pk] = max(0, min(17, int(task_config[pk])))
        except (TypeError, ValueError):
            pass
    if "delivery_matrix_place_min" in task_config or "delivery_matrix_place_max" in task_config:
        try:
            lo, hi = _normalized_matrix_place_span(
                task_config.get("delivery_matrix_place_min"),
                task_config.get("delivery_matrix_place_max"),
            )
            out["delivery_matrix_place_min"] = lo
            out["delivery_matrix_place_max"] = hi
        except Exception:
            pass
    if "delivery_submit_granularity" in task_config:
        _g = str(task_config.get("delivery_submit_granularity") or "").strip().lower()
        if _g in ("per_legal_batch", "single_cell"):
            out["delivery_submit_granularity"] = _g
    return out


def validate_task_venue_strategy(cfg):
    """保存自动任务前校验任务内场地策略。cfg 为 task['config']。"""
    errs = []
    if not isinstance(cfg, dict):
        return errs
    if not cfg.get("delivery_first_group_from_matrix"):
        return errs
    ft = cfg.get("delivery_first_group_times")
    if not isinstance(ft, list) or len(ft) == 0:
        errs.append("已开启首组按矩阵计算，请在任务中配置首组目标时段 delivery_first_group_times")
        return errs
    fo = cfg.get("delivery_first_group_time_preference_order")
    if isinstance(fo, list) and fo:
        fts = {str(t).strip() for t in ft if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())}
        for t in fo:
            s = str(t).strip()
            if re.fullmatch(r"\d{2}:\d{2}", s) and s not in fts:
                errs.append(f"delivery_first_group_time_preference_order 含不在 delivery_first_group_times 中的项: {s}")
                break
    tt = cfg.get("delivery_target_times")
    to = cfg.get("delivery_time_preference_order")
    if isinstance(tt, list) and isinstance(to, list) and tt and to:
        ts_set = {str(x).strip() for x in tt if re.fullmatch(r"\d{2}:\d{2}", str(x).strip())}
        for t in to:
            s = str(t).strip()
            if re.fullmatch(r"\d{2}:\d{2}", s) and s not in ts_set:
                errs.append(f"delivery_time_preference_order 含不在 delivery_target_times 中的项: {s}")
                break
    return errs


def _strip_venue_strategy_from_mapping(m):
    if not isinstance(m, dict):
        return
    for k in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
        m.pop(k, None)


def validate_required_execution_config(cfg):
    """
    极速订场 / 执行参数必填校验。返回错误字符串列表，空表示通过。
    """
    if not isinstance(cfg, dict):
        return ["配置根须为对象"]

    errs = []
    num_float = (
        "delivery_total_budget_seconds",
        "delivery_warmup_budget_seconds",
        "delivery_min_post_interval_seconds",
        "delivery_transport_round_interval_seconds",
        "delivery_refill_matrix_poll_seconds",
        "submit_timeout_seconds",
        "matrix_timeout_seconds",
    )
    for k in num_float:
        if k not in cfg or cfg.get(k) is None or (isinstance(cfg.get(k), str) and str(cfg.get(k)).strip() == ""):
            errs.append(f"缺少或为空: {k}")
            continue
        try:
            v = float(cfg[k])
        except (TypeError, ValueError):
            errs.append(f"{k} 须为数值")
            continue
        if v != v:  # NaN
            errs.append(f"{k} 须为有效数值")
            continue
        if k in ("submit_timeout_seconds", "matrix_timeout_seconds") and v < 0.5:
            errs.append(f"{k} 须 >= 0.5")
        if k == "delivery_refill_matrix_poll_seconds" and v < 0.05:
            errs.append("delivery_refill_matrix_poll_seconds 须 >= 0.05")
        if k == "delivery_transport_round_interval_seconds" and v < 0.05:
            errs.append("delivery_transport_round_interval_seconds 须 >= 0.05")
        if k in EXEC_PARAM_LIMITS:
            lo, hi = EXEC_PARAM_LIMITS[k]
            if v < lo or v > hi:
                errs.append(f"{k} 须在 [{lo}, {hi}] 内，当前为 {v}")

    if "delivery_warmup_max_retries" not in cfg or cfg.get("delivery_warmup_max_retries") is None:
        errs.append("缺少或为空: delivery_warmup_max_retries")
    else:
        try:
            wr = int(cfg["delivery_warmup_max_retries"])
            lo, hi = EXEC_PARAM_LIMITS["delivery_warmup_max_retries"]
            if wr < lo or wr > hi:
                errs.append(f"delivery_warmup_max_retries 须在 [{lo}, {hi}] 内")
        except (TypeError, ValueError):
            errs.append("delivery_warmup_max_retries 须为整数")

    if "delivery_refill_no_candidate_streak_limit" not in cfg or cfg.get("delivery_refill_no_candidate_streak_limit") is None:
        errs.append("缺少或为空: delivery_refill_no_candidate_streak_limit")
    else:
        try:
            sl = int(cfg["delivery_refill_no_candidate_streak_limit"])
            lo, hi = EXEC_PARAM_LIMITS["delivery_refill_no_candidate_streak_limit"]
            if sl < lo or sl > hi:
                errs.append(f"delivery_refill_no_candidate_streak_limit 须在 [{lo}, {hi}] 内，0 表示关闭早停")
        except (TypeError, ValueError):
            errs.append("delivery_refill_no_candidate_streak_limit 须为整数")

    for bk, lo, hi in (
        ("max_items_per_batch", 1, 12),
        ("max_consecutive_slots_per_place", 1, 6),
    ):
        if bk not in cfg or cfg.get(bk) is None:
            errs.append(f"缺少或为空: {bk}")
        else:
            try:
                iv = int(cfg[bk])
                if iv < lo or iv > hi:
                    errs.append(f"{bk} 须在 [{lo}, {hi}]")
            except (TypeError, ValueError):
                errs.append(f"{bk} 须为整数")

    return errs


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_TEMPLATE_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_FILE = CONFIG_TEMPLATE_FILE
CONFIG_SECRET_FILE = os.path.join(BASE_DIR, "config.secret.json")
# 敏感配置：仅从 config.secret.json 读写，不写入 config.json，便于与执行参数分离
SENSITIVE_TOP_LEVEL_KEYS = frozenset(
    {"accounts", "auth", "notification_phones", "pushplus_tokens", "sms", "web_ui_auth"}
)
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
    "max_places_per_timeslot",
    "delivery_refill_max_places_per_timeslot",
})
LOG_BUFFER = []
MAX_LOG_SIZE = 500
# 与 client.server_time_offset_seconds 同步，供 log() 前缀估计服务器时间（HTTP Date）
_LOG_TIME_OFFSET_SECONDS = 0.0
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

TRANSPORT_ERROR_EVENTS_MAX = 30
METRICS_LATENCY_SAMPLES_KEEP = 80


def classify_transport_error_text(msg):
    """将 get_matrix / 传输层错误文案归为 metrics 桶（与 POST 侧 timeout_count 口径分离）。"""
    lower = str(msg or "").lower()
    if "timed out" in lower or "timeout" in lower or "read time" in lower:
        return "timeout"
    if "connecttimeout" in lower.replace(" ", ""):
        return "timeout"
    if any(
        x in lower
        for x in (
            "connection reset",
            "connection refused",
            "connection aborted",
            "broken pipe",
            "name or service not known",
            "getaddrinfo failed",
            "network is unreachable",
            "max retries exceeded",
        )
    ):
        return "connection_error"
    if "404" in lower:
        return "resp_404"
    if any(x in lower for x in ("502", "503", "504", "bad gateway", "service unavailable", "nginx")):
        return "resp_5xx"
    return "other"


def append_transport_error_event(run_metric, phase, bucket, snippet, elapsed_ms=None):
    if not isinstance(run_metric, dict):
        return
    ev = {"phase": str(phase or ""), "bucket": str(bucket or ""), "snippet": str(snippet or "")[:120]}
    if elapsed_ms is not None:
        try:
            ev["elapsed_ms"] = int(elapsed_ms)
        except Exception:
            pass
    lst = run_metric.setdefault("transport_error_events", [])
    lst.append(ev)
    over = len(lst) - TRANSPORT_ERROR_EVENTS_MAX
    if over > 0:
        del lst[0:over]


def record_matrix_fetch_failure(run_metric, phase, err_msg, elapsed_ms=None):
    if not isinstance(run_metric, dict):
        return
    run_metric["matrix_fetch_fail_count"] = int(run_metric.get("matrix_fetch_fail_count") or 0) + 1
    b = classify_transport_error_text(err_msg)
    if b == "timeout":
        run_metric["matrix_timeout_count"] = int(run_metric.get("matrix_timeout_count") or 0) + 1
    elif b == "connection_error":
        run_metric["matrix_connection_error_count"] = int(run_metric.get("matrix_connection_error_count") or 0) + 1
    elif b == "resp_404":
        run_metric["matrix_resp_404_count"] = int(run_metric.get("matrix_resp_404_count") or 0) + 1
    elif b == "resp_5xx":
        run_metric["matrix_resp_5xx_count"] = int(run_metric.get("matrix_resp_5xx_count") or 0) + 1
    append_transport_error_event(run_metric, phase, b, str(err_msg or "")[:200], elapsed_ms)


def log(msg):
    """记录日志到内存缓冲区、控制台，可选按天落盘便于次日查看"""
    print(msg)
    timestamp = (
        datetime.now() + timedelta(seconds=float(_LOG_TIME_OFFSET_SECONDS or 0.0))
    ).strftime("%H:%M:%S")
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


def mine_places_by_time_from_matrix(matrix_live, places_list, target_times):
    """各目标时段下已订(min)场地号列表，供求解时邻接偏好排序。"""
    out = {}
    for t in target_times:
        ts = str(t).strip()
        acc = []
        for p in places_list:
            cell = (matrix_live.get(str(p)) or {}).get(ts)
            if _matrix_cell_is_mine(cell):
                acc.append(str(p))
        if acc:
            out[ts] = sorted(acc, key=lambda x: int(x))
    return out


def _place_distance_to_mine_set(p_str, mine_place_strs):
    """场地号到已订集合的最小编号距离；mine 为空时返回 0（无偏序）。"""
    if not mine_place_strs:
        return 0
    pi = int(p_str)
    nums = [int(m) for m in mine_place_strs if str(m).isdigit()]
    if not nums:
        return 0
    return min(abs(pi - n) for n in nums)


# 固定东八区（不依赖 IANA 数据库；Windows/群晖/Docker 均可用，等价于中国现行标准时间）
_CN_TZ = timezone(timedelta(hours=8))


def _pick_tb_appoint_config(data_dict, short_name):
    if not isinstance(data_dict, dict):
        return None
    cfgs = data_dict.get("tbAppointConfigs") or []
    want = str(short_name or "").strip().lower()
    for c in cfgs:
        if not isinstance(c, dict):
            continue
        if str(c.get("shortname", "")).strip().lower() == want:
            return c
    return None


def _parse_last_day_open_time(s):
    if not s or not isinstance(s, str):
        return datetime.strptime("12:00:00", "%H:%M:%S").time()
    s = s.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return datetime.strptime("12:00:00", "%H:%M:%S").time()


def booking_date_scope_from_appoint(date_str, appoint_cfg):
    """past | unlocked | future — 由 tbAppointConfigs 的 appointmenttime 与 lastDayOpenTime 决定。"""
    if not isinstance(appoint_cfg, dict):
        return "unlocked"
    try:
        n = int(appoint_cfg.get("appointmenttime", appoint_cfg.get("appointmentTime")) or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "unlocked"
    try:
        target_date = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
    except Exception:
        return "unlocked"
    now_cn = datetime.now(_CN_TZ)
    today_cn = now_cn.date()
    diff_days = (target_date - today_cn).days
    if diff_days < 0:
        return "past"
    if diff_days > n:
        return "future"
    if diff_days < n:
        return "unlocked"
    open_t = _parse_last_day_open_time(
        appoint_cfg.get("lastDayOpenTime") or appoint_cfg.get("lastdayopentime")
    )
    if now_cn.time() >= open_t:
        return "unlocked"
    return "future"


def matrix_booking_open_by_no_locked_cells(matrix):
    """
    矩阵中若存在任一 locked 格，视为尚未开约；若至少有一格且全无 locked，视为已开约。
    与「未开约时全场 locked」的馆方常见行为对齐；不依赖 available。
    返回 True / False / None（None：无有效格点，无法据此判断，应回退日历 scope）。
    """
    if not isinstance(matrix, dict) or not matrix:
        return None
    cell_n = 0
    for _p, row in matrix.items():
        if not isinstance(row, dict):
            continue
        for _t, st in row.items():
            cell_n += 1
            if st == "locked":
                return False
    if cell_n == 0:
        return None
    return True


def seconds_until_today_open_time_cn(open_t):
    """距北京时间「今天」open_t 的秒数；已过则 0。"""
    if not open_t:
        return 0.0
    now = datetime.now(_CN_TZ)
    target = datetime(
        now.year,
        now.month,
        now.day,
        open_t.hour,
        open_t.minute,
        open_t.second,
        open_t.microsecond,
        tzinfo=_CN_TZ,
    )
    return max(0.0, (target - now).total_seconds())


def map_slot_state_int(state_int, locked_state_values_set):
    if state_int == 1:
        return "available"
    if state_int == 2:
        return "mine"
    if state_int == 4:
        return "booked"
    if state_int == 6:
        return "locked"
    if state_int in locked_state_values_set:
        return "locked"
    return "booked"


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


def _normalized_matrix_place_span(lo_raw, hi_raw):
    """矩阵求解可选场地号闭区间；缺省 1–14，限制在 [1, cap]。"""
    cap = 50
    if lo_raw is None:
        lo = 1
    else:
        try:
            lo = int(lo_raw)
        except (TypeError, ValueError):
            lo = 1
    if hi_raw is None:
        hi = 14
    else:
        try:
            hi = int(hi_raw)
        except (TypeError, ValueError):
            hi = 14
    lo = max(1, min(lo, cap))
    hi = max(1, min(hi, cap))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _selectable_place_bounds_from_intent(intent):
    raw = intent or {}
    return _normalized_matrix_place_span(raw.get("selectable_place_min"), raw.get("selectable_place_max"))


def compute_first_group_from_matrix(matrix, places, times, config_or_dict):
    """
    从拉活得到的 matrix 计算第一组 group_items（偏好场地 + 块数×时间降级）。
    可选场地号段由配置决定（默认 1–14）；仅 matrix[p][t]=="available" 可订。
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

    span_lo, span_hi = _normalized_matrix_place_span(
        cfg.get("delivery_matrix_place_min"),
        cfg.get("delivery_matrix_place_max"),
    )
    places_list = list(places or matrix.keys())
    mine_bt = mine_places_by_time_from_matrix(matrix, places_list, target_times)
    intent = {
        "target_blocks": target_blocks,
        "target_times": target_times,
        "time_preference_order": time_preference_order,
        "preferred_place_min": int(cfg.get("delivery_preferred_place_min") or 0),
        "preferred_place_max": int(cfg.get("delivery_preferred_place_max") or 0),
        "selectable_place_min": span_lo,
        "selectable_place_max": span_hi,
        "require_consecutive": True,
        "mine_places_by_time": mine_bt,
    }
    solved = solve_candidate_from_matrix(
        matrix,
        places_list,
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
    mine_by_time = cfg.get("mine_places_by_time") if isinstance(cfg.get("mine_places_by_time"), dict) else {}

    lo, hi = _selectable_place_bounds_from_intent(cfg)
    selectable_places = sorted(
        [str(p) for p in (places or matrix.keys()) if str(p).isdigit() and lo <= int(str(p)) <= hi and str(p) in matrix],
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
        # 同场跨多时段略加分；在「全局仍多目标时段」时单时段占多条连续场地略减分（与业务优先级对齐）
        venue_time_bonus = 0.0
        if len(unique_times) >= 2 and len(unique_places) == 1:
            venue_time_bonus = 35.0
        elif len(target_times) >= 2 and len(unique_times) == 1 and len(unique_places) >= 2:
            venue_time_bonus = -22.0
        # 覆盖完整度 > 块数完整度 > 连号质量 > 偏好区命中
        return (
            100.0 * coverage_ratio
            + 60.0 * block_ratio
            + 40.0 * consecutive_ratio
            + 20.0 * preferred_ratio
            + venue_time_bonus
        )

    def _best_for_level(level_index, level_spec):
        """单层内最优候选；无可行解返回 None。"""
        tier_best = None
        if not level_spec:
            return None
        if require_consecutive:
            K = max(level_spec.values())
            if K > len(selectable_places):
                return None
            starts = list(range(len(selectable_places) - K + 1))
            if use_prefer:
                prefer_starts = [
                    i
                    for i in starts
                    if prefer_min <= int(selectable_places[i]) <= prefer_max
                    and prefer_min <= int(selectable_places[i + K - 1]) <= prefer_max
                ]
                starts = prefer_starts + [i for i in starts if i not in prefer_starts]

            def _start_anchor_rank(i):
                s_places = selectable_places[i : i + K]
                total_d = 0
                for t, kk in level_spec.items():
                    if int(kk or 0) <= 0:
                        continue
                    mt_list = mine_by_time.get(str(t).strip()) if mine_by_time else None
                    if not mt_list:
                        continue
                    for p in s_places:
                        total_d += _place_distance_to_mine_set(p, mt_list)
                return (total_d, i)

            if mine_by_time:
                starts = sorted(starts, key=_start_anchor_rank)
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
                if tier_best is None or score > float(tier_best.get("score") or -1):
                    tier_best = cand
        else:
            items = []
            for t in time_preference_order:
                need = int(level_spec.get(t, 0))
                if need <= 0:
                    continue
                avail = [p for p in selectable_places if (matrix.get(p) or {}).get(t) == "available"]
                mt_list = mine_by_time.get(str(t).strip()) if mine_by_time else None
                if use_prefer:
                    avail = sorted(
                        avail,
                        key=lambda p: (
                            _place_distance_to_mine_set(p, mt_list) if mt_list else 0,
                            0 if prefer_min <= int(p) <= prefer_max else 1,
                            int(p),
                        ),
                    )
                elif mt_list:
                    avail = sorted(
                        avail,
                        key=lambda p: (_place_distance_to_mine_set(p, mt_list), int(p)),
                    )
                pick_count = min(max(0, need), len(avail))
                if pick_count <= 0:
                    continue
                for p in avail[:pick_count]:
                    items.append({"place": p, "time": t})
            if not items:
                return None
            items = normalize_booking_items(items)
            score = _score_items(items, level_spec)
            tier_best = {
                "items": items,
                "level_index": level_index,
                "level_spec": dict(level_spec),
                "score": score,
            }
        return tier_best

    if mode == "aggressive":
        # 首组 aggressive：只做「首个有可行解的降级档」内的最优，避免跨档比分为劣档让路（如只抢 21:00 三连号）。
        for level_index, level_spec in enumerate(level_specs):
            tier_best = _best_for_level(level_index, level_spec)
            if tier_best is not None:
                return tier_best
        return None

    best = None
    for level_index, level_spec in enumerate(level_specs):
        tier_best = _best_for_level(level_index, level_spec)
        if tier_best is None:
            continue
        if best is None or float(tier_best.get("score") or -1) > float(best.get("score") or -1):
            best = tier_best
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
    lo, hi = _selectable_place_bounds_from_intent(intent_base)
    selectable = sorted(
        [str(p) for p in places_list if str(p).isdigit() and lo <= int(str(p)) <= hi and str(p) in matrix],
        key=lambda x: int(x),
    )
    mine_bt_scan = intent_base.get("mine_places_by_time") if isinstance(intent_base.get("mine_places_by_time"), dict) else {}
    for t in times_order:
        if need.get(t, 0) <= 0:
            continue
        mt_list = mine_bt_scan.get(str(t).strip()) if mine_bt_scan else None
        sel_scan = sorted(
            selectable,
            key=lambda p: (_place_distance_to_mine_set(p, mt_list) if mt_list else 0, int(p)),
        )
        for p in sel_scan:
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


def _manual_literal_goal_complete(submitted_items, target_items):
    """手工递送：已提交项与目标项在 (place,time) 上是否一致（集合相等）。"""

    def _pair_set(items):
        pairs = set()
        for it in normalize_booking_items(items or []):
            p = str(it.get("place") or "").strip()
            t = str(it.get("time") or "").strip()
            if p and t:
                pairs.add((p, t))
        return pairs

    return _pair_set(submitted_items) == _pair_set(target_items)


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
        max_places_per_timeslot = int(_cg("max_places_per_timeslot", 1) or 1)
    except Exception:
        max_places_per_timeslot = 1
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
            # legacy 分批相关键：仍从磁盘合并，供指标快照与旧版设置页兼容；极速订场递送仅读顶层 delivery_* / submit_timeout 等，不走 profile 内批次策略。
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
            if 'manual_auto_refill_enabled' in saved:
                CONFIG['manual_auto_refill_enabled'] = bool(saved['manual_auto_refill_enabled'])
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
            if 'delivery_refill_no_candidate_streak_limit' in saved:
                try:
                    val, _ = _clamp_exec_param(
                        "delivery_refill_no_candidate_streak_limit",
                        saved["delivery_refill_no_candidate_streak_limit"],
                        CONFIG.get("delivery_refill_no_candidate_streak_limit", 0),
                    )
                    CONFIG["delivery_refill_no_candidate_streak_limit"] = val
                except Exception:
                    pass
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
            if 'accounts' in saved and isinstance(saved['accounts'], list):
                CONFIG['accounts'] = copy.deepcopy(saved['accounts'])
            if 'auth' in saved:
                CONFIG['auth'].update(saved['auth'])
    except Exception as e:
        print(f"加载配置失败: {e}")

# 敏感配置单独文件：若存在 config.secret.json 则覆盖 CONFIG 中对应项（与执行参数分离）
if os.path.exists(CONFIG_SECRET_FILE):
    try:
        with open(CONFIG_SECRET_FILE, 'r', encoding='utf-8') as f:
            secret_saved = json.load(f)
        if isinstance(secret_saved, dict):
            if 'accounts' in secret_saved and isinstance(secret_saved['accounts'], list):
                CONFIG['accounts'] = copy.deepcopy(secret_saved['accounts'])
            if 'auth' in secret_saved and isinstance(secret_saved['auth'], dict):
                CONFIG['auth'].update(secret_saved['auth'])
            if 'notification_phones' in secret_saved:
                CONFIG['notification_phones'] = secret_saved['notification_phones'] if isinstance(secret_saved['notification_phones'], list) else []
            if 'pushplus_tokens' in secret_saved:
                CONFIG['pushplus_tokens'] = secret_saved['pushplus_tokens'] if isinstance(secret_saved['pushplus_tokens'], list) else []
            if 'sms' in secret_saved and isinstance(secret_saved['sms'], dict):
                CONFIG['sms'].update(secret_saved['sms'])
            if 'web_ui_auth' in secret_saved and isinstance(secret_saved['web_ui_auth'], dict):
                CONFIG['web_ui_auth'] = copy.deepcopy(secret_saved['web_ui_auth'])
    except Exception as e:
        print(f"加载敏感配置失败(将使用 config.json 中的值): {e}")

strip_delivery_keys_from_profiles(CONFIG)
for _vk in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
    CONFIG.pop(_vk, None)
sanitize_submit_profiles(CONFIG)
_cfg_startup_errors = validate_required_execution_config(CONFIG)
if _cfg_startup_errors:
    _cfg_err_msg = "执行参数校验失败，请在 config.json 中补全或修正: " + "; ".join(_cfg_startup_errors)
    print("[config] ERROR " + _cfg_err_msg)
    raise RuntimeError(_cfg_err_msg)

QUIET_WINDOW_LOCK = threading.RLock()
QUIET_WINDOW_REQUEST_CONTEXT = threading.local()
QUIET_WINDOW_PREQUIET_SECONDS = 25.0
QUIET_WINDOW_RECOVER_SECONDS = 8.0
QUIET_WINDOW_TTL_SECONDS = 120.0
# account_key 字符串 -> 与旧版相同的单桶快照结构；多账号互不覆盖
QUIET_WINDOW_STATES = {}


def build_quiet_window_scope(auth=None):
    auth_cfg = auth if isinstance(auth, dict) else (CONFIG.get("auth") or {})
    token = str(auth_cfg.get("token") or "").strip()
    shop_num = str(auth_cfg.get("shop_num") or "").strip()
    token_digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:12] if token else "no-token"
    return {
        "account_key": f"{shop_num}:{token_digest}",
        "shop_num": shop_num,
    }


def _normalize_account_item(raw, idx):
    if not isinstance(raw, dict):
        return None
    account_id = str(raw.get("id") or f"acc_{idx + 1}").strip() or f"acc_{idx + 1}"
    raw_limit = raw.get("delivery_max_places_per_timeslot")
    account_limit = None
    if raw_limit is not None and str(raw_limit).strip() != "":
        try:
            account_limit = max(1, min(6, int(raw_limit)))
        except (TypeError, ValueError):
            account_limit = None
    return {
        "id": account_id,
        "name": str(raw.get("name") or account_id).strip() or account_id,
        "token": str(raw.get("token") or "").strip(),
        "cookie": str(raw.get("cookie") or "").strip(),
        "card_index": str(raw.get("card_index") or "").strip(),
        "card_st_id": str(raw.get("card_st_id") or "").strip(),
        "shop_num": str(raw.get("shop_num") or "").strip(),
        "delivery_max_places_per_timeslot": account_limit,
    }


def normalize_accounts(accounts):
    parsed = []
    used = set()
    for idx, raw in enumerate(accounts or []):
        item = _normalize_account_item(raw, idx)
        if not item:
            continue
        base_id = item["id"]
        if base_id in used:
            suffix = 2
            while f"{base_id}_{suffix}" in used:
                suffix += 1
            item["id"] = f"{base_id}_{suffix}"
        used.add(item["id"])
        parsed.append(item)
    return parsed


def ensure_accounts_config():
    accounts = normalize_accounts(CONFIG.get("accounts") or [])
    if not accounts:
        auth = CONFIG.get("auth") if isinstance(CONFIG.get("auth"), dict) else {}
        accounts = normalize_accounts([{
            "id": "acc_1",
            "name": "账号1",
            "token": auth.get("token") or "",
            "cookie": auth.get("cookie") or "",
            "card_index": auth.get("card_index") or "",
            "card_st_id": auth.get("card_st_id") or "",
            "shop_num": auth.get("shop_num") or "",
        }])
    CONFIG["accounts"] = accounts
    primary = accounts[0] if accounts else {}
    CONFIG["auth"] = {
        "token": str(primary.get("token") or "").strip(),
        "cookie": str(primary.get("cookie") or "").strip(),
        "card_index": str(primary.get("card_index") or "").strip(),
        "card_st_id": str(primary.get("card_st_id") or "").strip(),
        "shop_num": str(primary.get("shop_num") or "").strip(),
    }
    return accounts


def resolve_account(account_id):
    account_id = str(account_id or "").strip()
    for acc in ensure_accounts_config():
        if str(acc.get("id") or "") == account_id:
            return copy.deepcopy(acc)
    return None


def build_client_for_account(account):
    # 禁止继承 CONFIG["auth"] 的 Cookie/token：否则多线程并行（如 /api/mine-overview）会带上主账号会话，
    # 馆方若以 Cookie 为准则两个账号会拉到同一人的订单，仅 accountId 标签不同。
    c = ApiClient(inherit_global_auth=False)
    c.token = str(account.get("token") or "").strip()
    c.shop_num = str(account.get("shop_num") or "").strip()
    c.card_index = str(account.get("card_index") or "").strip()
    c.card_st_id = str(account.get("card_st_id") or "").strip()
    c.delivery_max_places_per_timeslot = account.get("delivery_max_places_per_timeslot")
    cookie = str(account.get("cookie") or "").strip()
    if cookie:
        c.headers["Cookie"] = cookie
    return c


def sync_primary_client_auth():
    if "client" not in globals():
        return
    accounts = ensure_accounts_config()
    primary = accounts[0] if accounts else {}
    client.token = str(CONFIG.get("auth", {}).get("token") or "").strip()
    client.shop_num = str(CONFIG.get("auth", {}).get("shop_num") or "").strip()
    client.card_index = str(CONFIG.get("auth", {}).get("card_index") or "").strip()
    client.card_st_id = str(CONFIG.get("auth", {}).get("card_st_id") or "").strip()
    client.delivery_max_places_per_timeslot = primary.get("delivery_max_places_per_timeslot")
    cookie = str(CONFIG.get("auth", {}).get("cookie") or "").strip()
    if cookie:
        client.headers["Cookie"] = cookie
    elif "Cookie" in client.headers:
        del client.headers["Cookie"]


ensure_accounts_config()


def resolve_manual_account_from_request(account_id_raw, require_shop_num=True):
    accounts = ensure_accounts_config()
    if not accounts:
        return None, "未配置任何账号"
    account_id = str(account_id_raw or "").strip()
    account = resolve_account(account_id) if account_id else copy.deepcopy(accounts[0])
    if not account:
        return None, "账号不存在或已删除"
    if not str(account.get("token") or "").strip():
        return None, "该账号未配置 token"
    if require_shop_num and not str(account.get("shop_num") or "").strip():
        return None, "该账号未配置 shop_num"
    return account, ""


def resolve_task_account_and_scope(task):
    """解析定时任务 / Refill 绑定的账号及静默 scope；task 为 dict，读可选 accountId。"""
    if not isinstance(task, dict):
        return None, None, "任务数据无效"
    account, err = resolve_manual_account_from_request(task.get("accountId"), require_shop_num=True)
    if err:
        return None, None, err
    return account, build_quiet_window_scope(auth=account), ""


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


def _scope_storage_key(scope):
    if not isinstance(scope, dict):
        scope = build_quiet_window_scope()
    key = str(scope.get("account_key") or "").strip()
    return key if key else "__no_account__"


def _quiet_window_bucket_copy(scope):
    key = _scope_storage_key(scope)
    with QUIET_WINDOW_LOCK:
        s = QUIET_WINDOW_STATES.get(key)
        return copy.deepcopy(s) if s else None


def quiet_window_snapshot(scope=None):
    """scope 为 dict 时返回该账号静默桶副本；为 None 时返回所有桶的深拷贝 dict。"""
    with QUIET_WINDOW_LOCK:
        if scope is None:
            return {k: copy.deepcopy(v) for k, v in QUIET_WINDOW_STATES.items()}
        key = _scope_storage_key(scope)
        s = QUIET_WINDOW_STATES.get(key)
        return copy.deepcopy(s) if s else None


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
    resolved_scope = scope if isinstance(scope, dict) else build_quiet_window_scope()
    snapshot = _quiet_window_bucket_copy(resolved_scope)
    if not isinstance(snapshot, dict):
        snapshot = {}
    now_ts = time.time()
    active = bool(snapshot.get("active")) and (not _quiet_window_is_expired(snapshot, now_ts=now_ts))
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
    ak = str(resolved_scope.get("account_key") or "")
    return {
        "active": True,
        "state": state,
        "owner_task_id": snapshot.get("owner_task_id"),
        "remaining_ms": remaining_ms,
        "message": message_map.get(state, "静默窗口中"),
        "reason": str(snapshot.get("reason") or ""),
        "account_key": ak,
    }


def enter_quiet_window(owner_task_id, fire_at_ts, reason="", scope=None):
    now_ts = time.time()
    resolved_scope = scope if isinstance(scope, dict) else build_quiet_window_scope()
    owner_id = str(owner_task_id) if owner_task_id is not None else None
    key = _scope_storage_key(resolved_scope)
    with QUIET_WINDOW_LOCK:
        existing = QUIET_WINDOW_STATES.get(key)
        if existing and existing.get("active") and (not _quiet_window_is_expired(existing, now_ts=now_ts)):
            return copy.deepcopy(existing)
        next_state = {
            "active": True,
            "state": "pre_quiet",
            "owner_task_id": owner_id,
            "account_key": resolved_scope.get("account_key"),
            "shop_num": resolved_scope.get("shop_num"),
            "reason": str(reason or ""),
            "entered_at_ts": now_ts,
            "fire_at_ts": float(fire_at_ts or now_ts),
            "fire_started_at_ts": 0.0,
            "recover_until_ts": 0.0,
            "ttl_deadline_ts": max(float(fire_at_ts or now_ts), now_ts) + QUIET_WINDOW_TTL_SECONDS,
            "released_reason": "",
        }
        QUIET_WINDOW_STATES[key] = next_state
        out = copy.deepcopy(next_state)
    log(f"🔇 [quiet-window] 进入 pre_quiet，owner={owner_id}，account_key={key}，fire_at={datetime.fromtimestamp(next_state['fire_at_ts']).strftime('%H:%M:%S')}")
    return out


def mark_quiet_window_fire(owner_task_id, scope=None):
    owner_id = str(owner_task_id) if owner_task_id is not None else None
    with QUIET_WINDOW_LOCK:
        if isinstance(scope, dict):
            keys = [_scope_storage_key(scope)]
        else:
            keys = [
                k for k, v in QUIET_WINDOW_STATES.items()
                if v.get("active") and str(v.get("owner_task_id") or "") == str(owner_id or "")
            ]
        out = None
        for key in keys:
            snapshot = QUIET_WINDOW_STATES.get(key)
            if not snapshot or not snapshot.get("active"):
                continue
            if str(snapshot.get("owner_task_id") or "") != str(owner_id or ""):
                continue
            if str(snapshot.get("state") or "") == "fire_window":
                out = copy.deepcopy(snapshot)
                continue
            snapshot = copy.deepcopy(snapshot)
            snapshot["state"] = "fire_window"
            snapshot["fire_started_at_ts"] = time.time()
            snapshot["ttl_deadline_ts"] = max(float(snapshot.get("ttl_deadline_ts") or 0.0), snapshot["fire_started_at_ts"] + QUIET_WINDOW_TTL_SECONDS)
            QUIET_WINDOW_STATES[key] = snapshot
            out = copy.deepcopy(snapshot)
            log(f"🔫 [quiet-window] 进入 fire_window，owner={owner_id}，account_key={key}")
        return out if out is not None else {}


def mark_quiet_window_recovering(owner_task_id, reason="", scope=None):
    owner_id = str(owner_task_id) if owner_task_id is not None else None
    with QUIET_WINDOW_LOCK:
        if isinstance(scope, dict):
            keys = [_scope_storage_key(scope)]
        else:
            keys = [
                k for k, v in QUIET_WINDOW_STATES.items()
                if v.get("active") and str(v.get("owner_task_id") or "") == str(owner_id or "")
            ]
        out = None
        for key in keys:
            snapshot = QUIET_WINDOW_STATES.get(key)
            if not snapshot or not snapshot.get("active"):
                continue
            if str(snapshot.get("owner_task_id") or "") != str(owner_id or ""):
                continue
            snapshot = copy.deepcopy(snapshot)
            snapshot["state"] = "recovering"
            snapshot["recover_until_ts"] = time.time() + QUIET_WINDOW_RECOVER_SECONDS
            snapshot["ttl_deadline_ts"] = max(float(snapshot.get("ttl_deadline_ts") or 0.0), snapshot["recover_until_ts"] + 5.0)
            snapshot["released_reason"] = str(reason or "")
            QUIET_WINDOW_STATES[key] = snapshot
            out = copy.deepcopy(snapshot)
            log(f"🔄 [quiet-window] 进入 recovering，owner={owner_id}，account_key={key}，reason={reason or 'task-finished'}")
        return out if out is not None else {}


def release_quiet_window(reason="", scope=None):
    resolved_scope = scope if isinstance(scope, dict) else build_quiet_window_scope()
    key = _scope_storage_key(resolved_scope)
    with QUIET_WINDOW_LOCK:
        prev = QUIET_WINDOW_STATES.pop(key, None)
        had_active = bool(prev and prev.get("active"))
    rel_reason = str(reason or (prev or {}).get("released_reason") or "released")
    if had_active:
        log(f"🔓 [quiet-window] 已释放 account_key={key}，reason={rel_reason}")
    return {"active": False, "released_reason": rel_reason, "account_key": key}


def is_quiet_window_active(scope=None):
    return bool(get_quiet_window_status(scope=scope).get("active"))


def quiet_window_block_info(requester_kind, requester_task_id=None, owner_allowed=False, scope=None):
    resolved_scope = scope if isinstance(scope, dict) else build_quiet_window_scope()
    status = get_quiet_window_status(scope=resolved_scope)
    if not status.get("active"):
        return None
    snapshot = _quiet_window_bucket_copy(resolved_scope) or {}
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
        "api_gym_probe": "馆方 API 探测",
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
    def __init__(self, inherit_global_auth=True):
        self.host = "gymvip.bfsu.edu.cn"
        self.headers = {
            "Host": self.host,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254162e) XWEB/18151 Flue",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"https://{self.host}",
            "Referer": f"https://{self.host}/easyserp/index.html",
        }
        if inherit_global_auth:
            auth = CONFIG.get("auth") or {}
            cookie = str(auth.get("cookie", "")).strip()
            if cookie:
                self.headers["Cookie"] = cookie
            self.token = auth.get("token") or ""
            self.shop_num = str(auth.get("shop_num") or "").strip()
            self.card_index = str(auth.get("card_index") or "").strip()
            self.card_st_id = str(auth.get("card_st_id") or "").strip()
        else:
            self.token = ""
            self.shop_num = ""
            self.card_index = ""
            self.card_st_id = ""
        self.session = requests.Session()
        self.server_time_offset_seconds = 0.0
        self._matrix_cache = {}
        self._matrix_cache_window_s = 0.12
        self._matrix_cache_lock = threading.Lock()

    def _quiet_scope_from_client(self):
        return build_quiet_window_scope(auth={"token": self.token, "shop_num": self.shop_num})

    def raw_request(self, method, path_norm, params=None, data=None, json_body=None, timeout=15):
        """馆方 HTTP 原始请求；path_norm 须已规范为 easyserpClient/..."""
        method = (method or "GET").upper()
        path_norm = str(path_norm or "").strip().lstrip("/")
        url = f"https://{self.host}/{path_norm}"
        timeout = float(timeout or 15)
        timeout = max(0.5, min(45.0, timeout))
        t0 = time.time()
        if method == "GET":
            resp = self.session.get(
                url,
                headers=self.headers,
                params=params or {},
                timeout=timeout,
                verify=False,
            )
        elif method == "POST":
            hdrs = dict(self.headers)
            if json_body is not None:
                hdrs["Content-Type"] = "application/json;charset=UTF-8"
                resp = self.session.post(
                    url,
                    headers=hdrs,
                    params=params if params else None,
                    json=json_body,
                    timeout=timeout,
                    verify=False,
                )
            else:
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
                resp = self.session.post(
                    url,
                    headers=hdrs,
                    params=params if params else None,
                    data=data,
                    timeout=timeout,
                    verify=False,
                )
        else:
            return None, "仅支持 GET/POST"
        t1 = time.time()
        self._update_server_time_offset(resp, t0, t1)
        return resp, None

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
            globals()["_LOG_TIME_OFFSET_SECONDS"] = float(self.server_time_offset_seconds or 0.0)
        except Exception:
            return

    def get_aligned_now(self):
        return datetime.now() + timedelta(seconds=float(self.server_time_offset_seconds or 0.0))

    def _fieldinfo_place_labels(self, p_num):
        try:
            p_int = int(p_num)
        except (TypeError, ValueError):
            p_int = None
        if p_int is not None and p_int >= 15:
            return f"mdb{p_num}", f"木地板{p_num}"
        return f"ymq{p_num}", f"羽毛球{p_num}"

    def _build_field_info_list(self, date_str, selected_items):
        """与官网一致：同场地、同日、连续整点小时合并为一条 fieldinfo，金额按小时规则累加。"""

        def _legacy_rows_for_place(p_key, items_for_p):
            rows = []
            subtotal = 0
            place_short, place_name = self._fieldinfo_place_labels(p_key)
            for item in items_for_p:
                start = str(item["time"])
                try:
                    st_obj = datetime.strptime(start, "%H:%M")
                    et_obj = st_obj + timedelta(hours=1)
                    end = et_obj.strftime("%H:%M")
                    price = 80 if st_obj.hour < 14 else 100
                except Exception:
                    end = "22:00"
                    price = 100
                rows.append(
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
                subtotal += price
            return rows, subtotal

        normalized = normalize_booking_items(selected_items)
        place_order = []
        place_to_times = {}
        for item in normalized:
            p_key = str(item["place"])
            t_key = str(item["time"])
            if p_key not in place_to_times:
                place_to_times[p_key] = set()
                place_order.append(p_key)
            place_to_times[p_key].add(t_key)

        field_info_list = []
        total_money = 0

        for p_key in place_order:
            times_set = place_to_times[p_key]
            try:
                times_sorted = sorted(times_set, key=lambda t: datetime.strptime(t, "%H:%M"))
            except ValueError:
                times_sorted = None

            if times_sorted is None:
                items_p = [it for it in normalized if str(it["place"]) == p_key]
                rows, sub = _legacy_rows_for_place(p_key, items_p)
                field_info_list.extend(rows)
                total_money += sub
                continue

            place_short, place_name = self._fieldinfo_place_labels(p_key)
            run_start = None
            run_end_excl = None
            run_money = 0

            def _append_run():
                nonlocal total_money
                if run_start is None or run_end_excl is None:
                    return
                field_info_list.append(
                    {
                        "day": date_str,
                        "oldMoney": run_money,
                        "startTime": run_start.strftime("%H:%M"),
                        "endTime": run_end_excl.strftime("%H:%M"),
                        "placeShortName": place_short,
                        "name": place_name,
                        "stageTypeShortName": "ymq",
                        "newMoney": run_money,
                    }
                )
                total_money += run_money

            for t_str in times_sorted:
                st_obj = datetime.strptime(t_str, "%H:%M")
                et_obj = st_obj + timedelta(hours=1)
                price = 80 if st_obj.hour < 14 else 100
                if run_start is None:
                    run_start = st_obj
                    run_end_excl = et_obj
                    run_money = price
                elif st_obj == run_end_excl:
                    run_end_excl = et_obj
                    run_money += price
                else:
                    _append_run()
                    run_start = st_obj
                    run_end_excl = et_obj
                    run_money = price
            _append_run()

        return field_info_list, total_money

    def _build_reservation_body(self, date_str, selected_items):
        field_info_list, total_money = self._build_field_info_list(date_str, selected_items)
        info_str = urllib.parse.quote(
            json.dumps(field_info_list, separators=(",", ":"), ensure_ascii=False)
        )
        type_encoded = urllib.parse.quote("羽毛球")
        body = (
            f"token={self.token}&"
            f"shopNum={self.shop_num}&"
            f"fieldinfo={info_str}&"
            f"cardStId={self.card_st_id}&"
            f"oldTotal={total_money}.00&"
            f"cardPayType=0&"
            f"type={type_encoded}&"
            f"offerId=&"
            f"offerType=&"
            f"total={total_money}.00&"
            f"premerother=&"
            f"cardIndex={self.card_index}"
        )
        return body, field_info_list, total_money

    def _build_auth_snapshot(self):
        auth_cfg = CONFIG.get("auth") or {}
        header_cookie = str((self.headers or {}).get("Cookie") or "").strip()
        return {
            "token": str(self.token or auth_cfg.get("token") or "").strip(),
            "cookie": header_cookie or str(auth_cfg.get("cookie") or "").strip(),
            "shop_num": str(self.shop_num or auth_cfg.get("shop_num") or "").strip(),
            "card_index": str(self.card_index or auth_cfg.get("card_index") or "").strip(),
            "card_st_id": str(self.card_st_id or auth_cfg.get("card_st_id") or "").strip(),
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
                is_timeout = "timeout" in lower or "timed out" in lower
                bucket = "timeout" if is_timeout else "connection_error"
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
        if "可预约场地的时间跨度" in msg_raw or "同一时间的场地最多预约" in msg_raw:
            return {
                "action": "stop_task_fail",
                "normalized_msg": msg_raw[:200] or "馆方预约规则限制",
                "terminal_reason": "booking_rule_terminal",
                "bucket": "payload_fail",
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
            "unknown_business_fail_count": 0,
            "payload_fail_count": 0,
            "matrix_fetch_fail_count": 0,
            "matrix_timeout_count": 0,
            "matrix_connection_error_count": 0,
            "matrix_resp_404_count": 0,
            "matrix_resp_5xx_count": 0,
            "transport_error_events": [],
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
            "refill_no_candidate_max_streak": 0,
            "refill_no_candidate_streak_final": 0,
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

        primary_items = normalize_booking_items((groups[0] or {}).get("items") or [])
        venue_merged = _merge_task_venue_strategy(task_config, primary_items)
        account_max_places = None
        try:
            _raw_acc_limit = getattr(self, "delivery_max_places_per_timeslot", None)
            if _raw_acc_limit is not None and str(_raw_acc_limit).strip() != "":
                account_max_places = max(1, min(6, int(_raw_acc_limit)))
        except Exception:
            account_max_places = None
        if account_max_places is None:
            run_metric["delivery_status"] = "blocked"
            run_metric["business_status"] = "fail"
            run_metric["terminal_reason"] = "account_max_places_missing"
            run_metric["stopped_by"] = "account_config_invalid"
            return {
                "status": "fail",
                "msg": "账号未配置同一时段最多场地数，请到基础配置-账号配置填写 delivery_max_places_per_timeslot（1-6）",
                "success_items": [],
                "failed_items": primary_items,
                "run_metric": run_metric,
            }

        def cfg_campaign(key, default=None):
            """预算/间隔等读 CONFIG；场地时段策略读任务 config 与主组 items 合并结果（venue_merged）。"""
            if key in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
                return venue_merged.get(key, default)
            if key == "delivery_submit_granularity":
                v = venue_merged.get(key)
                if v is not None:
                    return v
                return CONFIG.get(key, default)
            if key == "max_places_per_timeslot":
                return int(account_max_places)
            return CONFIG.get(key, default)

        timeout_s = max(0.5, float(cfg_campaign("submit_timeout_seconds", 4.0) or 4.0))
        transport_round_interval_s = max(0.05, float(cfg_campaign("delivery_transport_round_interval_seconds", 0.25) or 0.25))
        refill_poll_interval_s = max(0.05, float(cfg_campaign("delivery_refill_matrix_poll_seconds", 0.35) or 0.35))
        try:
            _raw_sl = int(cfg_campaign("delivery_refill_no_candidate_streak_limit", 0) or 0)
        except (TypeError, ValueError):
            _raw_sl = 0
        refill_no_candidate_streak_limit, _ = _clamp_exec_param(
            "delivery_refill_no_candidate_streak_limit", _raw_sl, _raw_sl
        )
        delivery_min_post_interval_s = max(
            0.0, float(cfg_campaign("delivery_min_post_interval_seconds", CONFIG.get("delivery_min_post_interval_seconds", 2.2)) or 0)
        )
        run_metric["effective_delivery_min_post_interval_seconds"] = float(delivery_min_post_interval_s)
        run_metric["effective_max_places_per_timeslot"] = int(account_max_places)
        _dtb = float(cfg_campaign("delivery_total_budget_seconds", 600.0) or 600.0)
        delivery_total_budget_s, _ = _clamp_exec_param("delivery_total_budget_seconds", _dtb, _dtb)
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
            elif bucket_name == "non_json":
                run_metric["non_json_count"] += 1
            elif bucket_name == "unknown_business_fail":
                run_metric["unknown_business_fail_count"] += 1
            elif bucket_name == "payload_fail":
                run_metric["payload_fail_count"] += 1

        log(
            f"[极速订场] 开始 date={date_str} 单路递送+refill round_interval={transport_round_interval_s}s "
            f"refill_poll={refill_poll_interval_s}s min_post_interval={delivery_min_post_interval_s}s "
            f"budget={delivery_total_budget_s}s"
        )
        try:
            # 第一组发送前拉活：直接 get_matrix 并根据返回值判断，可重试错误有限次重试，鉴权错误不重试
            _wmr = int(cfg_campaign("delivery_warmup_max_retries", 200) or 200)
            warmup_max_attempts, _ = _clamp_exec_param("delivery_warmup_max_retries", _wmr, _wmr)
            _wbs = float(cfg_campaign("delivery_warmup_budget_seconds", 300.0) or 300.0)
            warmup_budget_s, _ = _clamp_exec_param("delivery_warmup_budget_seconds", _wbs, _wbs)
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
                mx_t0 = time.perf_counter()
                warmup = self.get_matrix(
                    date_str, include_mine_overlay=False, request_timeout=matrix_timeout_s, bypass_cache=True
                )
                if isinstance(warmup, dict) and not warmup.get("error"):
                    scope = (warmup.get("meta") or {}).get("date_booking_scope") or "unlocked"
                    mos = matrix_booking_open_by_no_locked_cells(warmup.get("matrix"))
                    if mos is True:
                        booking_open = True
                    elif mos is False:
                        booking_open = False
                    else:
                        booking_open = scope != "future"
                    if booking_open:
                        log("[极速订场] 拉活成功，开始递送")
                        warmup_ok = True
                        warmup_result = warmup
                        break
                    # 未开约：先睡到 lastDayOpenTime 再发一次主组 POST，然后继续拉活
                    last_open_meta = (warmup.get("meta") or {}).get("last_day_open_time") or "12:00:00"
                    open_t = _parse_last_day_open_time(last_open_meta)
                    sleep_sec = seconds_until_today_open_time_cn(open_t)
                    if sleep_sec > 0:
                        log(f"[极速订场] 未开约 sleep {sleep_sec:.1f}s 至 lastDayOpenTime={last_open_meta}")
                        warmup_deadline = max(warmup_deadline, time.time() + sleep_sec + 90.0)
                        time.sleep(sleep_sec)
                    if not probe_main_group_sent:
                        main_items = normalize_booking_items(groups[0].get("items") or [])
                        if main_items:
                            body, _, _ = self._build_reservation_body(date_str, main_items)
                            result = self._post_reservation_once(sessions[0], headers_snapshot, url, body, timeout_s)
                            if delivery_min_post_interval_s > 0:
                                post_spacing["last_end_mono"] = time.perf_counter()
                            probe_main_group_sent = True
                            log("[极速订场] 未开约 开约时刻主组 1 次")
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
                            if classified.get("action") != "stop_success":
                                append_transport_error_event(
                                    run_metric,
                                    "warmup_probe_post",
                                    bucket,
                                    (msg_snippet or str(raw_msg or ""))[:120],
                                    int(result.get("elapsed_ms") or 0),
                                )
                            if classified.get("action") == "stop_success":
                                run_metric["submit_success_resp_count"] = (run_metric.get("submit_success_resp_count") or 0) + 1
                                run_metric["delivery_window_ms"] = int(max(0.0, time.time() - campaign_started_at) * 1000)
                                run_metric["stopped_by"] = "business_success"
                                run_metric["delivery_status"] = "accepted"
                                run_metric["business_status"] = "success"
                                run_metric["terminal_reason"] = str(classified.get("terminal_reason") or "business_success")
                                group_id = str(groups[0].get("id") or "primary")
                                group_label = str(groups[0].get("label") or "主组合")
                                log(f"[极速订场] 未开约主组试探成功 结束 status=success group={group_id}")
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
                        log("[极速订场] 未开约 已发过开约时刻主组 继续拉活")
                    time.sleep(0.8)
                    continue
                err_msg = (
                    str(warmup.get("error", "") or "").strip()
                    if isinstance(warmup, dict)
                    else "matrix_invalid_response"
                )
                mx_elapsed_ms = int((time.perf_counter() - mx_t0) * 1000)
                record_matrix_fetch_failure(run_metric, "warmup_matrix", err_msg or "unknown", mx_elapsed_ms)
                err_l = err_msg.lower()
                if any(k in err_msg for k in auth_keywords) or any(k in err_l for k in ("token", "登录", "未登录")):
                    log(f"[极速订场] 拉活失败(鉴权/会话硬失败)，不重试: {err_msg[:200]}")
                    return {
                        "status": "fail",
                        "msg": "拉活失败：鉴权/会话失效（硬失败）" + (f" ({err_msg[:100]})" if err_msg else ""),
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

            if cfg_campaign("delivery_first_group_from_matrix") and warmup_result and warmup_result.get("matrix"):
                first_group_times_raw = cfg_campaign("delivery_first_group_times")
                first_group_times_ok = isinstance(first_group_times_raw, list) and len(first_group_times_raw) > 0
                if first_group_times_ok:
                    matrix_blocks = max(1, min(3, int(cfg_campaign("delivery_target_blocks", 2) or 2)))
                    run_metric["matrix_first_group_blocks_source"] = "config"
                    first_group_cfg = {
                        "delivery_target_blocks": matrix_blocks,
                        "delivery_target_times": list(cfg_campaign("delivery_target_times") or []),
                        "delivery_time_preference_order": list(cfg_campaign("delivery_time_preference_order") or []),
                        "delivery_first_group_times": cfg_campaign("delivery_first_group_times"),
                        "delivery_first_group_time_preference_order": cfg_campaign("delivery_first_group_time_preference_order"),
                        "delivery_preferred_place_min": cfg_campaign("delivery_preferred_place_min"),
                        "delivery_preferred_place_max": cfg_campaign("delivery_preferred_place_max"),
                        "delivery_matrix_place_min": cfg_campaign("delivery_matrix_place_min", 1),
                        "delivery_matrix_place_max": cfg_campaign("delivery_matrix_place_max", 14),
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
                    elif pre_matrix_primary_items:
                        groups[0] = {**groups[0], "items": list(pre_matrix_primary_items)}
                        run_metric["first_group_combo_level"] = "fallback_items"
                        log("[极速订场] 第一组矩阵无解，回退主组原 items 继续递送")
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
            intent_target_times = cfg_campaign("delivery_first_group_times") or target_times_from_items
            intent_target_times = [str(t).strip() for t in intent_target_times if re.fullmatch(r"\d{2}:\d{2}", str(t).strip())]
            if not intent_target_times:
                intent_target_times = list(target_times_from_items)
            intent_time_order = cfg_campaign("delivery_first_group_time_preference_order") or intent_target_times
            intent_time_order = [str(t).strip() for t in intent_time_order if str(t).strip() in set(intent_target_times)] or list(intent_target_times)
            intent_target_blocks = max(1, min(3, int(cfg_campaign("delivery_target_blocks", 2) or 2)))
            run_metric["goal_target_blocks_source"] = "config"
            span_lo, span_hi = _normalized_matrix_place_span(
                cfg_campaign("delivery_matrix_place_min", 1),
                cfg_campaign("delivery_matrix_place_max", 14),
            )
            campaign_intent = {
                "target_blocks": intent_target_blocks,
                "target_times": intent_target_times,
                "time_preference_order": intent_time_order,
                "preferred_place_min": int(cfg_campaign("delivery_preferred_place_min", 0) or 0),
                "preferred_place_max": int(cfg_campaign("delivery_preferred_place_max", 0) or 0),
                "selectable_place_min": span_lo,
                "selectable_place_max": span_hi,
                "require_consecutive": True,
            }
            run_metric["goal_target_blocks"] = int(campaign_intent.get("target_blocks") or 1)
            run_metric["goal_target_times"] = list(campaign_intent.get("target_times") or [])

            refill_limits = {}
            for _lim_key in ("max_items_per_batch", "max_consecutive_slots_per_place", "max_places_per_timeslot"):
                _cfg_key = f"delivery_refill_{_lim_key}"
                try:
                    _v = cfg_campaign(_cfg_key, None)
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
                if str(phase_tag).startswith("refill"):
                    campaign_ms = int(max(0.0, time.time() - campaign_started_at) * 1000)
                    log(
                        f"[极速订场] refill POST发送 wall={datetime.now().strftime('%H:%M:%S.%f')[:-3]} "
                        f"campaign_ms={campaign_ms} tag={phase_tag} items={batch_items}"
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
                if action != "stop_success":
                    post_snip = str(
                        result.get("raw_message")
                        or result.get("exception_text")
                        or classified.get("normalized_msg")
                        or ""
                    )[:120]
                    append_transport_error_event(
                        run_metric,
                        f"post_{phase_tag}",
                        bucket or action,
                        post_snip,
                        int(result.get("elapsed_ms") or 0),
                    )
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

            def _is_hard_task_fail(classified):
                """鉴权失败与明确馆方规则终局类 stop_task_fail 立即结束；其余 stop_task_fail 进入 refill。"""
                if not isinstance(classified, dict):
                    return False
                tr = str(classified.get("terminal_reason") or "")
                return tr == "auth_fail" or tr == "booking_rule_terminal"

            batch_limits_arg = refill_limits if refill_limits else None

            def _apply_submit_granularity_batches(batches_in):
                g = str(cfg_campaign("delivery_submit_granularity", "per_legal_batch") or "per_legal_batch").strip().lower()
                if g == "single_cell":
                    out = []
                    for b in batches_in:
                        for it in b:
                            out.append([it])
                    return out
                return list(batches_in)

            def _sequential_post_all_batches(batches_list, phase_base):
                """同轮顺序 POST；软失败不终局，硬失败返回 (classified, fail_batch)。"""
                merged_ok = []
                for idx, batch in enumerate(batches_list):
                    tag = phase_base if idx == 0 else f"{phase_base}{idx + 1}"
                    if idx == 0:
                        log(f"[极速订场] {tag} group={group_id} ({group_label}) items={batch}")
                    else:
                        log(f"[极速订场] {tag} group={group_id} ({group_label}) items={batch}")
                    classified_loop, _ = _campaign_post_batch(batch, tag)
                    if not classified_loop:
                        break
                    act_loop = classified_loop.get("action")
                    if act_loop == "stop_task_fail" and _is_hard_task_fail(classified_loop):
                        return classified_loop, batch
                    if act_loop == "stop_success":
                        merged_ok.extend(batch)
                    elif act_loop in ("continue_delivery", "min_backoff_continue"):
                        time.sleep(min(transport_round_interval_s, 0.5))
                    elif act_loop == "switch_backup":
                        run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
                return None, None

            legal_batches_raw = group_booking_items_into_legal_batches(
                target_items, cfg_campaign, profile_name=profile_name, batch_limits=batch_limits_arg
            )
            if not legal_batches_raw:
                return {
                    "status": "fail",
                    "msg": "主组合无法拆分为合法批次",
                    "success_items": [],
                    "failed_items": target_items,
                    "run_metric": run_metric,
                    "delivery_group_id": group_id,
                }
            legal_batches = _apply_submit_granularity_batches(legal_batches_raw)
            hard_cls, hard_batch = _sequential_post_all_batches(legal_batches, "首单")
            if hard_cls:
                return _return_task_fail(hard_cls, hard_batch or [])

            # refill：每轮先拉矩阵；多批顺序 POST；满额以矩阵 mine 计数为准
            refill_no_candidate_streak = 0
            while time.time() < deadline_ts:
                run_metric["refill_matrix_fetch_count"] = (run_metric.get("refill_matrix_fetch_count") or 0) + 1
                mx_t0 = time.perf_counter()
                mx = self.get_matrix(
                    date_str, include_mine_overlay=False, request_timeout=matrix_timeout_s, bypass_cache=True
                )
                if not isinstance(mx, dict) or mx.get("error"):
                    err = str((mx or {}).get("error", "matrix_fail") if isinstance(mx, dict) else "matrix_fail")[:120]
                    mx_fail_ms = int((time.perf_counter() - mx_t0) * 1000)
                    record_matrix_fetch_failure(run_metric, "refill_matrix", err, mx_fail_ms)
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
                intent_base["mine_places_by_time"] = mine_places_by_time_from_matrix(
                    matrix_live, places_list, target_times_live
                )
                solved, used_need_by_time, tier_label = solve_refill_need_tiered(
                    matrix_live,
                    places_list,
                    intent_base,
                    need_by_time,
                    allow_scatter=True,
                )
                if not solved or not solved.get("items"):
                    run_metric["refill_no_candidate_count"] = (run_metric.get("refill_no_candidate_count") or 0) + 1
                    refill_no_candidate_streak += 1
                    run_metric["refill_no_candidate_max_streak"] = max(
                        int(run_metric.get("refill_no_candidate_max_streak") or 0),
                        refill_no_candidate_streak,
                    )
                    if (
                        int(refill_no_candidate_streak_limit or 0) > 0
                        and refill_no_candidate_streak >= int(refill_no_candidate_streak_limit)
                    ):
                        run_metric["delivery_window_ms"] = int(
                            max(0.0, time.time() - campaign_started_at) * 1000
                        )
                        run_metric["stopped_by"] = "refill_no_candidate_streak"
                        run_metric["delivery_status"] = "exhausted"
                        run_metric["business_status"] = "unknown"
                        run_metric["terminal_reason"] = "refill_no_candidate_streak"
                        run_metric["refill_no_candidate_streak_final"] = int(refill_no_candidate_streak)
                        log(
                            f"[极速订场] refill 连续 {refill_no_candidate_streak} 次无满足约束候选，"
                            f"已达阈值 {refill_no_candidate_streak_limit}，早停 缺口={need_by_time}"
                        )
                        return {
                            "status": "fail",
                            "msg": (
                                f"refill 连续 {refill_no_candidate_streak} 次无满足约束候选，已早停"
                                f"（阈值={refill_no_candidate_streak_limit}）"
                            ),
                            "success_items": [],
                            "failed_items": target_items,
                            "run_metric": run_metric,
                            "delivery_group_id": group_id,
                        }
                    log(f"[极速订场] refill 本轮无满足约束候选，缺口={need_by_time}，短等待后重拉")
                    time.sleep(refill_poll_interval_s)
                    continue
                refill_no_candidate_streak = 0
                log(
                    f"[极速订场] refill 分层求解 tier={tier_label} used_need={used_need_by_time} "
                    f"原缺口={need_by_time}"
                )
                run_metric["refill_candidate_found_count"] = (run_metric.get("refill_candidate_found_count") or 0) + 1
                avail_items = normalize_booking_items(solved.get("items") or [])
                rbatches_raw = group_booking_items_into_legal_batches(
                    avail_items, cfg_campaign, profile_name=profile_name, batch_limits=batch_limits_arg
                )
                rbatches = _apply_submit_granularity_batches(rbatches_raw)
                if not rbatches:
                    time.sleep(refill_poll_interval_s)
                    continue
                hard_r, hard_rb = _sequential_post_all_batches(rbatches, "refill")
                if hard_r:
                    return _return_task_fail(hard_r, hard_rb or [])
                log("[极速订场] refill 本轮多批已处理，下轮重拉矩阵校验满额")
                run_metric["submit_retry_count"] = (run_metric.get("submit_retry_count") or 0) + 1
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
            scope=self._quiet_scope_from_client(),
        )
        if quiet_info:
            return {"ok": False, "unknown": True, "msg": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")}
        url = f"https://{self.host}/easyserpClient/place/reservationPlace"
        probe_body = (
            f"token={self.token}&"
            f"shopNum={self.shop_num}&"
            f"fieldinfo=%5B%5D&"
            f"cardStId={self.card_st_id}&"
            f"oldTotal=0.00&"
            f"cardPayType=0&"
            f"type=&"
            f"offerId=&"
            f"offerType=&"
            f"total=0.00&"
            f"premerother=&"
            f"cardIndex={self.card_index}"
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
            scope=self._quiet_scope_from_client(),
        )
        if quiet_info:
            return {"error": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")}
        url = f"https://{self.host}/easyserpClient/place/getPlaceOrder"
        all_orders = []

        for page_no in range(max_pages):
            params = {
                "pageNo": page_no,
                "pageSize": page_size,
                "shopNum": self.shop_num,
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
            scope=self._quiet_scope_from_client(),
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
            f"shopNum={urllib.parse.quote(str(self.shop_num or ''))}&"
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
            bill_num = str(
                order.get("billNum")
                or order.get("outtradeno")
                or order.get("outTradeNo")
                or order.get("orderNum")
                or ""
            ).strip()
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
            scope=self._quiet_scope_from_client(),
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
            "shopNum": self.shop_num,
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
            raw_data_dict = None
            if isinstance(raw_data, str):
                try:
                    parsed = json.loads(raw_data)
                except Exception:
                    return {"error": "JSON解析失败"}
            else:
                parsed = raw_data

            if isinstance(parsed, dict) and "placeArray" in parsed:
                raw_data_dict = parsed
                place_array = parsed["placeArray"]
            elif isinstance(parsed, list):
                place_array = parsed
            else:
                return {"error": "无法找到场地列表"}

            if not isinstance(place_array, list):
                return {"error": "无法找到场地列表"}

            STATE_SAMPLER.ingest(place_array)

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

            short_name_param = str(params.get("shortName") or "ymq")
            appoint_cfg = _pick_tb_appoint_config(raw_data_dict, short_name_param) if raw_data_dict else None
            date_booking_scope = booking_date_scope_from_appoint(date_str, appoint_cfg)
            last_open_t = _parse_last_day_open_time(
                (appoint_cfg.get("lastDayOpenTime") or appoint_cfg.get("lastdayopentime"))
                if isinstance(appoint_cfg, dict)
                else None
            )
            last_day_open_time_str = last_open_t.strftime("%H:%M:%S")

            for place in place_array:
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

                    status_map[t] = map_slot_state_int(state_int, locked_state_values)

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
                    "date_booking_scope": date_booking_scope,
                    "last_day_open_time": last_day_open_time_str,
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
        入口统一走极简直提：内部仅调用 submit_order_minimal -> submit_delivery_campaign。
        传统分批下单实现已移除。
        """
        ctx = get_runtime_request_context()
        quiet_info = quiet_window_block_info(
            "submit_order",
            requester_task_id=ctx.get("task_id"),
            owner_allowed=bool(ctx.get("owner")),
            scope=self._quiet_scope_from_client(),
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
        profile_name = str(submit_profile or "").strip()
        return self.submit_order_minimal(date_str, selected_items, submit_profile=profile_name)

client = ApiClient()

# ================= 任务调度系统 =================

class TaskManager:
    def __init__(self):
        self.tasks = []
        self.refill_tasks = []
        self._refill_lock = threading.Lock()
        self._refill_last_run = {}
        self._refill_last_post_end_mono = {}
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

    def _restore_runtime_services_after_quiet_release(self, reason, scope=None):
        resolved_scope = scope if isinstance(scope, dict) else build_quiet_window_scope()
        snapshot = quiet_window_snapshot(resolved_scope)
        if not isinstance(snapshot, dict) or not snapshot.get("active"):
            return snapshot
        released = release_quiet_window(reason=reason, scope=resolved_scope)
        self.reset_refill_scheduler_baseline()
        self.refresh_schedule()
        schedule_health_check()
        return released

    def process_quiet_window_tick(self):
        now_ts = time.time()
        with QUIET_WINDOW_LOCK:
            bucket_items = list(QUIET_WINDOW_STATES.items())
        for storage_key, snapshot in bucket_items:
            if not snapshot.get("active"):
                continue
            scope = {"account_key": snapshot.get("account_key"), "shop_num": snapshot.get("shop_num")}
            if _quiet_window_is_expired(snapshot, now_ts=now_ts):
                self._restore_runtime_services_after_quiet_release("ttl-expired", scope=scope)
                continue
            state = str(snapshot.get("state") or "")
            owner_task_id = snapshot.get("owner_task_id")
            if state == "fire_window" and owner_task_id is not None and not self.is_task_running(owner_task_id):
                mark_quiet_window_recovering(owner_task_id, reason="owner-finished", scope=scope)
                continue
            if state == "recovering":
                recover_until_ts = float(snapshot.get("recover_until_ts") or 0.0)
                if recover_until_ts > 0 and now_ts >= recover_until_ts:
                    self._restore_runtime_services_after_quiet_release("recovering-finished", scope=scope)
                continue

        now_dt = datetime.now()
        by_scope_key = {}
        for task in self.tasks:
            if not self._task_should_enter_quiet_window(task):
                continue
            next_run_dt = self._compute_next_run_datetime(task, now=now_dt)
            if next_run_dt is None:
                continue
            delta_s = (next_run_dt - now_dt).total_seconds()
            if delta_s < 0 or delta_s > QUIET_WINDOW_PREQUIET_SECONDS:
                continue
            _acc, cand_scope, cand_err = resolve_task_account_and_scope(task)
            if cand_err:
                continue
            sk = _scope_storage_key(cand_scope)
            prev = by_scope_key.get(sk)
            if prev is None or delta_s < prev[0]:
                by_scope_key[sk] = (delta_s, task, cand_scope, next_run_dt.timestamp())
        for sk, (_delta_s, candidate_task, cand_scope, candidate_fire_at) in by_scope_key.items():
            with QUIET_WINDOW_LOCK:
                existing = QUIET_WINDOW_STATES.get(sk)
                if existing and existing.get("active") and (not _quiet_window_is_expired(existing, now_ts=time.time())):
                    continue
            enter_quiet_window(
                candidate_task.get("id"),
                candidate_fire_at,
                reason=f"prepare-task-{candidate_task.get('id')}",
                scope=cand_scope,
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
        _acc, task_scope, acc_err = resolve_task_account_and_scope(task)
        if acc_err:
            log(f"❌ [自动任务] task={task_id} 账号不可用: {acc_err}")
            return False
        quiet_snapshot = quiet_window_snapshot(task_scope) or {}
        quiet_active = bool(quiet_snapshot.get("active")) and (not _quiet_window_is_expired(quiet_snapshot, now_ts=time.time()))
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
                    enter_quiet_window(task_id, time.time(), reason=f"late-owner-start-{task_id}", scope=task_scope)
                mark_quiet_window_fire(task_id, scope=task_scope)
                is_owner_task = True
            with runtime_request_context("task_execute", task_id=task_id, owner=is_owner_task):
                self.execute_task(task)
            return True
        finally:
            if is_owner_task:
                mark_quiet_window_recovering(task_id, reason="task-finally", scope=task_scope)
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
        for t in self.refill_tasks:
            if not isinstance(t, dict):
                continue
            try:
                t['interval_seconds'] = max(1.0, float(t.get('interval_seconds', 10.0) or 10.0))
            except Exception:
                t['interval_seconds'] = 10.0
            try:
                base_fast = min(float(t.get('interval_seconds', 10.0) or 10.0), 10.0)
                t['fast_interval_seconds'] = max(1.0, float(t.get('fast_interval_seconds', base_fast) or base_fast))
            except Exception:
                t['fast_interval_seconds'] = 10.0
            try:
                t['fast_window_minutes'] = max(0.0, float(t.get('fast_window_minutes', 0.0) or 0.0))
            except Exception:
                t['fast_window_minutes'] = 0.0
            try:
                t['interval_jitter_seconds'] = max(0.0, float(t.get('interval_jitter_seconds', 0.0) or 0.0))
            except Exception:
                t['interval_jitter_seconds'] = 0.0

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
        try:
            task['interval_jitter_seconds'] = max(0.0, float(task.get('interval_jitter_seconds', 0.0) or 0.0))
        except Exception:
            task['interval_jitter_seconds'] = 0.0
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
        task['accountId'] = str(task.get('accountId') or '').strip()
        self.refill_tasks.append(task)
        self.save_refill_tasks()
        return task

    def delete_refill_task(self, task_id):
        tid = int(task_id)
        self.refill_tasks = [t for t in self.refill_tasks if int(t.get('id', -1)) != tid]
        self._refill_last_run.pop(tid, None)
        self._refill_last_post_end_mono.pop(tid, None)
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
            if 'interval_jitter_seconds' in payload:
                try:
                    t['interval_jitter_seconds'] = max(0.0, float(payload.get('interval_jitter_seconds') or 0.0))
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
            if 'accountId' in payload:
                t['accountId'] = str(payload.get('accountId') or '').strip()
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
            # #region agent log
            debug_run_id = f"refill-{task_id}-{int(time.time() * 1000)}"
            def _dbg_log(hypothesis_id, location, message, data):
                try:
                    payload = {
                        "sessionId": "a1cc9c",
                        "runId": debug_run_id,
                        "hypothesisId": str(hypothesis_id),
                        "location": str(location),
                        "message": str(message),
                        "data": data if isinstance(data, dict) else {"value": str(data)},
                        "timestamp": int(time.time() * 1000),
                    }
                    with open("debug-a1cc9c.log", "a", encoding="utf-8") as _f:
                        _f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                except Exception:
                    pass
            # #endregion
            account_r, _sc_r, err_r = resolve_task_account_and_scope(refill_task)
            if err_r:
                log(f"❌ [refill#{task_id}|{source}] {err_r}")
                return {'status': 'error', 'msg': err_r}
            refill_client = build_client_for_account(account_r)
            acc_label = str(account_r.get("id") or "")
            tag = f"[refill#{task_id}|{source}|{acc_label}]"
            date_str = str(refill_task.get('date') or '').strip()
            target_times = [str(t).strip() for t in (refill_task.get('target_times') or []) if str(t).strip()]
            candidate_places = [str(p).strip() for p in (refill_task.get('candidate_places') or []) if str(p).strip()]
            target_count = max(1, min(MAX_TARGET_COUNT, int(refill_task.get('target_count', 1) or 1)))
            task_tokens = refill_task.get('pushplus_tokens') or None
            # #region agent log
            _dbg_log(
                "H1",
                "app.py:_run_refill_task_once:entry",
                "refill entry params",
                {
                    "task_id": task_id,
                    "date": date_str,
                    "target_times": target_times,
                    "candidate_places_len": len(candidate_places),
                    "candidate_places_tail": candidate_places[-5:],
                },
            )
            # #endregion

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
            matrix_timeout_s = max(0.5, float(CONFIG.get("matrix_timeout_seconds", 3.0) or 3.0))
            matrix_res = refill_client.get_matrix(
                date_str,
                include_mine_overlay=False,
                request_timeout=matrix_timeout_s,
                bypass_cache=True,
            )
            if 'error' in matrix_res:
                msg = f"获取矩阵失败: {matrix_res.get('error')}"
                log(f"❌ {tag} {msg}")
                return {'status': 'error', 'msg': msg}
            matrix = matrix_res.get('matrix') or {}
            # #region agent log
            avail_by_time_all = {}
            avail_by_time_le14 = {}
            mine_by_time = {}
            for _t in target_times:
                _avail_all = []
                _avail_le14 = []
                _mine = []
                for _p in candidate_places:
                    _st = (matrix.get(str(_p)) or {}).get(str(_t))
                    if _st == "available":
                        _avail_all.append(str(_p))
                        if str(_p).isdigit() and int(str(_p)) <= 14:
                            _avail_le14.append(str(_p))
                    if _st in ("mine", "self", "my_booked", "mybooked", "booked_by_me"):
                        _mine.append(str(_p))
                avail_by_time_all[_t] = _avail_all
                avail_by_time_le14[_t] = _avail_le14
                mine_by_time[_t] = _mine
            _dbg_log(
                "H2",
                "app.py:_run_refill_task_once:matrix_snapshot",
                "matrix availability snapshot",
                {
                    "avail_all": avail_by_time_all,
                    "avail_le14": avail_by_time_le14,
                    "mine_by_time": mine_by_time,
                },
            )
            # #endregion

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
            # #region agent log
            _dbg_log(
                "H3",
                "app.py:_run_refill_task_once:need_by_time",
                "computed need_by_time from mine overlay",
                {
                    "target_count": target_count,
                    "need_by_time": dict(need_res.get("need_by_time") or {}),
                },
            )
            # #endregion

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
                        wx_list = []
                        if isinstance(task_tokens, str):
                            wx_list = [x.strip() for x in task_tokens.split(",") if x.strip()]
                        elif isinstance(task_tokens, list):
                            wx_list = [str(x).strip() for x in task_tokens if str(x).strip()]
                        line = self._build_short_title("已停补", date_str, pseudo_items) or "已停补"
                        if wx_list:
                            self.send_wechat_notification(line, tokens=wx_list, title=line)
                except Exception as e:
                    log(f"⚠️ [refill#{task_id}] 自动停用/通知失败: {e}")
                return {'status': 'success', 'msg': msg, 'success_items': []}

            candidate_place_nums = []
            for p in candidate_places:
                try:
                    if str(p).isdigit():
                        candidate_place_nums.append(int(str(p)))
                except Exception:
                    continue
            intent_base = {
                "target_blocks": target_count,
                "target_times": list(target_times),
                "time_preference_order": list(target_times),
                "preferred_place_min": int(refill_task.get("preferred_place_min") or 0),
                "preferred_place_max": int(refill_task.get("preferred_place_max") or 0),
            }
            if candidate_place_nums:
                intent_base["selectable_place_min"] = min(candidate_place_nums)
                intent_base["selectable_place_max"] = max(candidate_place_nums)
            allow_scatter = not bool(refill_task.get("require_consecutive"))
            # #region agent log
            _dbg_log(
                "H1",
                "app.py:_run_refill_task_once:before_solve",
                "solver input intent/flags",
                {
                    "intent_base": dict(intent_base),
                    "allow_scatter": bool(allow_scatter),
                    "candidate_places_len": len(candidate_places),
                },
            )
            # #endregion
            solved, used_need, tier_label = solve_refill_need_tiered(
                matrix,
                candidate_places,
                intent_base,
                dict(need_res.get("need_by_time") or {}),
                allow_scatter=allow_scatter,
            )
            picks = normalize_booking_items((solved or {}).get("items") or [])
            # #region agent log
            _dbg_log(
                "H4",
                "app.py:_run_refill_task_once:after_solve",
                "solver output",
                {
                    "tier_label": str(tier_label),
                    "used_need": dict(used_need or {}),
                    "picks_len": len(picks),
                    "picks": picks[:8],
                },
            )
            # #endregion

            if not picks:
                msg = f"当前无可补订组合，缺口: {need_res['need_by_time']}"
                log(f"🙈 {tag} {msg}")
                return {'status': 'fail', 'msg': msg}

            log(f"📦 {tag} 分层 tier={tier_label} used_need={used_need} 本轮提交: {picks}")
            try:
                delivery_min_post_interval_s = max(
                    0.0,
                    float(CONFIG.get("delivery_min_post_interval_seconds", 2.2) or 0.0),
                )
            except Exception:
                delivery_min_post_interval_s = 0.0
            if delivery_min_post_interval_s > 0:
                tid = int(refill_task.get('id', 0) or 0)
                last_post_end = self._refill_last_post_end_mono.get(tid)
                if last_post_end is not None:
                    remain = delivery_min_post_interval_s - (time.perf_counter() - float(last_post_end))
                    if remain > 0:
                        time.sleep(remain)
            submit_res = refill_client.submit_delivery_campaign(
                date_str,
                [{"id": "primary", "label": "Refill", "items": picks}],
                submit_profile="auto_minimal",
                task_config={"delivery_groups": [{"id": "primary", "label": "Refill", "items": picks}]},
            )
            if delivery_min_post_interval_s > 0:
                tid = int(refill_task.get('id', 0) or 0)
                self._refill_last_post_end_mono[tid] = time.perf_counter()
            log(f"🧾 {tag} 本轮结果: {submit_res.get('status')} - {submit_res.get('msg')}")
            if submit_res.get('status') in ('success', 'partial') and (submit_res.get('success_items') or []):
                ok_items = submit_res.get('success_items') or []
                item_text = '、'.join([f"{it.get('place')}号{it.get('time')}" for it in ok_items[:6]])
                msg = f"Refill#{task_id}补订成功({len(ok_items)}项): {date_str} {item_text}"
                wx_list = []
                if isinstance(task_tokens, str):
                    wx_list = [x.strip() for x in task_tokens.split(",") if x.strip()]
                elif isinstance(task_tokens, list):
                    wx_list = [str(x).strip() for x in task_tokens if str(x).strip()]
                line = self._build_short_title("已补订", date_str, ok_items) or "已补订"
                if wx_list:
                    self.send_wechat_notification(line, tokens=wx_list, title=line)
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
                    task_tokens = t.get('pushplus_tokens') or None
                    wx_list = []
                    if isinstance(task_tokens, str):
                        wx_list = [x.strip() for x in task_tokens.split(",") if x.strip()]
                    elif isinstance(task_tokens, list):
                        wx_list = [str(x).strip() for x in task_tokens if str(x).strip()]
                    pseudo_items = []
                    times = [str(x).strip() for x in (t.get('target_times') or []) if str(x).strip()]
                    places = [str(x).strip() for x in (t.get('candidate_places') or []) if str(x).strip()]
                    for p in places:
                        for tm in times:
                            pseudo_items.append({"place": p, "time": tm})
                    base = self._build_short_title("已停补", date_str, pseudo_items) or "已停补"
                    line = f"{base}截止"
                    if wx_list:
                        self.send_wechat_notification(line, tokens=wx_list, title=line)
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
            try:
                interval_jitter_s = max(0.0, float(t.get('interval_jitter_seconds', 0.0) or 0.0))
            except Exception:
                interval_jitter_s = 0.0
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
            sampled_jitter = random.uniform(0.0, interval_jitter_s) if interval_jitter_s > 0 else 0.0
            _ra, scope_r, err_r = resolve_task_account_and_scope(t)
            if err_r:
                self._refill_last_run[tid] = now + sampled_jitter
                t['last_result'] = {'status': 'config_error', 'msg': err_r}
                log(f"⚠️ [refill#{tid}] 账号无效，跳过: {err_r}")
                continue
            quiet_info = quiet_window_block_info("refill_scheduler", owner_allowed=False, scope=scope_r)
            if quiet_info:
                self._refill_last_run[tid] = now + sampled_jitter
                t['last_result'] = {'status': 'quiet_skipped', 'msg': quiet_info.get('msg')}
                log(f"🔇 [refill#{tid}] 已跳过：{quiet_info.get('msg')}")
                continue
            self._refill_last_run[tid] = now + sampled_jitter
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
        elif 'target_count' in cfg:
            try:
                cfg['target_count'] = max(1, min(MAX_TARGET_COUNT, int(cfg.get('target_count', 2))))
            except Exception:
                cfg['target_count'] = 2

        for _dk in list(cfg.keys()):
            if not isinstance(_dk, str) or not _dk.startswith("delivery_"):
                continue
            if _dk == "delivery_groups" or _dk in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
                continue
            cfg.pop(_dk, None)

        task['config'] = cfg
        return task

    def save_tasks(self):
        with open(TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)
            
    def add_task(self, task):
        # task: {id, type='daily'|'weekly', run_time='08:00', target_day_offset=2, items=[...]}
        _cfg_pre = task.get("config") if isinstance(task.get("config"), dict) else None
        if _cfg_pre is not None:
            _ve = validate_task_venue_strategy(_cfg_pre)
            if _ve:
                raise ValueError("; ".join(_ve))
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
                _cfg_pre = task.get("config") if isinstance(task.get("config"), dict) else None
                if _cfg_pre is not None:
                    _ve = validate_task_venue_strategy(_cfg_pre)
                    if _ve:
                        raise ValueError("; ".join(_ve))
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
        account_exec, _scope_exec, acc_err_exec = resolve_task_account_and_scope(task)
        if acc_err_exec:
            log(f"❌ [自动任务] task={task.get('id')} {acc_err_exec}")
            return
        task_client = build_client_for_account(account_exec)
        log(f"⏰ [自动任务] 开始执行任务: {task.get('id')} account={account_exec.get('id')}")
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
            "unknown_business_fail_count": 0,
            "payload_fail_count": 0,
            "matrix_fetch_fail_count": 0,
            "matrix_timeout_count": 0,
            "matrix_connection_error_count": 0,
            "matrix_resp_404_count": 0,
            "matrix_resp_5xx_count": 0,
            "transport_error_events": [],
            "combo_tier": None,
            "backup_promoted_count": 0,
            "refill_matrix_fetch_count": 0,
            "too_fast_matrix_refresh_count": 0,
            "refill_candidate_found_count": 0,
            "refill_no_candidate_count": 0,
            "refill_no_candidate_max_streak": 0,
            "refill_no_candidate_streak_final": 0,
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

        def notify_task_result(success, message, items=None, date_str=None, partial=False):
            phones_list = []
            if isinstance(task_phones, str):
                phones_list = [p.strip() for p in task_phones.split(",") if p.strip()]
            elif isinstance(task_phones, list):
                phones_list = [str(p).strip() for p in task_phones if str(p).strip()]
            tokens_list = []
            if isinstance(task_pushplus_tokens, str):
                tokens_list = [p.strip() for p in task_pushplus_tokens.split(",") if p.strip()]
            elif isinstance(task_pushplus_tokens, list):
                tokens_list = [str(p).strip() for p in task_pushplus_tokens if str(p).strip()]

            if success or partial:
                line = self._build_short_title("已预订", date_str, items or []) or "已预订"
                if partial:
                    line = f"{line}（部分）"
            else:
                line = self._build_short_title("已失败", date_str, items or []) or "已失败"
            if phones_list:
                self.send_notification(line, phones=phones_list)
            if tokens_list:
                self.send_wechat_notification(line, tokens=tokens_list, title=line)

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
            sub_lat = submit_metric.get("submit_latencies_ms")
            if isinstance(sub_lat, list) and sub_lat:
                base_lat = run_metrics.setdefault("submit_latencies_ms", [])
                for x in sub_lat:
                    try:
                        base_lat.append(int(x))
                    except Exception:
                        continue
                over = len(base_lat) - METRICS_LATENCY_SAMPLES_KEEP
                if over > 0:
                    del base_lat[0:over]
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
                "unknown_business_fail_count",
                "payload_fail_count",
                "backup_promoted_count",
                "refill_matrix_fetch_count",
                "too_fast_matrix_refresh_count",
                "refill_candidate_found_count",
                "refill_no_candidate_count",
                "matrix_fetch_fail_count",
                "matrix_timeout_count",
                "matrix_connection_error_count",
                "matrix_resp_404_count",
                "matrix_resp_5xx_count",
            ):
                run_metrics[key] = int(run_metrics.get(key) or 0) + int(submit_metric.get(key) or 0)
            run_metrics["refill_no_candidate_max_streak"] = max(
                int(run_metrics.get("refill_no_candidate_max_streak") or 0),
                int(submit_metric.get("refill_no_candidate_max_streak") or 0),
            )
            run_metrics["refill_no_candidate_streak_final"] = max(
                int(run_metrics.get("refill_no_candidate_streak_final") or 0),
                int(submit_metric.get("refill_no_candidate_streak_final") or 0),
            )
            sub_ev = submit_metric.get("transport_error_events")
            if isinstance(sub_ev, list) and sub_ev:
                base_ev = run_metrics.setdefault("transport_error_events", [])
                for ev in sub_ev:
                    if isinstance(ev, dict):
                        base_ev.append(dict(ev))
                over_ev = len(base_ev) - TRANSPORT_ERROR_EVENTS_MAX
                if over_ev > 0:
                    del base_ev[0:over_ev]
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
                for arr_key in ("submit_latencies_ms", "confirm_latencies_ms"):
                    arr = run_metrics.get(arr_key)
                    if isinstance(arr, list) and len(arr) > METRICS_LATENCY_SAMPLES_KEEP:
                        run_metrics[arr_key] = arr[-METRICS_LATENCY_SAMPLES_KEEP:]
                tev = run_metrics.get("transport_error_events")
                if isinstance(tev, list) and len(tev) > TRANSPORT_ERROR_EVENTS_MAX:
                    run_metrics["transport_error_events"] = tev[-TRANSPORT_ERROR_EVENTS_MAX:]
                append_task_run_metric(run_metrics)
                evs = run_metrics.get("transport_error_events") or []
                last_ev = evs[-1] if isinstance(evs, list) and evs else None
                last_snip = ""
                if isinstance(last_ev, dict):
                    last_snip = str(last_ev.get("snippet") or "")[:80]
                log(
                    f"📊 [run-metric] task={run_metrics.get('task_id')} matrix_attempts={run_metrics.get('attempt_count')} "
                    f"submit_req={run_metrics.get('submit_req_count')} post_timeout={run_metrics.get('timeout_count') or 0} "
                    f"mx_fail={run_metrics.get('matrix_fetch_fail_count') or 0} mx_timeout={run_metrics.get('matrix_timeout_count') or 0} "
                    f"first_matrix={run_metrics.get('first_matrix_ok_ms')}ms first_submit={run_metrics.get('first_submit_ms')}ms "
                    f"first_post={run_metrics.get('t_first_post_ms')}ms "
                    f"first_success={run_metrics.get('first_success_ms')}ms p95={run_metrics.get('submit_latency_p95_ms')}ms "
                    f"last_ev={last_snip!r}"
                )
            except Exception as e:
                log(f"⚠️ [run-metric] 汇总失败: {e}")

        # 0. legacy 模式下先检查 token；极简直提模式跳过该额外请求，缩短首个有效 POST 路径。
        if not direct_mode_enabled:
            is_valid, token_msg = task_client.check_token()
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

            aligned_now = task_client.get_aligned_now()
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
                f"🕒 [时间对齐] server_offset={round(task_client.server_time_offset_seconds, 3)}s, "
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
                        now_aligned = task_client.get_aligned_now()
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
                        probe = task_client.check_booking_auth_probe()
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
                        now_aligned = task_client.get_aligned_now()
                        seconds_to_base = (base_run - now_aligned).total_seconds()
                        if seconds_to_base <= 5.0:
                            break
                        # 保证至少 min_gap 秒间隔，同时不跨过 base_run-3s
                        sleep_s = min(min_gap, max(0.5, seconds_to_base - 3.0))
                        if sleep_s > 0:
                            time.sleep(sleep_s)
            except Exception as e:
                log(f"⚠️ [预热探针] 执行异常，跳过预热: {e}")

            aligned_now_after = task_client.get_aligned_now()
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

        # 3. 旧版兼容：仅 items、无 config 时仍走极速订场单组
        if not config and 'items' in task:
            legacy_items = normalize_booking_items(task['items'])
            res = task_client.submit_delivery_campaign(
                target_date,
                [{"id": "primary", "label": "主组", "items": legacy_items}],
                submit_profile="auto_minimal",
                task_config={"delivery_groups": [{"id": "primary", "label": "主组", "items": legacy_items}]},
            )
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
                mx_snap_t0 = time.perf_counter()
                snapshot_res = task_client.get_matrix(target_date, include_mine_overlay=False)
                if run_metrics.get("first_matrix_ok_ms") is None and isinstance(snapshot_res, dict) and not snapshot_res.get("error"):
                    run_metrics["first_matrix_ok_ms"] = int(max(0.0, snapshot_started_at - active_started_ts) * 1000)
                elif isinstance(snapshot_res, dict) and snapshot_res.get("error"):
                    err_snap = str(snapshot_res.get("error") or "")
                    record_matrix_fetch_failure(
                        run_metrics,
                        "presubmit_matrix",
                        err_snap,
                        int((time.perf_counter() - mx_snap_t0) * 1000),
                    )
                    log(f"⚠️ [极简直提] 预提交矩阵快照失败，继续直提: {snapshot_res.get('error')}")

            run_metrics["attempt_count"] += 1
            submit_started_at = time.time()
            if run_metrics.get("first_submit_ms") is None:
                first_post_ms = int(max(0.0, submit_started_at - active_started_ts) * 1000)
                run_metrics["first_submit_ms"] = first_post_ms
                run_metrics["t_first_post_ms"] = first_post_ms
            run_metrics["target_date"] = str(target_date)
            log(f"🚀 [终极递送器] 启动主组合递送: {primary_items}")
            res = task_client.submit_delivery_campaign(target_date, delivery_groups, submit_profile="auto_minimal", task_config=config)
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

    def refresh_schedule(self):
        schedule.clear("task")
        print(f"🔄 [调度器] 正在刷新任务列表 (共 {len(self.tasks)} 个)...")

        # 内部工具函数：支持单次任务执行完后自动删除自身
        def make_job(t, is_once=False):
            def _job():
                print(f"⏰ [调度器] 触发任务 ID: {t['id']}")

                def _runner():
                    try:
                        self.execute_task_with_lock(t)
                    finally:
                        if is_once:
                            print(f"✅ 单次任务 {t['id']} 执行完成，自动从任务列表中删除")
                            self.delete_task(t['id'], refresh=False)

                threading.Thread(target=_runner, daemon=True).start()
                # 同刻多任务若同步执行会阻塞 run_pending，导致只能串行；改为后台线程与「立即运行」一致。
                if is_once:
                    return schedule.CancelJob
                return None

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

# 启动后台线程（CLI/探针 import app 前可设 BEIJINTICK_SKIP_IMPORT_SCHEDULER=1 跳过）
if os.environ.get("BEIJINTICK_SKIP_IMPORT_SCHEDULER", "").strip() != "1":
    threading.Thread(target=run_scheduler, daemon=True).start()

# ================= Web 管理端 HTTP Basic（可选） =================
web_ui_http_auth = HTTPBasicAuth()


@web_ui_http_auth.verify_password
def _verify_web_ui_http_basic(username, password):
    cfg = CONFIG.get("web_ui_auth") or {}
    u = str(cfg.get("username") or "").strip()
    p = str(cfg.get("password") or "")
    if not u or not p:
        return False
    return username == u and password == p


@app.before_request
def _require_web_ui_http_basic():
    cfg = CONFIG.get("web_ui_auth") or {}
    if not cfg.get("enabled"):
        return None
    if request.endpoint == "static":
        return None
    return web_ui_http_auth.login_required(lambda: None)()


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
    date = request.args.get('date')
    include_mine_raw = request.args.get('include_mine', '1')
    include_mine_overlay = str(include_mine_raw).lower() not in ('0', 'false', 'no')
    account_id = request.args.get('accountId')
    account, account_err = resolve_manual_account_from_request(account_id, require_shop_num=True)
    if account_err:
        return jsonify({"error": account_err})
    quiet_info = quiet_window_block_info("api_matrix", owner_allowed=False, scope=build_quiet_window_scope(auth=account))
    if quiet_info:
        return jsonify({"error": quiet_info.get("msg"), "quiet_window_blocked": True, "quiet_window": quiet_info.get("quiet_window")})
    account_client = build_client_for_account(account)
    with runtime_request_context("api_matrix", owner=False):
        return jsonify(account_client.get_matrix(date, include_mine_overlay=include_mine_overlay))

@app.route('/api/mine-overview')
def api_mine_overview():
    include_balance_raw = request.args.get('include_balance', '0')
    include_balance = str(include_balance_raw).lower() in ('1', 'true', 'yes')
    order_max_pages_raw = request.args.get('order_max_pages', '2')
    order_timeout_s_raw = request.args.get('order_timeout_s', '4')
    max_workers_raw = request.args.get('max_workers', '')
    try:
        order_max_pages = int(order_max_pages_raw)
    except (TypeError, ValueError):
        order_max_pages = 2
    order_max_pages = max(1, min(order_max_pages, 4))
    try:
        order_timeout_s = float(order_timeout_s_raw)
    except (TypeError, ValueError):
        order_timeout_s = 4.0
    order_timeout_s = max(1.0, min(order_timeout_s, 8.0))
    try:
        max_workers = int(max_workers_raw) if str(max_workers_raw).strip() else 4
    except (TypeError, ValueError):
        max_workers = 4
    max_workers = max(1, min(max_workers, 6))

    def _fetch_account_overview(idx, acc, include_balance_flag=False):
        account_id = str(acc.get("id") or "").strip()
        account_name = str(acc.get("name") or account_id or f"账号{idx + 1}").strip() or f"账号{idx + 1}"
        color_key = f"acc-{idx + 1}"
        if not account_id:
            return {"skip": True}
        if not str(acc.get("token") or "").strip() or not str(acc.get("shop_num") or "").strip():
            return {
                "skip": False,
                "accountId": account_id,
                "accountName": account_name,
                "colorKey": color_key,
                "balance": None,
                "error": "缺少 token 或 shop_num",
                "slots": [],
            }

        account_client = build_client_for_account(acc)
        with runtime_request_context("api_mine_overview", owner=False):
            orders_res = account_client.get_place_orders(
                max_pages=order_max_pages,
                timeout_s=order_timeout_s,
            )
        if 'error' in orders_res:
            err_msg = str(orders_res.get('error') or '获取订单失败')
            return {
                "skip": False,
                "accountId": account_id,
                "accountName": account_name,
                "colorKey": color_key,
                "balance": None,
                "error": err_msg,
                "slots": [],
            }

        balance = None
        if include_balance_flag:
            try:
                with runtime_request_context("api_mine_overview", owner=False):
                    card_res = account_client.get_use_card_info()
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

        grouped = account_client.extract_mine_slots_by_date_with_bill_num(orders_res.get('data') or [])
        slots = []
        for date_str, one_date_slots in (grouped or {}).items():
            for it in one_date_slots or []:
                row = {
                    "date": str(date_str),
                    "place": str(it.get("place") or "").strip(),
                    "time": str(it.get("time") or "").strip(),
                    "billNum": str(it.get("billNum") or "").strip(),
                    "accountId": account_id,
                    "accountName": account_name,
                    "accountColorKey": color_key,
                }
                if row["place"] and row["time"]:
                    slots.append(row)
        return {
            "skip": False,
            "accountId": account_id,
            "accountName": account_name,
            "colorKey": color_key,
            "balance": balance,
            "error": "",
            "slots": slots,
        }

    records = {}
    accounts_meta = []
    account_errors = []
    accounts = ensure_accounts_config()
    indexed_accounts = list(enumerate(accounts))
    worker_count = max(1, min(max_workers, len(indexed_accounts) if indexed_accounts else 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_fetch_account_overview, idx, acc, include_balance): (idx, acc)
            for idx, acc in indexed_accounts
        }
        for fut in as_completed(future_map):
            idx, acc = future_map[fut]
            account_id = str(acc.get("id") or "").strip()
            account_name = str(acc.get("name") or account_id or f"账号{idx + 1}").strip() or f"账号{idx + 1}"
            color_key = f"acc-{idx + 1}"
            try:
                result = fut.result()
            except Exception as e:
                err_msg = str(e) or "账号查询异常"
                account_errors.append({"accountId": account_id, "accountName": account_name, "error": err_msg})
                accounts_meta.append({"id": account_id, "name": account_name, "colorKey": color_key, "balance": None, "error": err_msg, "_order": idx})
                continue
            if result.get("skip"):
                continue
            err = str(result.get("error") or "").strip()
            if err:
                account_errors.append({"accountId": result.get("accountId"), "accountName": result.get("accountName"), "error": err})
            accounts_meta.append({
                "id": result.get("accountId"),
                "name": result.get("accountName"),
                "colorKey": result.get("colorKey"),
                "balance": result.get("balance"),
                "error": err,
                "_order": idx,
            })
            for row in result.get("slots") or []:
                target = records.setdefault(str(row.get("date") or ""), [])
                target.append({
                    "place": str(row.get("place") or "").strip(),
                    "time": str(row.get("time") or "").strip(),
                    "billNum": str(row.get("billNum") or "").strip(),
                    "accountId": str(row.get("accountId") or "").strip(),
                    "accountName": str(row.get("accountName") or "").strip(),
                    "accountColorKey": str(row.get("accountColorKey") or "").strip(),
                })

    accounts_meta.sort(key=lambda x: int(x.get("_order") or 0))
    for x in accounts_meta:
        x.pop("_order", None)

    for d, rows in list(records.items()):
        records[d] = sorted(
            rows,
            key=lambda x: (
                str(x.get("accountName") or ""),
                int(x.get("place")) if str(x.get("place") or "").isdigit() else 999,
                str(x.get("time") or ""),
            ),
        )
    return jsonify({"records": records, "accounts": accounts_meta, "errors": account_errors})


@app.route('/api/mine-balances')
def api_mine_balances():
    max_workers_raw = request.args.get('max_workers', '')
    try:
        max_workers = int(max_workers_raw) if str(max_workers_raw).strip() else 4
    except (TypeError, ValueError):
        max_workers = 4
    max_workers = max(1, min(max_workers, 6))
    accounts = ensure_accounts_config()
    indexed_accounts = list(enumerate(accounts))
    worker_count = max(1, min(max_workers, len(indexed_accounts) if indexed_accounts else 1))
    result_accounts = []
    result_errors = []

    def _fetch_balance(idx, acc):
        account_id = str(acc.get("id") or "").strip()
        account_name = str(acc.get("name") or account_id or f"账号{idx + 1}").strip() or f"账号{idx + 1}"
        color_key = f"acc-{idx + 1}"
        if not account_id:
            return {"skip": True}
        if not str(acc.get("token") or "").strip() or not str(acc.get("shop_num") or "").strip():
            return {"skip": False, "id": account_id, "name": account_name, "colorKey": color_key, "balance": None, "error": "缺少 token 或 shop_num"}
        account_client = build_client_for_account(acc)
        balance = None
        err = ""
        try:
            with runtime_request_context("api_mine_balances", owner=False):
                card_res = account_client.get_use_card_info()
            if 'error' in card_res:
                err = str(card_res.get('error') or '余额获取失败')
            elif card_res.get('universal'):
                first = card_res['universal'][0]
                raw = first.get('cardcash')
                if raw is not None:
                    try:
                        balance = float(raw)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            err = str(e) or "余额获取失败"
        return {"skip": False, "id": account_id, "name": account_name, "colorKey": color_key, "balance": balance, "error": err}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(_fetch_balance, idx, acc): (idx, acc) for idx, acc in indexed_accounts}
        for fut in as_completed(future_map):
            idx, acc = future_map[fut]
            account_id = str(acc.get("id") or "").strip()
            account_name = str(acc.get("name") or account_id or f"账号{idx + 1}").strip() or f"账号{idx + 1}"
            color_key = f"acc-{idx + 1}"
            try:
                one = fut.result()
            except Exception as e:
                msg = str(e) or "余额获取失败"
                one = {"skip": False, "id": account_id, "name": account_name, "colorKey": color_key, "balance": None, "error": msg}
            if one.get("skip"):
                continue
            one["_order"] = idx
            result_accounts.append(one)
            if one.get("error"):
                result_errors.append({"accountId": one.get("id"), "accountName": one.get("name"), "error": one.get("error")})

    result_accounts.sort(key=lambda x: int(x.get("_order") or 0))
    for x in result_accounts:
        x.pop("_order", None)
    return jsonify({"accounts": result_accounts, "errors": result_errors})


@app.route('/api/cancel-order', methods=['POST'])
def api_cancel_order():
    data = request.get_json() or {}
    bill_num = (data.get('billNum') or '').strip()
    account_id = (data.get('accountId') or '').strip()
    if not bill_num:
        return jsonify({'ok': False, 'msg': '缺少 billNum'})
    if not account_id:
        return jsonify({'ok': False, 'msg': '缺少 accountId'})
    account = resolve_account(account_id)
    if not account:
        return jsonify({'ok': False, 'msg': '账号不存在或已删除'})
    if not str(account.get("token") or "").strip():
        return jsonify({'ok': False, 'msg': '该账号未配置 token'})
    reason = (data.get('reason') or '用户取消').strip() or '用户取消'
    account_client = build_client_for_account(account)
    with runtime_request_context("api_cancel_order", owner=False):
        res = account_client.cancel_place_order(bill_num, reason=reason)
    if res.get('ok'):
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': res.get('msg', '取消失败')})


@app.route('/api/time')
def api_time():
    return jsonify({"timestamp": datetime.now().timestamp()})

@app.route('/api/quiet-window')
def api_quiet_window():
    account_id_raw = (request.args.get('accountId') or '').strip()
    account, acc_err = resolve_manual_account_from_request(account_id_raw, require_shop_num=True)
    if acc_err:
        return jsonify({"error": acc_err}), 400
    return jsonify(get_quiet_window_status(scope=build_quiet_window_scope(auth=account)))

@app.route('/api/book', methods=['POST'])
def api_book():
    data = request.json or {}
    date = data.get('date')
    items = data.get('items')
    account_id = data.get('accountId')
    account, account_err = resolve_manual_account_from_request(account_id, require_shop_num=True)
    if account_err:
        return jsonify({"status": "error", "msg": account_err})
    quiet_info = quiet_window_block_info("api_book", owner_allowed=False, scope=build_quiet_window_scope(auth=account))
    if quiet_info:
        return jsonify({
            "status": "quiet_window_blocked",
            "msg": quiet_info.get("msg"),
            "quiet_window": quiet_info.get("quiet_window"),
        })
    account_client = build_client_for_account(account)
    submit_mode = str(data.get('submit_mode') or '').strip().lower()
    if submit_mode in ("minimal", "direct"):
        manual_submit_profile = str(CONFIG.get("auto_submit_profile", "auto_minimal") or "auto_minimal").strip() or "auto_minimal"
    else:
        manual_submit_profile = str(CONFIG.get("manual_submit_profile", "manual_minimal") or "manual_minimal").strip() or "manual_minimal"
    with runtime_request_context("api_book", owner=False):
        res = account_client.submit_order(date, items, submit_profile=manual_submit_profile)

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
            verify_res = account_client.get_matrix(date, include_mine_overlay=False, request_timeout=verify_timeout_s)
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
                orders_res = account_client.get_place_orders(max_pages=order_max_pages, timeout_s=order_timeout_s)
                if isinstance(orders_res, dict) and not orders_res.get('error'):
                    grouped = account_client.extract_mine_slots_by_date(orders_res.get('data') or [])
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
                refill_matrix_res = account_client.get_matrix(date, include_mine_overlay=True, request_timeout=verify_timeout_s)
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
                        refill_res = account_client.submit_order(
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
            'unknown_business_fail_count': int(run_metric.get('unknown_business_fail_count') or 0),
            'payload_fail_count': int(run_metric.get('payload_fail_count') or 0),
            'matrix_fetch_fail_count': int(run_metric.get('matrix_fetch_fail_count') or 0),
            'matrix_timeout_count': int(run_metric.get('matrix_timeout_count') or 0),
            'matrix_connection_error_count': int(run_metric.get('matrix_connection_error_count') or 0),
            'matrix_resp_404_count': int(run_metric.get('matrix_resp_404_count') or 0),
            'matrix_resp_5xx_count': int(run_metric.get('matrix_resp_5xx_count') or 0),
            'transport_error_events': list(run_metric.get('transport_error_events') or [])[-TRANSPORT_ERROR_EVENTS_MAX:],
            'combo_tier': str(run_metric.get('combo_tier') or ''),
            'backup_promoted_count': int(run_metric.get('backup_promoted_count') or 0),
            'refill_matrix_fetch_count': int(run_metric.get('refill_matrix_fetch_count') or 0),
            'too_fast_matrix_refresh_count': int(run_metric.get('too_fast_matrix_refresh_count') or 0),
            'effective_delivery_min_post_interval_seconds': float(
                run_metric.get('effective_delivery_min_post_interval_seconds') or 0.0
            ),
            'refill_candidate_found_count': int(run_metric.get('refill_candidate_found_count') or 0),
            'refill_no_candidate_count': int(run_metric.get('refill_no_candidate_count') or 0),
            'refill_no_candidate_max_streak': int(run_metric.get('refill_no_candidate_max_streak') or 0),
            'refill_no_candidate_streak_final': int(run_metric.get('refill_no_candidate_streak_final') or 0),
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
    CONFIG.pop("max_places_per_timeslot", None)
    CONFIG.pop("delivery_refill_max_places_per_timeslot", None)
    for _k in DEPRECATED_EXEC_PARAM_KEYS:
        CONFIG.pop(_k, None)
    strip_delivery_keys_from_profiles(CONFIG)
    for _vk in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
        CONFIG.pop(_vk, None)
    sanitize_submit_profiles(CONFIG)
    ensure_accounts_config()
    sync_primary_client_auth()
    if not isinstance(CONFIG.get("gym_api_probe_presets"), list):
        CONFIG["gym_api_probe_presets"] = []


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
            if k in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
                continue
            try:
                saved_public[k] = copy.deepcopy(data[k])
                CONFIG[k] = copy.deepcopy(data[k])
            except Exception:
                pass
        _strip_venue_strategy_from_mapping(saved_public)
        for _vk in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
            CONFIG.pop(_vk, None)
        strip_delivery_keys_from_profiles(saved_public)
        _import_trial = dict(CONFIG)
        _import_trial.update({k: v for k, v in saved_public.items() if k not in SENSITIVE_TOP_LEVEL_KEYS})
        strip_delivery_keys_from_profiles(_import_trial)
        _strip_venue_strategy_from_mapping(_import_trial)
        sanitize_submit_profiles(_import_trial)
        _import_errs = validate_required_execution_config(_import_trial)
        if _import_errs:
            return jsonify(
                {"status": "error", "msg": "导入后执行参数校验失败", "missing_or_invalid": _import_errs},
                400,
            )
        sanitize_submit_profiles(saved_public)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(saved_public, f, ensure_ascii=False, indent=2)
        ensure_accounts_config()
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
            if k in ("max_places_per_timeslot", "delivery_refill_max_places_per_timeslot"):
                continue
            if k in TASK_VENUE_STRATEGY_DELIVERY_KEYS:
                continue
            if k == 'gym_api_probe_presets':
                sanitized, serr = sanitize_gym_api_probe_presets_for_persist(data[k])
                if serr:
                    return jsonify({"status": "error", "msg": serr}), 400
                saved[k] = sanitized
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
                    else (
                        0
                        if key == "delivery_refill_no_candidate_streak_limit"
                        else (2.2 if key == "delivery_min_post_interval_seconds" else 20.0)
                    )
                ),
            )
            raw_val = saved.get(key)
            val, was_clamped = _clamp_exec_param(key, raw_val, default)
            saved[key] = val
            if was_clamped:
                clamped.append({"key": key, "requested": raw_val, "saved": val})
        strip_delivery_keys_from_profiles(saved)
        saved.pop("max_places_per_timeslot", None)
        saved.pop("delivery_refill_max_places_per_timeslot", None)
        saved_public = {k: v for k, v in saved.items() if k not in SENSITIVE_TOP_LEVEL_KEYS}
        saved_public.pop("max_places_per_timeslot", None)
        saved_public.pop("delivery_refill_max_places_per_timeslot", None)
        _strip_venue_strategy_from_mapping(saved_public)
        sanitize_submit_profiles(saved_public)
        _trial_cfg = dict(CONFIG)
        _trial_cfg.update(saved_public)
        strip_delivery_keys_from_profiles(_trial_cfg)
        _strip_venue_strategy_from_mapping(_trial_cfg)
        sanitize_submit_profiles(_trial_cfg)
        _vex = validate_required_execution_config(_trial_cfg)
        if _vex:
            return jsonify(
                {"status": "error", "msg": "执行参数校验失败", "missing_or_invalid": _vex},
                400,
            )
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
        if 'accounts' not in saved:
            saved['accounts'] = copy.deepcopy(CONFIG.get('accounts', []))
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
        if 'accounts' in data:
            accounts = normalize_accounts(data.get('accounts') or [])
            invalid_accounts = []
            for acc in accounts:
                limit = acc.get("delivery_max_places_per_timeslot")
                if limit is None:
                    invalid_accounts.append(str(acc.get("id") or ""))
            if invalid_accounts:
                details = [
                    f"账号 {aid or '-'} 缺少 delivery_max_places_per_timeslot（1-6）"
                    for aid in invalid_accounts
                ]
                return jsonify({
                    "status": "error",
                    "msg": "基础参数校验失败",
                    "missing_or_invalid": details,
                }), 400
            CONFIG['accounts'] = accounts
            saved['accounts'] = copy.deepcopy(accounts)

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
        if 'delivery_refill_no_candidate_streak_limit' in data:
            try:
                val, _ = _clamp_exec_param(
                    'delivery_refill_no_candidate_streak_limit',
                    data['delivery_refill_no_candidate_streak_limit'],
                    CONFIG.get('delivery_refill_no_candidate_streak_limit', 0),
                )
                CONFIG['delivery_refill_no_candidate_streak_limit'] = val
                saved['delivery_refill_no_candidate_streak_limit'] = val
            except (TypeError, ValueError):
                pass
        for key in (
            'post_submit_verify_orders_on_matrix_partial_only', 'post_submit_skip_sync_orders_query',
            'post_submit_orders_sync_fallback', 'post_submit_treat_verify_timeout_as_retry',
            'manual_verify_pending_orders_fallback_enabled', 'manual_auto_refill_enabled', 'too_fast_skip_refill_in_same_request',
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
            _strip_venue_strategy_from_mapping(saved_public)
            sanitize_submit_profiles(saved_public)
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
    ensure_accounts_config()
    sync_primary_client_auth()
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
    """仅保存基础参数（通知手机号、PushPlus、sms、auth.card_index / auth.card_st_id）→ config.secret.json。"""
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
            _strip_venue_strategy_from_mapping(saved_public)
            sanitize_submit_profiles(saved_public)
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

        accounts = ensure_accounts_config()
        if not accounts:
            return jsonify({"status": "error", "msg": "未配置账号"})
        accounts[0]['token'] = token
        accounts[0]['cookie'] = cookie
        CONFIG['accounts'] = accounts
        ensure_accounts_config()
        sync_primary_client_auth()

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

            saved['accounts'] = copy.deepcopy(CONFIG.get('accounts') or [])
            saved['auth'] = copy.deepcopy(CONFIG.get('auth') or {})

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
        _rta, run_scope, run_acc_err = resolve_task_account_and_scope(task)
        if run_acc_err:
            return jsonify({"status": "error", "msg": run_acc_err}), 400
        quiet_info = quiet_window_block_info(
            "run_task_now",
            requester_task_id=str(task_id),
            owner_allowed=True,
            scope=run_scope,
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
    _aa, add_scope, add_acc_err = resolve_task_account_and_scope({"accountId": data.get("accountId")})
    if add_acc_err:
        return jsonify({"status": "error", "msg": add_acc_err}), 400
    if is_quiet_window_active(scope=add_scope):
        if 'enabled' in data and bool(data.get('enabled')):
            quiet_info = quiet_window_block_info("add_refill_task", owner_allowed=False, scope=add_scope) or {}
            return jsonify({'status': 'quiet_window_blocked', 'msg': quiet_info.get('msg', '静默窗口中，暂不允许启用 Refill 任务。'), 'quiet_window': quiet_info.get('quiet_window')})
        if 'enabled' not in data:
            data['enabled'] = False
    task = task_manager.add_refill_task(data)
    return jsonify({'status': 'success', 'task': task})




@app.route('/api/refill-tasks/<task_id>', methods=['PUT'])
def update_refill_task_api(task_id):
    data = request.json or {}
    task_existing = next((t for t in task_manager.refill_tasks if str(t.get('id')) == str(task_id)), None)
    if not task_existing:
        return jsonify({'status': 'error', 'msg': 'Refill task not found'}), 404
    merged_put = dict(task_existing)
    merged_put.update(data)
    _ua, upd_scope, upd_acc_err = resolve_task_account_and_scope(merged_put)
    if upd_acc_err:
        return jsonify({"status": "error", "msg": upd_acc_err}), 400
    if bool(data.get('enabled', False)) and is_quiet_window_active(scope=upd_scope):
        quiet_info = quiet_window_block_info("update_refill_task", owner_allowed=False, scope=upd_scope) or {}
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
    task = next((t for t in task_manager.refill_tasks if str(t.get('id')) == str(task_id)), None)
    if not task:
        return jsonify({'status': 'error', 'msg': 'Refill task not found'}), 404
    _rm, scope_rm, err_rm = resolve_task_account_and_scope(task)
    if err_rm:
        return jsonify({'status': 'error', 'msg': err_rm}), 400
    quiet_info = quiet_window_block_info("run_refill_task", owner_allowed=False, scope=scope_rm)
    if quiet_info:
        return jsonify({'status': 'quiet_window_blocked', 'msg': quiet_info.get('msg'), 'quiet_window': quiet_info.get('quiet_window')})

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
    data = request.get_json(silent=True) or {}
    account_id = data.get("accountId")
    account, account_err = resolve_manual_account_from_request(account_id, require_shop_num=True)
    if account_err:
        return jsonify({"status": "error", "msg": account_err})
    quiet_info = quiet_window_block_info("api_check_token", owner_allowed=False, scope=build_quiet_window_scope(auth=account))
    if quiet_info:
        return jsonify({
            "status": "quiet_window_blocked",
            "msg": quiet_info.get("msg"),
            "quiet_window": quiet_info.get("quiet_window"),
        })
    account_client = build_client_for_account(account)
    with runtime_request_context("api_check_token", owner=False):
        query_ok, query_msg = account_client.check_token()
        booking_probe = account_client.check_booking_auth_probe()

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
        "accountId": str(account.get("id") or ""),
        "accountName": str(account.get("name") or account.get("id") or ""),
        "query_auth_ok": query_ok,
        "query_auth_msg": query_msg,
        "booking_auth_ok": booking_probe.get('ok', False),
        "booking_auth_unknown": booking_probe.get('unknown', True),
        "booking_auth_msg": booking_probe.get('msg', ''),
    })


@app.route('/api/gym-probe', methods=['POST'])
def api_gym_probe():
    """服务端代发馆方 easyserpClient 请求，仅用于调试查看响应。"""
    data = request.get_json(silent=True) or {}
    account, account_err = resolve_manual_account_from_request(data.get('account_id'), require_shop_num=True)
    if account_err:
        return jsonify({"ok": False, "error": account_err}), 400
    method = str(data.get('method') or 'GET').upper()
    if method not in ('GET', 'POST'):
        return jsonify({"ok": False, "error": "method 仅支持 GET/POST"}), 400
    try:
        path_norm = normalize_gym_api_probe_path(data.get('path'))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if gym_api_probe_path_needs_confirm(path_norm) and not bool(data.get('confirm_write')):
        return jsonify({
            "ok": False,
            "error": "该路径可能产生订场/取消等写操作，请勾选「确认写请求」后重试",
            "needs_confirm_write": True,
        }), 400
    query_in = data.get('query')
    if query_in is None:
        query_in = {}
    if not isinstance(query_in, dict):
        return jsonify({"ok": False, "error": "query 须为 JSON 对象"}), 400
    if len(query_in) > GYM_API_PROBE_MAX_QUERY_KEYS:
        return jsonify({"ok": False, "error": "query 键过多"}), 400
    params = {str(k): ("" if v is None else str(v)) for k, v in query_in.items()}
    inject_auth = data.get('inject_auth')
    if inject_auth is None:
        inject_auth = True
    inject_auth = bool(inject_auth)
    inject_card = bool(data.get('inject_card_fields'))
    if inject_auth:
        params['token'] = str(account.get('token') or '')
        params['shopNum'] = str(account.get('shop_num') or '')
    if inject_card:
        ci = str(account.get('card_index') or '').strip()
        cs = str(account.get('card_st_id') or '').strip()
        if ci:
            params['card_index'] = ci
        if cs:
            params['card_st_id'] = cs
    client = build_client_for_account(account)
    quiet_info = quiet_window_block_info(
        'api_gym_probe',
        requester_task_id=None,
        owner_allowed=False,
        scope=client._quiet_scope_from_client(),
    )
    if quiet_info:
        return jsonify({
            'ok': False,
            'error': quiet_info.get('msg', '静默窗口中'),
            'quiet_window_blocked': True,
            'quiet_window': quiet_info.get('quiet_window'),
        })
    body_mode = str(data.get('body_mode') or 'form').strip().lower()
    if body_mode not in ('form', 'json'):
        body_mode = 'form'
    body_raw = data.get('body')
    body_str = '' if body_raw is None else str(body_raw)
    if len(body_str.encode('utf-8')) > GYM_API_PROBE_MAX_BODY_BYTES:
        return jsonify({'ok': False, 'error': 'body 过长'}), 400
    try:
        timeout = float(data.get('timeout') or 15)
    except (TypeError, ValueError):
        timeout = 15.0
    timeout = max(3.0, min(45.0, timeout))

    post_data = None
    json_body = None
    if method == 'POST':
        if inject_auth:
            if body_mode == 'json':
                try:
                    jd = json.loads(body_str) if body_str.strip() else {}
                except json.JSONDecodeError as e:
                    return jsonify({'ok': False, 'error': f'JSON 无效: {e}'}), 400
                if not isinstance(jd, dict):
                    return jsonify({'ok': False, 'error': 'JSON body 须为对象'}), 400
                jd['token'] = str(account.get('token') or '')
                jd['shopNum'] = str(account.get('shop_num') or '')
                json_body = jd
            else:
                pairs = urllib.parse.parse_qsl(body_str or '', keep_blank_values=True)
                bd = dict(pairs)
                bd['token'] = str(account.get('token') or '')
                bd['shopNum'] = str(account.get('shop_num') or '')
                post_data = urllib.parse.urlencode(bd, doseq=True)
        else:
            if body_mode == 'json':
                try:
                    json_body = json.loads(body_str) if body_str.strip() else {}
                except json.JSONDecodeError as e:
                    return jsonify({'ok': False, 'error': f'JSON 无效: {e}'}), 400
                if not isinstance(json_body, dict):
                    return jsonify({'ok': False, 'error': 'JSON body 须为对象'}), 400
            else:
                post_data = body_str

    t0 = time.time()
    try:
        resp, raw_err = client.raw_request(
            method,
            path_norm,
            params=params,
            data=post_data,
            json_body=json_body,
            timeout=timeout,
        )
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return jsonify({'ok': False, 'error': f'请求异常: {e}', 'elapsed_ms': elapsed_ms})
    elapsed_ms = int((time.time() - t0) * 1000)
    if raw_err:
        return jsonify({'ok': False, 'error': raw_err, 'elapsed_ms': elapsed_ms})
    resp_headers = {
        k: v
        for k, v in (resp.headers or {}).items()
        if str(k).lower() in GYM_API_PROBE_RESP_HEADER_ALLOW
    }
    try:
        text = resp.text or ''
    except Exception:
        text = ''
    truncated = False
    try:
        b = text.encode('utf-8')
        if len(b) > GYM_API_PROBE_MAX_RESPONSE_BYTES:
            text = b[:GYM_API_PROBE_MAX_RESPONSE_BYTES].decode('utf-8', errors='ignore')
            truncated = True
    except Exception:
        truncated = True
    body_json = None
    try:
        ct = (resp.headers.get('Content-Type') or '').lower()
        if 'json' in ct and text.strip():
            body_json = json.loads(text)
    except Exception:
        body_json = None
    query_keys = sorted(params.keys()) if params else []
    return jsonify({
        'ok': True,
        'status_code': resp.status_code,
        'elapsed_ms': elapsed_ms,
        'truncated': truncated,
        'request_redacted': {
            'method': method,
            'path': path_norm,
            'query_keys': query_keys,
        },
        'response_headers': resp_headers,
        'body_text': text,
        'body_json': body_json,
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
