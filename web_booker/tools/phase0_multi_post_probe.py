#!/usr/bin/env python3
"""
Phase 0：同一会话内「一次矩阵拉活」后连续多笔 reservation POST，记录每步服务端返回文案。

与极速订场一致：同一 ApiClient / Cookie；批间 sleep >= delivery_min_post_interval_seconds。

用法（仅拉矩阵、不下单）：
  cd web_booker
  python tools/phase0_multi_post_probe.py --plan tools/phase0_example_plan.json --dry-run

真实多批 POST（会尝试真实预订，请改用可取消日期/场地）：
  python tools/phase0_multi_post_probe.py --plan my_plan.json --confirm-live-post

计划文件 JSON 示例见 tools/phase0_example_plan.json。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

WEB_BOOKER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_plan(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("计划文件须为 JSON 对象")
    date_str = str(data.get("date") or "").strip()
    batches = data.get("batches")
    if not date_str:
        raise SystemExit("计划缺少 date (YYYY-MM-DD)")
    if not isinstance(batches, list) or not batches:
        raise SystemExit("计划缺少 batches 非空数组")
    for i, b in enumerate(batches):
        if not isinstance(b, list) or not b:
            raise SystemExit(f"batches[{i}] 须为非空数组")
    return data


def main() -> None:
    os.chdir(WEB_BOOKER_ROOT)
    if WEB_BOOKER_ROOT not in sys.path:
        sys.path.insert(0, WEB_BOOKER_ROOT)
    # import app 会加载全模块；跳过后台调度，避免仅跑探针时拉起定时线程
    os.environ["BEIJINTICK_SKIP_IMPORT_SCHEDULER"] = "1"

    parser = argparse.ArgumentParser(description="Phase 0 多批 POST 探针（同会话一次拉活）")
    parser.add_argument("--plan", required=True, help="JSON 计划路径（含 date、batches）")
    parser.add_argument("--dry-run", action="store_true", help="仅 get_matrix 拉活，不发 POST")
    parser.add_argument(
        "--confirm-live-post",
        action="store_true",
        help="确认将发起真实预订 POST（非 dry-run 时必选）",
    )
    parser.add_argument(
        "--out",
        default="",
        help="结果 JSON 输出路径；默认写入 logs/phase0_multi_post_<utc时间>.json",
    )
    args = parser.parse_args()

    plan = _load_plan(os.path.abspath(args.plan))
    date_str = str(plan["date"]).strip()
    batches_raw = plan["batches"]

    import app as wb  # noqa: E402 — 加载 CONFIG / client

    wb._load_config_from_disk()
    client = wb.client
    timeout_s = max(0.5, float(wb.CONFIG.get("submit_timeout_seconds", 4.0) or 4.0))
    matrix_timeout_s = max(0.5, float(wb.CONFIG.get("matrix_timeout_seconds", 3.0) or 3.0))
    min_interval = max(
        0.0,
        float(wb.CONFIG["delivery_min_post_interval_seconds"]),
    )
    url = f"https://{client.host}/easyserpClient/place/reservationPlace"
    headers_snapshot = dict(client.headers or {})

    record = {
        "phase": "phase0_server_multi_post",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "date": date_str,
        "dry_run": bool(args.dry_run),
        "delivery_min_post_interval_seconds": min_interval,
        "warmup_matrix": None,
        "posts": [],
        "conclusion_hint": "",
    }

    mx_t0 = time.perf_counter()
    warmup = client.get_matrix(
        date_str, include_mine_overlay=False, request_timeout=matrix_timeout_s, bypass_cache=True
    )
    record["warmup_matrix"] = {
        "elapsed_ms": int((time.perf_counter() - mx_t0) * 1000),
        "error": warmup.get("error") if isinstance(warmup, dict) else str(warmup),
        "meta": (warmup.get("meta") if isinstance(warmup, dict) else None),
    }
    if isinstance(warmup, dict) and not warmup.get("error"):
        record["warmup_matrix"]["matrix_keys_sample"] = sorted(
            list((warmup.get("matrix") or {}).keys())[:12]
        )

    if args.dry_run:
        record["conclusion_hint"] = "dry-run：未发 POST；请在开约后使用 --confirm-live-post 完成多批验证并归档返回文案。"
        _write_out(record, args.out)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return

    if not args.confirm_live_post:
        raise SystemExit("真实 POST 须同时指定 --confirm-live-post；或先用 --dry-run")

    last_end = None
    for idx, batch in enumerate(batches_raw):
        items = wb.normalize_booking_items(batch)
        if not items:
            raise SystemExit(f"批次 {idx} normalize 后为空")
        if min_interval > 0 and last_end is not None:
            remain = min_interval - (time.perf_counter() - last_end)
            if remain > 0:
                time.sleep(remain)
        body, field_info_list, total_money = client._build_reservation_body(date_str, items)
        post_t0 = time.perf_counter()
        result = client._post_reservation_once(
            client.session, headers_snapshot, url, body, timeout_s
        )
        last_end = time.perf_counter()
        raw_msg = result.get("raw_message") if result.get("ok") else result.get("exception_text")
        classified = client._classify_delivery_response(
            result.get("raw_message"),
            resp_data=result.get("resp_data"),
            exception_text=result.get("exception_text") if not result.get("ok") else None,
        )
        step = {
            "batch_index": idx,
            "items": items,
            "field_info_count": len(field_info_list),
            "total_money": total_money,
            "elapsed_ms": int(result.get("elapsed_ms") or 0),
            "ok": bool(result.get("ok")),
            "status_code": result.get("status_code"),
            "raw_message": str(raw_msg or "").strip(),
            "raw_text_snippet": (str(result.get("raw_text") or "")[:800]),
            "classified_action": classified.get("action"),
            "classified_bucket": classified.get("bucket"),
            "terminal_reason": classified.get("terminal_reason"),
        }
        record["posts"].append(step)
        print(
            f"[batch {idx}] action={step['classified_action']} msg={step['raw_message'][:200]!r}"
        )

    successes = sum(1 for p in record["posts"] if p.get("classified_action") == "stop_success")
    if successes == len(record["posts"]) and record["posts"]:
        record["conclusion_hint"] = "各批均为 stop_success：同会话多批 POST 均被服务端接受（请以订单页复核）。"
    elif successes > 0:
        record["conclusion_hint"] = "部分成功：后续批可能受规则/库存/会话影响，见各步 raw_message。"
    else:
        record["conclusion_hint"] = "无 stop_success：请根据 raw_message 判断是否需批间重拉矩阵或调整粒度。"

    _write_out(record, args.out)
    print(json.dumps(record, ensure_ascii=False, indent=2))


def _write_out(record: dict, out_path: str) -> None:
    logs_dir = os.path.join(WEB_BOOKER_ROOT, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    if out_path.strip():
        path = os.path.abspath(out_path)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(logs_dir, f"phase0_multi_post_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"已写入: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
