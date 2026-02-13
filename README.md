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
    - 将 `web_booker/config.example.json` 复制为 `web_booker/config.local.json`。
    - 填入你自己的 Token（必填）以及 Cookie（可选，建议保留）。
    - 运行时优先读取 `web_booker/config.local.json`，避免被 `git pull` 覆盖。

4.  **准备任务文件（可选）**
    - 将 `web_booker/tasks.example.json` 复制为 `web_booker/tasks.local.json`。
    - 运行时优先读取 `web_booker/tasks.local.json`，避免任务配置被 `git pull` 覆盖。

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
- 请确保 `web_booker/config.local.json` 中 Token 是最新有效值；Cookie 可选。
- 本地运行数据使用 `*.local.json`（`config.local.json` / `tasks.local.json`），可避免拉取代码时覆盖。
- 不要在仓库里提交真实的 Token、Cookie、手机号或短信 API Key。
- 如果遇到 SSL 报错，`app.py` 中已配置自动跳过验证。
