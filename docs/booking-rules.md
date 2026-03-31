# 预订规则文档（含示例）

## 目标
- 先保证提交合法，再尽量补齐目标时段缺口。
- 优先同场连续时段，其次邻接已订场地，最后再退到更分散方案。

## 规则 1：组场优先级（先选什么）
- 第一优先：同一场地连续时段（如 `15@20 + 15@21`）。
- 第二优先：邻接已订场地（与已订场地号距离最小）。
- 第三优先：同一时段连续场地号（如 `15@20 + 16@20`）。
- 退阶：以上都不可得时允许单格补位。

示例：
- 目标：`20:00` 与 `21:00` 各需要 2 块。
- 候选 A：`15@20, 15@21, 16@20, 16@21`（优先）
- 候选 B：`9@21, 10@21, 11@21`（只覆盖一个时段，不优先）

## 规则 2：合法分批（怎么拆批）
- `max_places_per_timeslot`：同一批内，同一时刻允许的**不同场地号**个数，唯一来源是账号配置 `accounts[].delivery_max_places_per_timeslot`（1-6，必填）。
- 默认 `delivery_submit_granularity=per_legal_batch`：同场连续时段可同批。
- 可选 `delivery_submit_granularity=single_cell`：每个 POST 仅 1 个 `(place,time)`，更稳但更慢。

示例：
- 输入 items：`15@20, 15@21, 16@20, 16@21`（2×2）
- `per_legal_batch` 且 `max_places_per_timeslot=2`：常见为一批 `[15@20, 15@21, 16@20, 16@21]`（与官网多选一次提交一致）。
- 若 `max_places_per_timeslot=1`：会拆成两批（如先 15 两场再 16 两场），第二笔易触发「同时段最多 1 块」类馆方规则。
- `single_cell`：4 批单发。

## 规则 3：同轮发送（先发完再判断）
- 一轮内按顺序发送全部合法批次，批间隔 >= `delivery_min_post_interval_seconds`。
- 不因单个软失败中断后续批次。
- 一轮全部发送后，再判断是否满额。

示例：
- 批 1 `stop_success`，批 2 `switch_backup`，批 3 仍继续发送。

## 规则 4：成功/失败判定
- 满额成功：每个目标时段 mine 数量 >= `target_blocks`。
- 硬失败立即结束：`auth_fail`、`booking_rule_terminal`。
- 软失败继续：`switch_backup`、`continue_delivery`、`min_backoff_continue`。

示例：
- `21:00` 已满，`20:00` 还缺 1 块：不结束，进入 refill。

## 规则 5：refill（未满就继续）
- 每轮先拉矩阵，再算 `need_by_time`，再求解并分批发送。
- refill 与首轮使用同一套分批和错误处理规则。
- 连续多轮无候选时按阈值早停。

示例：
- 缺口 `{'20:00': 1, '21:00': 0}` 持续 10 轮无可用格：早停并记录缺口。

## 一句话顺序
- 拉活 -> 按优先级组场 -> 合法分批并同轮发完 -> 判满 -> 未满则 refill 重复。

## fieldinfo 与官网一致（HTTP 封装）
- 提交体里 `fieldinfo` JSON：**同一日期、同一场地**下，**连续整点小时**在封装层合并为 **一条**记录：`startTime` 为段首、`endTime` 为最后一格结束时刻（与官方微信多选连续时段一致）。
- `oldMoney` / `newMoney` 为该段内各小时单价之和（与合并前逐格相加总价一致）；非正常解析的时段仍按单条退化逻辑处理。
- 账号字段 `delivery_max_places_per_timeslot` **仍只约束**客户端分批：同一钟点上、`items` 里允许几个**不同场地号**进入同一批 POST，**不是**对 `fieldinfo` 数组长度的命名定义。
