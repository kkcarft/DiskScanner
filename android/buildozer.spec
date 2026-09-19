[app]

# 应用名 / 包名
title = 存储扫描
package.name = diskscanner
package.domain = org.ds.diskscanner

# 源码目录（相对于本文件所在目录）
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf
source.exclude_exts = spec
source.exclude_dirs = bin,build,.buildozer,__pycache__

# 版本号（格式必须是 x.y.z）
version = 0.1.0

# 依赖：python3 + kivy。核心逻辑 corescan.py 只用标准库，无需额外 recipe
requirements = python3,kivy

# 入口
# 注意：buildozer 固定以 main.py 作为入口，改名会导致打包失败
# （这也是本项目把主程序命名为 main.py 的唯一原因）

# 图标（可选，缺省用 Kivy 默认图标）
# icon.filename = %(source.dir)s/data/icon.png

# 屏幕方向：portrait=竖屏, landscape=横屏, sensor=跟随传感器
orientation = portrait

# 状态栏 / 全屏
fullscreen = 0

# 允许备份
allow_backup = True

android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE

# ---------------------------------------------------------------------------
# Android 构建参数
# ---------------------------------------------------------------------------
# api    = 编译目标 SDK。MANAGE_EXTERNAL_STORAGE 需要 30+，这里直接对齐最新
# minapi = 最低可安装版本。低于 30 的设备走旧的读写存储权限，代码里有分支处理
# arch   = arm64-v8a 覆盖几乎所有现役手机；需要覆盖老设备可再加 armeabi-v7a
#
# 注意：如果 CI 报 "Could not find android api 35" 之类错误，
#       多半是 python-for-android 版本太旧 —— 把下面 p4a.branch 打开即可。
android.api = 35
android.minapi = 23
android.arch = arm64-v8a
android.enable_androidx = True

# 用 p4a 主线（已启用）：稳定版 p4a 对 android.api 35 的支持可能滞后。
# 若构建报 "Could not find android api 35"，就是把 android.api 降到 33 再试。
p4a.branch = master

[buildozer]

# 日志级别：1=精简 2=详细。CI 首次构建建议用 2，报错时信息更全
log_level = 2

# 构建警告不算致命
warn_on_root = 1

# 构建缓存目录（CI 里会把这里缓存起来，避免每次都重新下载 SDK/NDK）
build_dir = ./.buildozer
