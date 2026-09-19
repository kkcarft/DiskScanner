@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "APP_NAME=DiskScanner"
set "ENTRY=scanner.py"
set "PY="

echo ==============================================================
echo   磁盘扫描可视化 - 一键打包脚本
echo   产物: dist\%APP_NAME%.exe  (单文件 / 无控制台窗口)
echo ==============================================================
echo.

REM ---------------------------------------------------------------- 1. 定位 Python
echo [1/6] 检查 Python 环境 ...

where python >nul 2>&1
if %errorlevel%==0 (
    python -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
    if !errorlevel!==0 set "PY=python"
)

if not defined PY (
    where py >nul 2>&1
    if !errorlevel!==0 (
        py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
        if !errorlevel!==0 set "PY=py -3"
    )
)

if not defined PY (
    echo    [错误] 未找到 Python 3.8 及以上版本。
    echo.
    echo    请先安装 Python: https://www.python.org/downloads/windows/
    echo    安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo    已找到 Python !PYVER!  ^(命令: %PY%^)

REM ---------------------------------------------------------------- 2. 检查 pip
echo.
echo [2/6] 检查 pip ...
%PY% -m pip --version >nul 2>&1
if not !errorlevel!==0 (
    echo     未检测到 pip，尝试用 ensurepip 修复 ...
    %PY% -m ensurepip --upgrade >nul 2>&1
    %PY% -m pip --version >nul 2>&1
    if not !errorlevel!==0 (
        echo    [错误] pip 不可用，请手动执行: %PY% -m ensurepip --upgrade
        pause
        exit /b 1
    )
)
echo    pip 正常。

REM ---------------------------------------------------------------- 3. 安装依赖
echo.
echo [3/6] 检查并安装依赖 (PyQt6 / pyinstaller) ...

%PY% -c "import PyQt6" >nul 2>&1
if !errorlevel!==0 (
    echo    PyQt6 已安装。
) else (
    echo    正在安装 PyQt6 ...
    %PY% -m pip install PyQt6
    if not !errorlevel!==0 (
        echo    [错误] PyQt6 安装失败。若为网络问题，可换国内源，例如：
        echo           %PY% -m pip install PyQt6 -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

%PY% -c "import PyInstaller" >nul 2>&1
if !errorlevel!==0 (
    echo    pyinstaller 已安装。
) else (
    echo    正在安装 pyinstaller ...
    %PY% -m pip install pyinstaller
    if not !errorlevel!==0 (
        echo    [错误] pyinstaller 安装失败。若为网络问题，可换国内源，例如：
        echo           %PY% -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------------------- 4. 入口校验
echo.
echo [4/6] 校验源码 ...
if not exist "%ENTRY%" (
    echo    [错误] 当前目录下找不到 %ENTRY%
    echo    请把本脚本与 %ENTRY% 放在同一个文件夹内。
    pause
    exit /b 1
)
%PY% -m py_compile "%ENTRY%"
if not !errorlevel!==0 (
    echo    [错误] %ENTRY% 存在语法错误，已终止打包。
    pause
    exit /b 1
)
echo    源码语法检查通过。

REM ---------------------------------------------------------------- 5. 打包
echo.
echo [5/6] 开始打包（单文件 + 无控制台窗口，首次约 1~3 分钟）...
echo.

if exist "build" rd /s /q "build"
if exist "dist"  rd /s /q "dist"
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

%PY% -m PyInstaller ^
    -F -w ^
    --name "%APP_NAME%" ^
    --clean --noconfirm ^
    --exclude-module tkinter ^
    --exclude-module unittest ^
    --exclude-module pydoc_data ^
    "%ENTRY%"

if not exist "dist\%APP_NAME%.exe" (
    echo.
    echo    [错误] 打包失败，请查看上面的 PyInstaller 日志。
    pause
    exit /b 1
)

REM ---------------------------------------------------------------- 6. 完成
echo.
echo [6/6] 打包完成
for %%f in ("dist\%APP_NAME%.exe") do set "SIZE=%%~zf"
set /a SIZEMB=!SIZE! / 1048576
echo ==============================================================
echo    产物路径 : %CD%\dist\%APP_NAME%.exe
echo    文件大小 : 约 !SIZEMB! MB
echo ==============================================================
echo.
echo    提示: 扫描 C 盘的系统目录需要管理员权限，
echo          右键 EXE -^> "以管理员身份运行" 可以少跳过一些目录。
echo.
echo    是否现在打开 dist 文件夹？(Y/N)
set /p OPENIT=
if /i "!OPENIT!"=="Y" start "" "%CD%\dist"

endlocal
pause
exit /b 0
