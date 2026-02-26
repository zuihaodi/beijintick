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
