# 手动 / 自动预订产品对齐与实现设计方案

> 版本：v1.1  
> 状态：已落地（P0–P2：`submit_delivery_campaign` 批内保鲜、/api/book 默认无深度复核、gym_message_raw、前端 toast/原文；配置项 `manual_deep_reconcile_enabled`）

---

## 1. 目标摘要

| 线路 | 产品承诺 |
|------|----------|
| **手动预订** | 仅保留：**合法分批 POST** + **失败时原文提示** + **成功时短时 toast「预订成功」**。不把「历史已订 / 矩阵 mine / 半自动复核 / 补订」作为默认成功条件。 |
| **自动任务** | 保持：**矩阵驱动算场 → 分批 POST → refill 直至目标满足或预算耗尽**；可选性能优化见 §5。 |

---

## 2. 现状与差距

### 2.1 后端 `POST /api/book`（`app.py` · `api_book`）

1. 调用 `submit_order(..., skip_warmup=True)` → `submit_delivery_campaign`（统一递送循环）。
2. 若返回 `status == 'verify_pending'`：**大段逻辑**——矩阵多轮复核、订单兜底、`manual_auto_refill_enabled` 下二次 `submit_order` 补订，最后可能改写为 `success` / `partial`。

**与手动产品差距**：默认路径仍在做「深度核对 + 半自动补订」，**超出**「只认本次 POST 结论 + 原文错误」。

### 2.2 前端 `submitOrder`（`templates/index.html`）

- 成功：`showToast('请求已提交，请稍后手动刷新“已订补订”确认结果。', …)` —— **不是**「预订成功」短提示，且语义依赖用户再去别处确认。
- 失败：`alert(result.msg)` —— 需确认 `msg` 是否为**馆方原文**（或需增加 `server_msg_raw` / 分类器 `normalized_msg` 字段显式返回）。
- 请求体：`submit_mode: 'minimal'` → 后端选用 **`auto_submit_profile`**（`auto_minimal`），与「手动」命名易混淆；实施时可评估是否改为显式 `manual_minimal` 或专用 `request_mode`。

### 2.3 统一递送 `submit_delivery_campaign`（手动也走）

已识别问题：**`delivery_plan_max_age_seconds` 与首单 `same_batch_soft_retries=5`、`delivery_min_post_interval_seconds` 叠加**：在**同一批软重试**过程中频繁触发「算场快照过期」→ 无意义重拉矩阵，对手动固定 `items` 尤其不合理。

**与手动产品差距**：不解决则手动仍易出现「逻辑上不该刷新却刷新」的体验与日志噪音。

---

## 3. 目标架构（逻辑分层）

```
┌─────────────────────────────────────────────────────────────┐
│  手动页 /api/book                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 仅：submit_order(skip_warmup) → 直接采用 campaign 返回   │  │
│  │ 默认：不再进入 verify_pending 复核 / 订单兜底 / 自动补订  │  │
│  └───────────────────────────────────────────────────────┘  │
│  响应：success / fail / partial / quiet_window_blocked …     │
│       + 失败时携带可展示「原文」字段（见 §4.2）                 │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  自动任务 execute_task → submit_delivery_campaign             │
│  （skip_warmup=False）矩阵算场、refill、goal_satisfied 等      │
│  保持现有目标导向语义                                          │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 手动预订：目标步骤（产品 + 实现对应）

1. **输入**：`date`、`items`（用户选格）、`accountId`。
2. **静默窗口**：现有 `quiet_window` 逻辑保留。
3. **递送**：`submit_delivery_campaign`（`skip_warmup=True`）内：按需 `get_matrix`（会话/风控）、**不重算首组**（任务无 `delivery_first_group_from_matrix` 时与现网一致）、合法分批、顺序 POST。
4. **成功定义（产品）**：以 **campaign 对「本次手动请求」的终局**为准：例如 `status in ('success', 'partial')` 中，**仅当业务上视为用户所选批次已被接受**时算成功；**不**再因 `verify_pending` 在 `api_book` 内二次改写为成功。  
   - *实现时需与 `_classify_delivery_response` / 当前 `success`/`partial`/`fail` 语义对齐，必要时为手动增加显式 `manual_terminal: accepted|rejected` 或收窄 `success` 条件。*
5. **失败**：HTTP 或业务否定时，响应中带 **馆方可见原文**（见下节）。
6. **前端**：成功 → 短时 toast「预订成功」；失败 → `alert` 或统一 toast 展示**原文**；可选保留 `run_metric` 供排障，但不作为主文案。

### 4.1 `verify_pending` 与半自动能力如何处理

| 方案 | 做法 |
|------|------|
| **A（推荐默认）** | 手动路径 **删除或绕过** `verify_pending` 整段；若 campaign 仍返回 `verify_pending`，**原样**交给前端，按失败或「待确认」展示原文，**不**在 `api_book` 内拉矩阵/补订。 |
| **B（兼容）** | 增加配置如 `manual_deep_reconcile_enabled`（默认 `false`），为 `true` 时恢复旧行为，供极少数运维场景。 |

产品默认对齐 **A**；若选 **B**，须在配置页标注「非默认手动产品」。

### 4.2 「原文」字段约定

- 优先：`server_msg_raw`（或最后一次 POST 的 `raw_message`）。  
- 兜底：`msg`、`business_fail_msg`、`normalized_msg`（分类器）。  
- 设计响应 JSON：`error_detail` 或 `gym_message_raw`（二选一命名，前后端统一），避免前端只能拿到被改写后的 `msg`。

---

## 5. 自动任务：目标步骤与「深度核对」说明

1. **启动**：定时/触发 → `submit_delivery_campaign`（`skip_warmup=False`）。
2. **循环**：`get_matrix` → 首组（若配置从矩阵算）→ 主单分批 POST → **按矩阵（+ 本会话已 accept 格）算 `need_by_time`** → refill 直至 `goal_satisfied` 或预算/终局失败。

**「深度核对」在此处的含义**：相对手动，自动的「成功」依赖 **目标块数/缺口** 与 **矩阵 mine 状态**（及订单类辅助若存在），**不是**单笔 POST 即结束。

### 5.1 可选优化（优先级：中低）

**若本轮 POST 累计 `stop_success` 的格数已覆盖当前 `need_by_time`（与会话 `campaign_accepted_cells` 一致）**，可在**不拉矩阵**的情况下判定本段目标已满足并进入终局或下一轮逻辑。

- **收益**：省矩阵请求、略降尾延迟。  
- **风险**：mine 展示极端滞后时与矩阵不一致；可通过「仅当分类器已明确 stop_success 且计数闭合」限制使用场景。  
- **建议**：作为第二阶段或配置开关 `delivery_skip_matrix_when_need_satisfied_by_accept`（默认关或开需压测）。

---

## 6. `submit_delivery_campaign` 共性修补（手动 + 自动均受益）

**问题**：首单 `_sequential_post_all_batches(..., stale_check=_plan_is_stale)` 在**每次**软重试前检查保鲜；当 `plan_max_age`（如 8s）< `min_post_interval × 重试次数` 时，必然频繁 `PLAN_STALE`。

**设计选项（择一或组合）**：

| 选项 | 描述 |
|------|------|
| **R1** | 同批软重试循环内 **不**调用 `stale_check`，仅在批间或 `primary_done` 后进入 refill 前检查。 |
| **R2** | `skip_warmup=True` 且 `delivery_first_group_from_matrix=False` 时，首单 **不传** `stale_check` 或使用极大 `plan_max_age`（仅手动）。 |
| **R3** | 配置 `delivery_plan_max_age_apply_during_primary_soft_retries`（默认 `false`）。 |

推荐 **R1** 或 **R3**，避免手动/自动行为分叉过大。

---

## 7. 配置与清理（与前期「废弃」项合并）

### 7.1 已从逻辑废弃（可删键或仅兼容读取）

- `delivery_warmup_max_retries`、`delivery_warmup_budget_seconds`

### 7.2 手动深度核对相关（默认关闭后可选删除或归档）

- `manual_verify_pending_recheck_times`、`manual_verify_pending_retry_seconds`、`manual_auto_refill_enabled`、`manual_verify_pending_orders_fallback_enabled`  
- 若采用方案 B，保留并文档说明；若纯 A，可标 deprecated。

### 7.3 建议在前端「系统配置」暴露的核心递送参数（与自动强相关）

- `delivery_total_budget_seconds`、`delivery_plan_max_age_seconds`、`delivery_min_post_interval_seconds`  
- `delivery_transport_round_interval_seconds`、`delivery_refill_matrix_poll_seconds`、`delivery_retry_jitter_ms`  
- `delivery_refill_no_candidate_streak_limit`、`matrix_timeout_seconds`、`submit_timeout_seconds`  
- `max_items_per_batch`、`max_consecutive_slots_per_place`、`delivery_submit_granularity`  
- fieldinfo / 求解相关键（与现有 `index.html` 表单对齐）

### 7.4 可删除的无引用函数（实施时全局再 grep）

- `matrix_booking_open_by_no_locked_cells`（已无调用）  
- `seconds_until_today_open_time_cn`（已无调用）

---

## 8. 分阶段任务清单（实施顺序建议）

| 阶段 | 内容 | 依赖 |
|------|------|------|
| **P0** | 修补 `stale_check` 与 `plan_max_age`、软重试的交互（§6） | 无 |
| **P1** | `api_book`：默认关闭 `verify_pending` 整段；响应增加原文字段（§4.1–4.2） | P0 可选并行 |
| **P2** | 前端：`submitOrder` 成功 toast「预订成功」、失败展示原文；梳理 `submit_mode`/`profile` 命名（§2.2） | P1 |
| **P3** | 配置清理 + 管理页字段（§7） | P1 后 |
| **P4** | 死代码删除 + 文档/README 一句产品分界线 | 任意 |
| **P5** | 自动「accept 已闭合缺口则跳过矩阵」（§5.1） | 低优先级 |

---

## 9. 验收要点（手测）

1. **手动**：选格 → 下单 → 成功仅短时「预订成功」；失败弹窗为馆方原文；**无**默认二次矩阵轮询与补订。  
2. **手动**：`min_post_interval=5`、`plan_max_age=8` 下多轮软重试 **不再**无意义刷屏「算场快照过期」。  
3. **自动**：现有抢场/refill 回归通过；指标与日志仍可读。  
4. **配置**：新装可不填 `delivery_warmup_*`；核心递送参数可在页面保存生效。

---

## 10. 文档维护

| 日期 | 变更 |
|------|------|
| 2026-04-04 | 初稿：手动三件套、自动目标导向、stale 修补、配置与分阶段任务 |

与 **[delivery-unified-loop-spec.md](./delivery-unified-loop-spec.md)** 的关系：该文档描述「单循环 + bookable 语义」；本文档在其上定义 **手动与自动的产品边界** 与 **api_book / 前端** 的落地范围。
