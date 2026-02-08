@echo off
chcp 65001 >nul 2>&1
rem 解决中文乱码，需确保脚本文件保存为UTF-8 (BOM) 编码

rem ====================== 定义路径 ======================
set "source=D:\个人\独立项目\Beijintick\"
set "update_file=%source%update.txt"
set "parent_target=D:\个人\独立项目\抢票系统\"

rem ====================== 1. 手动更新内容 ======================
if exist "%update_file%" (
    echo [提示] 正在打开更新日志：%update_file%
    echo [提示] 请在记事本中修改内容，保存并“关闭”记事本后，脚本将自动继续。
    
    rem 使用 /wait 参数，只有当你关闭记事本后，脚本才会继续往下走
    start /wait notepad.exe "%update_file%"
    
    echo [提示] 检测到记事本已关闭，准备开始复制...
) else (
    echo [警告] 未找到更新文件：%update_file%，跳过编辑步骤。
    pause
)

rem ====================== 2. 动态生成时间戳 ======================
rem 调用 PowerShell 格式化日期
for /f %%a in ('powershell -command "Get-Date -Format 'yyyyMMdd_HHmm'"') do set "timestamp=%%a"

set "target=%parent_target%Beijintick(%timestamp%)\"


rem ====================== 3. 确保父路径存在 ======================
if not exist "%parent_target%" (
    md "%parent_target%"
)
md "%target%" 2>nul

rem ====================== 4. 执行复制 ======================
echo 正在复制文件，请稍候...
xcopy "%source%" "%target%" /E /H /Y /C /I

rem ====================== 执行反馈 ======================
if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo 复制成功！
    echo 时间戳：%timestamp%
    echo 目标路径：%target%
    echo ============================================
) else (
    echo.
    echo 复制失败！错误码：%errorlevel%
    pause
)
pause