# PR 冲突安全整合作战手册（beijintick）

> 适用场景：多个“未合并 PR”同时修改抢订关键链路（submit/verify/refill/scheduler/run-metrics/UI）时，如何在不放大并发风险的前提下安全合并。

## 1. 目标与边界

- **首要目标**：在解锁瞬间不降低抢订成功率，且不引入重试风暴。
- **硬门禁**：任何合并不得让 `submit -> verify` 关键链路 p99 明显劣化（建议阈值：不超过基线 +5%）。
- **变更原则**：先做“去重 + 冲突收敛 + 开关隔离”，再做功能叠加。

## 2. 小白可执行版（照着做即可）

下面是可以直接复制执行的 10 步，默认你要处理的 PR 是 `101 111 114 115 122`。

### Step 0：先确认当前工作区干净

```bash
git status --short
```

- 预期：没有输出（或者你明确知道哪些文件可丢弃）。
- 若有临时改动，先暂存：`git stash -u`。

### Step 1：切到主干并拉最新

```bash
git checkout main
git pull --ff-only
```

- 预期：`Already up to date.` 或 fast-forward 更新成功。

### Step 2：创建“只用于冲突整合”的分支

```bash
git checkout -b integration/conflict-resolve-$(date +%Y%m%d)
```

- 预期：看到 `Switched to a new branch ...`。

### Step 3：给当前稳定点打标签（回滚保险）

```bash
git tag pre-conflict-merge-$(date +%Y%m%d-%H%M)
```

### Step 4：生成 PR 重叠矩阵（先看冲突热区）

```bash
bash scripts/pr_diff_matrix.sh 101 111 114 115 122 | tee /tmp/pr-matrix.txt
```

- 重点看：`Overlap matrix`。
- 规则：两个 PR 的共享文件数 `>0`，就代表存在冲突风险。

### Step 5：先做“主线 PR”选择

建议把覆盖最全、最新的 PR 当主线（通常是 #122）。

执行前先看差异：

```bash
git diff --name-status main...pr-122
```

### Step 6：按能力域分批处理，不要一次全吞

建议分 3 批，每批都要可回滚：

1. 提交与确认链路（pipeline/submit/verify/order-query）
2. 补订与调度（refill scheduler/state sampler/run-metrics）
3. UI 与配置（index.html、配置项）

### Step 7：冲突解决顺序（从高风险到低风险）

1. 幂等与去重（防重复下单）
2. 重试策略（指数退避 + 抖动）
3. 并发配额（账号/场次隔离）
4. 结果确认与补偿
5. UI 文案与样式

### Step 8：每一批都要跑最小验证

```bash
python -m py_compile web_booker/app.py
python - <<'PY'
from jinja2 import Environment
from pathlib import Path
Environment().parse(Path('web_booker/templates/index.html').read_text(encoding='utf-8'))
print('template syntax ok')
PY
```

### Step 9：关键指标门禁（不过就不合）

至少确认以下指标未劣化：

- 抢订成功率（完整场地命中率）
- `submit->verify` p95/p99
- 重复提交率
- 重试次数分布（有无同步重试峰值）
- 5xx / 超时率

### Step 10：准备回滚开关与回退命令

新增策略必须有开关（默认兼容旧行为）：

- `enable_pipeline_submit_v2`
- `enable_refill_scheduler_v2`
- `enable_post_submit_verify_v2`

若线上异常，回退模板：

```bash
# 1) 先关开关（配置层）
# 2) 再回退代码（git层）
git reset --hard <stable_tag_or_commit>
```

## 3. 推荐流程（工程视角）

### Step A：建立集成分支并冻结合并入口

```bash
git fetch --all --prune
git checkout -b integration/conflict-resolve-$(date +%Y%m%d) origin/main
```

说明：所有冲突都先在集成分支解决，不直接在 `main` 或功能分支硬改。

### Step B：先做 PR 文件级重叠分析

使用脚本（见 `scripts/pr_diff_matrix.sh`）先统计“每个 PR 改了哪些文件 + 文件重叠矩阵”:

```bash
bash scripts/pr_diff_matrix.sh 101 111 114 115 122
```

输出解读：
- `overlap_count > 0` 表示两个 PR 对相同文件有修改，需要重点审查语义冲突。
- 优先标记以下目录为高风险：
  - `web_booker/app.py`（调度/并发/重试/下单路径）
  - `web_booker/templates/index.html`（UI 配置入口与操作路径）

### Step C：能力域分组，避免整条 PR 机械合并

把提交按能力域分组，而不是按 PR 号直接合并：

1. **提交与确认链路组**：pipeline / submit / verify / order-query
2. **补订与调度组**：refill scheduler / state sampler / run-metrics
3. **UI 与配置组**：表单项、开关项、默认值

建议策略：
- 选择最新且最完整实现作为“主线”（通常是最后一个 PR）。
- 其它 PR 只 `cherry-pick` 其“未覆盖增量提交”，避免重复逻辑叠加。

### Step D：冲突处理优先级

先处理高风险语义，再处理低风险展示层：

1. 幂等与去重（防重复下单）
2. 重试策略（指数退避 + 抖动，防同刻重试风暴）
3. 并发配额（账户/场次隔离）
4. 结果确认与补偿
5. UI 文案与样式

### Step E：必须加开关（可回滚）

新增策略必须支持开关，建议命名：
- `enable_pipeline_submit_v2`
- `enable_refill_scheduler_v2`
- `enable_post_submit_verify_v2`

要求：
- 默认值保持兼容旧行为。
- 出现异常可在 1 分钟内回落旧路径。

## 4. 最小验证清单（合并前）

### 4.1 功能正确性

- 单账户单任务：可下单、可查单、状态闭环。
- 多账户并发：不存在重复下单风暴。
- 配置读写：旧配置可兼容，新字段有默认值。

### 4.2 性能与稳定性

至少对“解锁窗口”做一次压测/回放，关注：

- 抢订成功率（完整场地命中率）
- `submit->verify` p95/p99
- 重复提交率
- 重试次数分布（是否出现峰值同步重试）
- 5xx/超时率

### 4.3 失败阈值（建议）

满足任一条件即禁止合并并触发回滚：

- 错误率 > 5% 且持续 60s
- p99 > 基线 1.3x 且持续 3 个采样周期
- 重复提交率 > 基线 2x

## 5. 常见问题（FAQ）

### Q1：脚本报 `remote 'origin' not found` 怎么办？

先执行：

```bash
git remote -v
git remote add origin <你的仓库地址>
git fetch origin --prune
```

再重跑矩阵脚本。

### Q2：为什么不建议按 PR 编号一个个直接 merge？

因为这类 PR 大多同时改 `app.py` 和 `index.html`，属于高重叠；文本冲突解决后仍可能保留语义冲突（例如重复重试、双路径下单）。

## 6. 实操建议（结合当前 PR 列表）

对于 #101 / #111 / #114 / #115 / #122 这类高重叠变更，优先执行：

1. 用脚本确认重叠矩阵。
2. 将 #122 作为候选主线（若覆盖度最高）。
3. 从其它 PR 提取“未覆盖能力点”的最小提交。
4. 在集成分支一次性跑完整回归后再发起最终合并 PR。

---

如果你要让我继续自动化下一步，我建议直接执行：

```bash
bash scripts/pr_diff_matrix.sh 101 111 114 115 122 > /tmp/pr-matrix.txt
```

然后按矩阵生成“保留/丢弃/重写”清单，再进入具体冲突解法。
