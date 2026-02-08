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
- 请确保 `config.json` 中的 Token 和 Cookie 是最新的。
- 如果遇到 SSL 报错，`app.py` 中已配置自动跳过验证。
