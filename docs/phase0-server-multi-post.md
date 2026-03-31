# Phase 0：同会话一次拉活后多批 POST 是否被服务器接受

## 目的

开约后在同一页面会话认知下，**不强制每笔 POST 前重拉矩阵**，连续发 2～3 个合法小批（间隔 ≥ `delivery_min_post_interval_seconds`），观察每笔是否被接受，或第二笔起是否出现会话/重复/规则类文案。

## 方法一：浏览器手工

1. 登录馆方预订页，选定日期，待矩阵开约。
2. 第一笔：同一场地两个连续时段（若页面允许同批多时段），例如场地 15 的 20:00、21:00，提交并**截图或复制**返回提示全文。
3. 等待至少与配置一致的间隔（建议 ≥3s），**不强制刷新矩阵**。
4. 第二笔：另一场地单时段，例如 16@20:00，记录返回文案。
5. 再间隔后第三笔：例如 17@21:00，记录返回文案。
6. 在下方「实测记录」表中填写每步返回原文。

## 方法二：探针脚本（与 `ApiClient` 行为一致）

在 `web_booker` 目录下：

```text
python tools/phase0_multi_post_probe.py --plan tools/phase0_example_plan.json --dry-run
```

仅执行一次 `get_matrix`（拉活认知），**不下单**。  
确认计划 JSON 中日期与场次可用后，真实多批 POST（会尝试下单）：

```text
python tools/phase0_multi_post_probe.py --plan my_plan.json --confirm-live-post
```

结果默认写入 `web_booker/logs/phase0_multi_post_<UTC时间>.json`，含每批 `raw_message`、`classified_action` 等。

## 结论怎么用

- 若多批均可接受（或探针全 `stop_success`）：顺序多批 + refill 仍适用；账号 `delivery_max_places_per_timeslot=2` 时尽量一锅出 2×2，减少第二笔触发「同时段限额」。旧卡可在对应账号设回 `1`。
- 若第二笔起报会话/登录/重复预约等：需偏向「每 POST 前拉矩阵」或更保守的 `delivery_submit_granularity`。

## 实测记录（返回文案归档）

| 步骤 | 批次内容 | 返回文案（原文） | 备注 |
|------|-----------|------------------|------|
| 1 | （例：15@20 + 15@21） | | |
| 2 | （例：16@20） | | |
| 3 | （例：17@21） | | |

**本次仓库侧自动化说明**：CI/助手环境未使用生产凭证执行真实多笔下订；请在本机配置 `config.json` / `config.secret.json` 后运行探针或手工完成上表。

## 附录：探针 dry-run 归档样例（一次矩阵拉活、无 POST）

- 命令：`python tools/phase0_multi_post_probe.py --plan tools/phase0_example_plan.json --dry-run`（在 `web_booker` 目录执行）
- 归档文件：`web_booker/logs/phase0_multi_post_20260330_061349.json`
- 摘要：`warmup_matrix.error` 为 `null`，`meta.date_booking_scope` 为 `unlocked`；`posts` 为空（未下单）。多批 POST 的**服务端返回文案**须在开约窗口使用 `--confirm-live-post` 或浏览器手工补充到上表。
