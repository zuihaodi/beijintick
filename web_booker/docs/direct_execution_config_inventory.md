# 极速订场（direct）执行参数分层说明

面向：**仅使用 `submit_delivery_campaign` + direct 任务**。完整可保存的键集合以 **[config.example.json](../config.example.json)** 为唯一范例；缺项或非法时保存会失败并提示对照该文件。

## A 类：主战高频（建议优先在「任务执行参数」里关注）

| 键 | 含义 |
|----|------|
| `delivery_total_budget_seconds` | 递送总预算（秒） |
| `delivery_warmup_budget_seconds` / `delivery_warmup_max_retries` | **已废弃**（旧版独立 warmup；现任务起即递送循环内拉矩阵） |
| `delivery_min_post_interval_seconds` | POST 最小间隔 |
| `delivery_transport_round_interval_seconds` | 轮次间隔 |
| `delivery_refill_matrix_poll_seconds` | 拉矩阵失败/无解后的短等待间隔 |
| `delivery_plan_max_age_seconds` | 基于某次矩阵快照的 POST 超过该秒数须先重拉矩阵再算场（必填） |
| `delivery_refill_no_candidate_streak_limit` | 连续无解早停（0=关） |
| `submit_timeout_seconds` / `matrix_timeout_seconds` | 下单与矩阵请求超时 |
| `delivery_account_phase_offset_ms` / `delivery_retry_jitter_ms` | 错峰与抖动 |
| `max_items_per_batch` / `max_consecutive_slots_per_place` | 分批与连续格上限 |
| `delivery_submit_granularity` | `per_legal_batch` / `single_cell` |
| `delivery_chunk_posts_by_fieldinfo` / `delivery_max_fieldinfo_hours` | fieldinfo 切批与单条最长小时 |
| `delivery_monotone_total_downgrade` / `delivery_solver_max_total_cells` / `delivery_solver_auto_time_consecutive` / `delivery_solver_aggressive_best_tier` | 组场求解策略 |
| `transient_storm_*` / `matrix_timeout_storm_seconds` | 矩阵风暴退避与加长超时 |

## B 类：次要但仍须在完整 config 中存在（校验或业务仍会读）

提交后校验、订单查询、日志、指标、健康检查、`submit_profiles` 等，见 `config.example.json` 中 A 块之后的键。**勿从磁盘删除**除非同步收紧 `validate_required_execution_config`。

## C 类：已从示例中移除（未接线或 pipeline 遗留）

曾出现在旧示例中的 `delivery_burst_workers`、`delivery_rate_limit_workers`、`delivery_transport_sustain_workers`、`delivery_burst_window_seconds`、`delivery_rate_limit_backoff_seconds`、`delivery_backup_switch_delay_seconds` 等，当前极速路径不依赖；磁盘上若仍保留无害，新装勿再抄入。

## 与校验器的关系

[`validate_required_execution_config`](../app.py) 对数值/整数必填项有明确要求；布尔类 `delivery_*` 若缺省则跳过类型检查（与现逻辑一致）。保存失败时服务端会在错误列表末尾追加一句，指向 **`web_booker/config.example.json`**。
