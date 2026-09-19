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
# api    = 编译目标 SDK。用 33：这是 python-for-android 官方 RECOMMENDED_TARGET_API，
#          比追最新更稳。MANAGE_EXTERNAL_STORAGE 只需要 30+，33 完全够。
# minapi = 最低可安装版本（= Android 7.0）。
#          【踩过的坑】这个值不能低于 24。p4a 的 python3 recipe 装的是 CPython 3.14，
#          其 remote_debugging.c 用到 preadv / pwritev，而 NDK 的 API 23 sysroot
#          里没有这两个函数，编译会直接报
#          "call to undeclared function 'preadv'"。24 起才有。
# arch   = arm64-v8a 覆盖几乎所有现役手机；需要覆盖老设备可再加 armeabi-v7a
android.api = 33
android.minapi = 24
android.arch = arm64-v8a
android.enable_androidx = True

# 【重要】buildozer 不用 pip 里装的 p4a，它会自己 git clone 一份再 checkout 这个分支。
# 不写的话默认 master —— 那会拉到 CPython 3.14 + NDK r28c 的激进组合，构建会挂。
# 这里钉到 v2024.01.21（= Python 3.11.5 + Kivy 2.3.0 + NDK r25b，社区验证最充分的组合）
p4a.branch = v2024.01.21

[buildozer]

# 日志级别：1=精简 2=详细。CI 首次构建建议用 2，报错时信息更全
log_level = 2

# 构建警告不算致命
warn_on_root = 1

# 构建缓存目录（CI 里会把这里缓存起来，避免每次都重新下载 SDK/NDK）
build_dir = ./.buildozer
