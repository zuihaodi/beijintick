# 羽毛球场地抢票助手

这是一个用于自动抢定羽毛球场地的工具集合。

## 迁移与安装说明

如果你将本项目复制到了新的电脑上，请按照以下步骤配置环境：

1.  **安装 Python**
    - 请确保电脑上安装了 Python 3.8 或以上版本。

2.  **安装依赖库**
    - 打开终端（Terminal 或 CMD），进入本项目文件夹。
    - 运行以下命令安装所需库：
      ```bash
      pip install -r requirements.txt
      ```

3.  **准备配置文件（必做）**
    - 将 `web_booker/config.example.json` 复制为 `web_booker/config.json`。
    - 填入你自己的 Token（必填）以及 Cookie（可选，建议保留）。
    - 运行时读取 `web_booker/config.json`。

4.  **准备任务文件（可选）**
    - 将 `web_booker/tasks.example.json` 复制为 `web_booker/tasks.json`。
    - 运行时读取 `web_booker/tasks.json`。

## 如何运行

本项目包含多个脚本，主要使用 Web 界面版：

### Web 界面版 (推荐)
启动 Web 服务后，可以在浏览器中图形化操作。
```bash
python web_booker/app.py
```
启动后访问：http://127.0.0.1:5000

### 命令行工具
- **自动抢票脚本**: `python auto_badminton.py`
- **智能策略脚本**: `python smart_booker.py`
- **分步向导脚本**: `python step_by_step_booker.py`

## 注意事项
- 健康检查当前主要验证“查询链路”（能否获取场地状态）；查询正常不等于下单一定成功。
- 下单失败除了 Token/Cookie 外，还可能受风控、并发抢占、时间窗口、参数配置影响。
- 抢票参数较多时，建议先用一组“稳妥起点”：`retry_interval=1.0`、`aggressive_retry_interval=1.0`、`batch_retry_times=2`、`batch_retry_interval=0.5`、`locked_retry_interval=0.5~1.0`、`locked_max_seconds=60`、`open_retry_seconds=20`，再根据实际成功率微调。
- 请确保 `web_booker/config.json` 中 Token 是最新有效值；Cookie 可选。
- 建议将 `web_booker/config.json` / `web_booker/tasks.json` 视为本地运行数据文件（默认已被 `.gitignore` 忽略）。
- 不要在仓库里提交真实的 Token、Cookie、手机号或短信 API Key。

- 新增状态采样接口 `GET /api/state-sampler`：按秒聚合场地 `state` 计数并给出 `recommended_locked_states` 建议（仅统计数量，不记录个人敏感数据）。
- 新增独立补订任务接口：`/api/refill-tasks`（GET/POST/DELETE）与 `/api/refill-tasks/<id>/run`（POST）。补订任务会落盘到 `web_booker/refill_tasks.json`，服务重启后仍可继续。
- 任务中心已增加「🧩 独立 Refill 补订」前端入口，可直接创建/运行/删除补订任务（兼容手机端布局）。
- Refill 列表日期显示包含周几，便于按周周期排班。
- Refill 编辑弹层支持“保存并启动”，用于改完参数后立即恢复轮询。
- Refill 面板中「立即执行1轮」是一次性手动触发；持续补订依赖任务本身启用状态 + interval 自动轮询。
- 若点击后“看起来没反应”，优先查看任务条目中的“最近结果/最近执行时间”与后台 `[refill#任务ID|manual]` 日志。
- Refill 任务支持：编辑弹层、复制、启用/停用、截止时间（到点自动停用）、最近 10 次执行记录（环形保留）。
- 截止时间支持两种模式：固定时间（absolute）与开场前N小时（before_start）。
- Refill 每次补订成功（success/partial 且存在成功项）会发送通知（短信/PushPlus，按已有配置），并做同任务同分钟节流避免短信风暴。
- Refill 到达截止时间自动停用时也会发送通知，避免任务静默停止。
- `/api/logs` 支持按 `refill_id`、`status_kw` 与 `window_min` 组合过滤（示例：`/api/logs?refill_id=1772...&status_kw=success&window_min=15`）。
- `window_min` 过滤已兼容跨天边界（如 00:03 查询最近15分钟可包含前一日 23:5x 日志）。
- 运行复盘接口 `/api/run-metrics` 默认仅统计“锁定→解锁”样本（`unlock_only=1`），聚焦真正抢票窗口；仅在排查时再切换全样本。
- `/api/run-metrics` 的 `recommendation` 为“建议值，不自动写回配置”，并附带 `confidence/sample_size/min_sample_size` 便于判断建议可信度。
- 建议将“持续补齐”主要交给独立 Refill 任务；任务模式默认选中 Pipeline（推荐），同时保留普通稳定模式与智能连号作为兜底；其中 pipeline 内 refill 仅保留实验开关，默认不建议启用，以降低双路径并发干扰。
- 主任务增加“任务锁”：同一任务执行中再次触发会自动跳过并记录日志，避免重复并发抢占。
- Pipeline 新增轻量事件切换：连续多轮缺口未改善时可提前从 continuous 切换到 random。
- “达标后立即停止”建议开启：达到目标数量后立即结束，减少无效并发。
- 新增 `biz_fail_cooldown_seconds`：pipeline 中业务失败组合冷却秒数，短时间内优先避开业务失败组合、优先重放网络失败组合。
- 如果遇到 SSL 报错，`app.py` 中已配置自动跳过验证。


## 发布前自检（推荐）

建议每次改动 `web_booker/templates/index.html` 后先执行：

```bash
python - <<'PY'
from jinja2 import Environment
from pathlib import Path
Environment().parse(Path('web_booker/templates/index.html').read_text(encoding='utf-8'))
print('template syntax ok')
PY
```

这样可以在启动前尽早发现 `if/endif` 配对错误。

## 多 PR 冲突处理（推荐流程）

当存在多个未合并 PR 且改动重叠时，建议先在集成分支处理，不要直接在主分支逐个硬合。

**最简 5 步（可直接照做）**：

1. 检查工作区干净：`git status --short`
2. 创建集成分支：`git checkout -b integration/conflict-resolve-$(date +%Y%m%d) origin/main`
3. 生成重叠矩阵：`bash scripts/pr_diff_matrix.sh 101 111 114 115 122`
4. 以覆盖最全 PR 为主线（常见是 #122），其它 PR 仅摘取增量提交。
5. 每一批冲突解决后都执行语法检查与关键链路回归，再继续下一批。

详细作战手册（含预期输出、故障处理、回滚步骤）见：`docs/PR_CONFLICT_RESOLUTION_PLAYBOOK.md`。


## 抢订成功率提升计划（窗口 30-60 秒）

### 需求本质
在开票/解锁瞬间，用最短关键路径完成「完整场地（如 2 块 * 2 小时）」下单，并避免重试风暴导致的自我拥塞。

### 关键瓶颈假设
- 首批提交仍是串行批次循环，后批次天然晚于前批次。
- 提交路径内存在可前置的 JSON 编码/时间解析，抢票窗口内有 CPU 抖动。
- 遇到“操作过快”等可重试错误时，固定重试会造成同相位碰撞。
- 提交后立即高频矩阵/订单确认会挤占连接和服务端配额。

### 可量化目标（建议基线后 2 周内）
- 抢订窗口前 3 秒：首个 POST 发出时间（first_submit_ms）下降 40%+
- 抢订窗口前 10 秒：完整场地达成率提升 15%+
- 高峰期：提交链路 p99 下降 30%，可重试错误占比下降 20%

### 方案分层

#### 快速止血（本周，低风险）
1. 并发首发：首批分片并发发送，不再等待上一批返回。
2. 预计算请求体：将 fieldinfo/body 预构造放到解锁前预热阶段。
3. 验证降载：提交成功后采用“乐观成功 + 延后确认”，减少前 5 秒查询。
4. 重试加抖动：对“操作过快/限流”使用指数退避+抖动，避免同时重试。

#### 中期优化（1-2 周）
1. 多账号并发编排：账号隔离队列，避免单账号串扰。
2. 目标扩池：候选场地从 2 个扩到 6-8 个，先保“完整场地”成功率。
3. 参数自适应：根据最近 7 天错误画像动态调整 batch/timeout。

#### 长期架构（2-4 周）
1. 触发精度体系：NTP 偏移校准、连接池预热、定点发射。
2. 统一调度层：优先级队列 + 幂等键 + 熔断/降级策略。
3. 观测闭环：指标、日志、回放、A/B 试验平台化。

### 建议参数（先灰度）
```json
{
  "initial_submit_batch_size": 2,
  "batch_min_interval": 1.0,
  "submit_timeout_seconds": 1.5,
  "post_submit_skip_sync_orders_query": true,
  "preselect_enabled": true,
  "preselect_ttl_seconds": 5.0
}
```

### 风险与回滚
- 风险1：并发首发触发风控/封禁；
  - 失败信号：4xx/风控文案显著上升。
  - 回滚：关闭并发首发开关，退回串行+更大抖动。
- 风险2：乐观成功造成“假成功”；
  - 失败信号：成功回执与最终订单不一致率升高。
  - 回滚：恢复“成功后单次轻量确认”，禁用高频确认。
- 风险3：超时过短导致误杀慢成功；
  - 失败信号：timeout 比例高但最终成功率下降。
  - 回滚：timeout 从 1.5 回调至 2.0/2.2。

### 评估结论（必要性 / 可行性 / 优先级）
- A 并发首发：必要性高，可行性高，优先级 P0。
- B 预计算 Body：必要性中高，可行性高，优先级 P0。
- C 增大首批 batch：必要性高（若接口支持多场合单），可行性中高，优先级 P1。
- D 验证由重变轻：必要性高，可行性高，优先级 P0。
- E “2块2小时”专项配置：必要性高，可行性高，优先级 P0（配置先行）。

### 实施清单（待确认后执行）
1. 增加 `concurrent_initial_submit_enabled` 与线程池并发首发（可开关）。
2. 增加提交体预构造缓存（按日期+组合键）。
3. 增加 `post_submit_verify_delay_seconds`，将确认后置。
4. 新增指标：first_submit_ms / first_success_ms / retryable_fail_rate / final_complete_rate。
5. 小流量灰度（20%任务）+ 一键回滚配置。
