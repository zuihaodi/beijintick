# 极速递送：单循环矩阵算场（1/6 等价可订）— 需求、方案与任务明细

> 版本：v0.2  
> 状态：已实现（见 `app.py` 单循环递送 + `is_matrix_cell_bookable_for_new_booking`）

---

## 一、需求说明

### 1.1 背景与问题

当前 `submit_delivery_campaign` 存在相对独立的 **warmup（拉活）** 阶段：通过 `matrix_booking_open_by_no_locked_cells` 等逻辑判断「是否开约」，未开约时睡眠或高频重试拉矩阵，再进入统一递送循环。业务上已确认：**开约后矩阵不再出现 `locked`（状态 6）**；锁定期格子多为 6，与可订格在「生成下单 payload」阶段可统一对待。

### 1.2 目标

- **单一业务主线**：从任务开始到结束，始终执行同一套逻辑——**拉矩阵 → 按规则从矩阵算场（含首组与后续缺口）→ 合法分批 → POST**，循环直至成功、失败终局或预算耗尽。
- **取消「开约」作为显式分支**：不再依赖「矩阵中是否存在 locked」或等价信号来切换流程；不在代码中维护独立的「等开约」状态机（可保留由 **POST 返回**、**调度启动时间**、**总预算** 体现的客观约束）。
- **规划语义（可订格）**：在参与「新抢场地」的求解时，**状态 1（available）与 6（locked）视为等价——均可作为可尝试预订的格子**（基于约定：开约后不再出现 6；6 仅作为锁定期占位）。
- **非可订语义（保持不变）**：
  - **4（booked）**：他人已占，**不得**作为新订目标进入 payload。
  - **2（mine）**：本人已占，用于**缺口/目标达成**统计，**不得**再作为「抢新空位」目标重复提交（除非产品另行定义幂等补单，本需求默认排除）。

### 1.3 范围

- 自动任务触发的 **delivery campaign**（`submit_delivery_campaign` 主路径）。
- 与 **首组从矩阵计算**（`compute_first_group_from_matrix` / `solve_candidate_from_matrix` 及同类逻辑）、**refill 求解**、**矩阵状态映射**相关的规划与循环。
- 配置项、运行指标、测试用例的配套调整。

### 1.4 非目标（本需求不做的）

- 不改变馆方接口协议与 HTTP 路径。
- 不承诺「未到点 POST 一定成功」；仅通过**同一循环**内的重试、退避与分类器处理失败。
- 不在本文档中规定具体 Cron；由运维/任务启动时间负责。

### 1.5 假设与约束（需产品/运维确认）

| 假设 | 若失效时的影响 |
|------|----------------|
| 开约后矩阵中不再出现状态 6 | 若仍出现 6，求解会把 6 当可订格，可能增加无效 POST；依赖重拉矩阵与业务分类收敛 |
| 状态 4 表示确定不可再抢 | 若语义变化，需调整「禁止选 4」规则 |
| 现有 `_classify_delivery_response` 对「未到点/频控」等可继续递送 | 若分类过严导致早退，需单独调分类而非恢复 warmup |

### 1.6 验收标准（建议）

1. **无独立 warmup 门禁**：自动递送路径中不存在「仅因矩阵含 locked 而阻塞进入主循环」的逻辑；`matrix_booking_open_by_no_locked_cells` 不再用于控制是否开始 POST（可删除调用或仅保留作日志/指标采样）。
2. **求解可订集合**：首组、refill 等与「从矩阵选空位」相关的求解，在选格时将 **1 与 6** 同时视为可选（名称上可实现为统一的 `bookable` 判定函数，避免散落硬编码 `== "available"`）。
3. **禁止 4、区分 2**：新订 payload 不包含 `booked` 格；`mine` 参与已占统计，不重复抢同格（与当前「目标块数 / need_by_time」语义一致）。
4. **单循环可观测**：`run_metric` 仍能区分矩阵拉取次数、POST 次数、终止原因；可选增加「是否曾见状态 6」等采样字段便于验证假设。
5. **回归**：现有与递送、分批、refill 相关的自动化测试更新通过；手动场景：锁定期全 6 仍能生成首组并在开约后收敛。

---

## 二、技术方案

### 2.1 现状摘要（实现锚点）

- `submit_delivery_campaign`：`skip_warmup=False` 时先跑 **warmup** `while`（`get_matrix` + `matrix_booking_open_by_no_locked_cells` + 未开约 `sleep`），通过后 `mx_work` 进入主 `while`。
- `compute_first_group_from_matrix` 文档写明仅 `available` 可订；`solve_candidate_from_matrix` 及散号等路径多处 `== "available"`。
- `map_slot_state_int` 仍将若干整型映射为 `locked`；本方案**不删除**原始枚举，仅在**求解入口**将 `locked` 与 `available` 一并纳入可订判定（或先归一化为内部 `bookable`）。

### 2.2 目标架构

```
任务启动
  └─► while 未超时且未终局:
        拉矩阵 (get_matrix, bypass_cache 等沿用现策略)
        可选: 计划保鲜 (delivery_plan_max_age_seconds) 触发重拉 — 不变
        首组/缺口: 用「bookable = available ∪ locked」从矩阵求解 → 合法分批
        顺序 POST → 分类器 → 成功/继续/硬失败
```

- **不再**存在前置阶段：「直到 booking_open 才允许第一次进入上述循环」。
- **「开约」**不作为代码分支；若任务早于馆方接受时间启动，由 **POST 业务返回 + 间隔重试** 消化。

### 2.3 状态语义（求解层）

| 原始状态 | 求解：新抢目标 | 说明 |
|----------|----------------|------|
| 1 available | 可选 | |
| 6 locked | 可选 | 与 1 等价；依赖「开约后无 6」 |
| 4 booked | 不可选 | |
| 2 mine | 不可选为「新抢」；计入已占/缺口 | |

建议在实现上增加集中函数，例如语义层：`is_matrix_cell_bookable_for_new_booking(state_str) -> bool`，避免多处字符串比较分叉。

### 2.4 与 `skip_warmup`（手动预订）的关系

- **手动路径**仍可跳过「任务启动时的首次矩阵」假设；但若内部仍存在「仅 available」的求解，应与自动路径**统一 bookable 规则**，避免两套语义。
- 若移除 warmup 后 `skip_warmup` 仅表示「不预先拉矩阵」，需在文档与 UI 提示中写清：手动模式需保证会话有效且矩阵已刷新。

### 2.5 配置项演进

- `delivery_warmup_max_retries`、`delivery_warmup_budget_seconds`：**拟废弃**或改为仅影响「首帧前可选的短重试」（若完全删除阶段，可标记 deprecated，读配置时忽略并打日志，避免老配置报错）。
- `config.example.json`、持久化 CONFIG 校验（`_validate_config` 等）：删除或放宽对 warmup 必填校验。
- 前端/管理 API 若暴露 warmup 字段：标注废弃或隐藏。

### 2.6 指标与日志

- `warmup_attempts_total`、`warmup_success_at_ms` 等：改为 **campaign 矩阵阶段** 统一计数，或重命名为 `campaign_matrix_fetch_*`，避免语义残留。
- 日志文案去掉「未开约 sleep 至 lastDayOpenTime」等与「开约分支」强绑定的描述，改为「递送循环 get_matrix / POST」一致前缀。

### 2.7 风险与缓解

| 风险 | 缓解 |
|------|------|
| 馆方变更，开约后仍返回 6 | run 指标采样「矩阵含 6」次数；超阈值告警或配置开关收紧 bookable |
| 过早 POST 增多 | 依赖任务启动时间；必要时保留可选「最早 POST 墙钟」配置（非本需求必选） |
| 全 6 快照陈旧 | 保留 `delivery_plan_max_age_seconds`，超时重拉后再算 |

### 2.8 回滚策略

- Git 回退到保留 warmup 的版本；或增加 feature flag（若实现时引入）默认走新逻辑。

---

## 三、任务明细

以下为可勾选实施清单；**建议顺序**自上而下。

### 3.1 设计与文档

- [ ] **T1**：在本文档「验收标准」上与负责人签字/评论确认（假设 开约后无 6）。
- [ ] **T2**：若对外有 README/运维说明，增补「单循环、无开约门禁、建议任务启动时间」一节（可选，避免与 README 其它章节重复则链到本文档）。

### 3.2 核心逻辑（`web_booker/app.py`）

- [ ] **T3**：移除或掏空 `submit_delivery_campaign` 中 **warmup `while`**，使默认路径与现「递送循环」入口一致；合并 `mx_work` 初始化逻辑（首轮即 `get_matrix`）。
- [ ] **T4**：删除（或降级为仅日志）对 `matrix_booking_open_by_no_locked_cells` 的**流程控制**调用；全局检索是否另有「开约」分支一并清理。
- [ ] **T5**：实现集中 **`bookable` 判定**（`available` 与 `locked` 为 True；`booked`/`mine` 按第二节表）。
- [ ] **T6**：替换 `solve_candidate_from_matrix` 及其内部所有「选格」路径中的 `== "available"` 为 **bookable 判定**（含 `specs_by_s`、散号 `scatter_pick_items_with_time_streak_bias` 等 grep 到的位置）。
- [ ] **T7**：更新 `compute_first_group_from_matrix` 的文档字符串与行为，与 bookable 语义一致。
- [ ] **T8**：审阅 **refill / need_by_time / monotone** 路径，确保凡从矩阵选「新空位」处均用 bookable，且 **mine/booked** 语义与现 `is_goal_satisfied` 一致。
- [ ] **T9**：`skip_warmup` 分支与主路径在「求解语义」上对齐；更新函数 docstring（`submit_delivery_campaign` 顶部说明）。

### 3.3 配置与 API

- [ ] **T10**：`config.example.json`：标注或移除 `delivery_warmup_*`；`EXEC_PARAM_LIMITS` / 校验列表同步。
- [ ] **T11**：配置加载与保存 API（如 `update_config`）：兼容旧 JSON 中仍含 warmup 键（忽略或迁移日志）。
- [ ] **T12**：若 `templates/index.html` 或静态配置页展示 warmup，更新文案或隐藏项。

### 3.4 指标与可观测性

- [ ] **T13**：调整 `run_metric`：warmup 相关字段改名或合并为「campaign 矩阵拉取」；`phase`/`phase_events` 中 `warmup` 改为 `delivery_loop` 或等价。
- [ ] **T14**（可选）：增加 `matrix_saw_locked_cell` 布尔或计数，用于验证「开约后无 6」假设。

### 3.5 测试

- [ ] **T15**：`web_booker/tests/`：新增或更新用例——矩阵全 `locked` 时首组求解仍能产出 items；矩阵 `booked` 不参与新抢；`mine` 减少缺口。
- [ ] **T16**：若有针对 `matrix_booking_open_by_no_locked_cells` 或 warmup 行为的测试，删除或改写为「单循环」行为。
- [ ] **T17**：手动或集成探针：锁定期启动任务 → 观察日志无「等开约」分支、POST 在开约后收敛（与现网约定一致）。

### 3.6 发布与清理

- [ ] **T18**：CHANGELOG / 提交说明中写明：破坏性变更（配置废弃）、运维注意（任务启动时间）。
- [ ] **T19**：合并后观察一周 `task_run_metrics` 或日志：无效 POST 比例、失败桶分布。

---

## 四、文档维护

| 日期 | 变更 |
|------|------|
| 2026-04-03 | 初稿：需求、方案、任务明细 |
