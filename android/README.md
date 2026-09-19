# 存储扫描 · Android 版

Windows 版 DiskScanner 的安卓移植。用 Kivy 实现界面，核心逻辑复用 `corescan.py`。

## 与 Windows 版的关系

```
scanner.py（Windows 版，2540 行）
   ├── 核心逻辑 ~900 行 ────► corescan.py   ← 程序化抽取，逐字节未改动
   │                                          （纯标准库，可在手机上直接跑）
   └── Qt 界面 ~1700 行 ──► 本目录 main.py  ← Kivy 重写
```

`corescan.py` 是从 `scanner.py` 按行区间整块抽取的，没有手工重写，因此
**扫描、体积聚合、筛选、排序、进度模型、删除的行为与 Windows 版一致**。

## 功能差异（按需求做了删减）

| 功能 | Windows 版 | Android 版 |
|---|---|---|
| 选择扫描目标 | 盘符下拉框（C:/ D:/） | 固定扫共享存储根目录 —— 安卓没有盘符 |
| 多选方式 | 左键拖拽拉选 + 右键加选 | 每行一个勾选框（触摸屏没有右键） |
| 删除方式 | 回收站 / 永久删除 二选一 | 直接删除 —— 安卓没有回收站 API |
| 打开所在位置 | explorer.exe | 已移除 —— 安卓没有资源管理器 |
| 复制完整路径 | 支持 | 支持（路径形如 `/storage/emulated/0/DCIM/...`） |
| 扫描 / 聚合 / 筛选 / 排序 / 进度 | 支持 | 支持，逻辑完全一致 |

**保留的安全设计**（安卓上更重要，因为没有回收站兜底）：
删除确认框、默认焦点在取消按钮、超过 50 项必须手动键入数量才解禁。

## 打包：GitHub Actions（本机零改动）

SDK / NDK / 编译全部发生在 GitHub 的 Linux runner 上，**你本机不会下载任何大文件**。

1. 把 `android/` 与 `.github/workflows/build-android.yml` 推到仓库
2. 仓库页面 → **Actions** → *Build Android APK* → **Run workflow**
3. 首次约 20–40 分钟（要下载并编译 SDK/NDK）；完成后在 Artifacts 里下载 APK

流程分两关：先跑 `test_core.py`（约 30 秒），通过后才开始打包。
核心逻辑有问题会在 30 秒内失败，不用干等半小时。

## 手机上使用

1. 安装 APK（用 `adb install` 或把 APK 传到手机上点开）
2. 首次打开会跳转到系统设置 —— 开启**「所有文件访问」**（设置 → 特殊应用权限）
3. 回到 App 点「开始扫描」

> 这个权限不能靠弹窗申请，必须由用户手动在系统设置里开，这是安卓的规定。

## 已知限制

- **看不到各 App 的私有数据**（`/data/data/...`）。那是手机占用的大头，
  但系统不允许第三方应用枚举 —— 有没有权限都不行。能扫到的只有共享存储：
  内部存储 `/storage/emulated/0` 和外置 SD 卡。
- 删除照片后会调用 `MediaScannerConnection.scanFile()` 通知系统媒体库刷新索引。
  不做这一步，文件删了但相册里仍会显示（点开提示不存在），可能要几小时才自愈。
- 若只打算自己装来用，不需要 Google Play 审核；要上架 Play 则「所有文件访问」
  需提交声明并获得批准（仅限文件管理器、备份、杀毒等用途）。

## 本地测试

```bash
cd android

# 核心逻辑（真实文件系统上跑，47 项断言）
python test_core.py

# 界面冒烟测试（会弹窗几秒：扫描 → 展开 → 勾选 → 筛选 → 删除）
python smoke_ui.py
```

桌面调试时可设环境变量指定扫描目标，避免扫整个盘：

```bash
CORESCAN_ROOT=/some/small/dir python smoke_ui.py
```
