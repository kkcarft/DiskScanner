# -*- coding: utf-8 -*-
r"""
corescan —— 磁盘/存储扫描的核心逻辑（平台无关，纯标准库）
================================================================================
本文件由 scanner.py **程序化抽取**而来，逐字节搬运、未做改写，以保证与
Windows 版行为一致。被排除的部分：

  · 第 140 行 NODE_ROLE          —— 依赖 Qt
  · 第 837-976 行「文件操作层」   —— Windows 专有（回收站 / 资源管理器 /
                                     \\?\ 长路径前缀），已在下面的
                                     delete_permanently() 里用可移植写法重写
  · 第 1114 行之后的视图/模型/窗口 —— 全部 Qt，安卓版改用 Kivy 实现

可离线测试：本文件不 import 任何 GUI 库。
================================================================================
"""

from __future__ import annotations

import os
import queue
import threading
import time
import datetime
import shutil
import stat as stat_mod

# 一、可调参数
# ==============================================================================

# --- 颜色阈值 ---------------------------------------------------------------
DAYS_FRESH = 7            # <= 7 天     -> 绿
DAYS_MAX = 180            # <= 180 天   -> 黄；超过 -> 红

SIZE_MID = 1 * 1024 ** 3  # <  1GB       -> 绿
SIZE_BIG = 3 * 1024 ** 3  # <  3GB       -> 黄；>= 3GB -> 红

# --- 配色（按 bucket 名取色，便于统一调整） ---------------------------------
BUCKET_COLORS = {
    "fresh": "#1e8e3e",   # 绿：近期 / 小体积
    "mid":   "#b8860b",   # 黄：中期 / 中等体积
    "old":   "#d93025",   # 红：陈旧 / 大体积
}
DETAIL_COLOR = "#8a8a8a"  # 灰色小字

# --- 前段指标：决定"前段着色"与灰色详情第一个字段取哪个时间 -------------
# 说明：Windows 文件系统只记录"最后访问时间"，并不存在"访问次数"这种字段，
#       所以这里用访问时间来对应参考格式中的"访问"一栏。
METRIC_MTIME = "mtime"
METRIC_ATIME = "atime"
METRIC_CHOICES = [
    (METRIC_ATIME, "最后访问时间", "访问"),
    (METRIC_MTIME, "修改时间", "修改"),
]
METRIC_LABEL = {key: label for key, _name, label in METRIC_CHOICES}
DEFAULT_METRIC = METRIC_ATIME          # 默认按"最近打开时间"着色

# --- 行内样式 ---------------------------------------------------------------
# marks : 参考形状 —— [彩标]名字[彩标] (访问: … | 大小: …)
#         两个彩标只表示颜色档位，具体数值都在灰色括号里，不重复显示
# values: 彩段直接带数值 —— 2023-10-01 名字 2.5GB (访问: … | 大小: …)
ROW_STYLE_MARKS = "marks"
ROW_STYLE_VALUES = "values"
ROW_STYLE_CHOICES = [
    (ROW_STYLE_MARKS, "色标（参考形状）"),
    (ROW_STYLE_VALUES, "色段带数值"),
]
DEFAULT_ROW_STYLE = ROW_STYLE_MARKS
MARK_MIN_PX = 9                        # 色标最小边长（像素）

# --- 文件操作 ---------------------------------------------------------------
# 删除默认走「回收站」，可恢复；永久删除必须由用户在确认框里显式勾选。
BULK_DELETE_THRESHOLD = 50       # 超过这个数量，确认框里必须手动键入数量才能删
LONG_PATH_WARN = 240             # 路径长度超过此值给出长路径提示（Windows MAX_PATH=260）
SELECTION_LIST_LIMIT = 5000      # 多选窗口最多渲染多少行（超出仍可整体操作）
CLIPBOARD_SEP = "\n"             # 复制多个路径时的分隔符
MAX_REVEAL_DIRS = 3              # "打开所在位置"最多开几个资源管理器窗口

# --- 扫描进度估算 -----------------------------------------------------------
# 扫描前无法知道总条目数（要先数一遍就得遍历一遍，等于扫两次），所以枚举阶段
# 只能估算；聚合与排序阶段的总量是已知的，可以精确显示。
PHASE_ENUM_WEIGHT = 80.0         # 枚举阶段占整条进度条的份额（%）
PHASE_AGG_WEIGHT = 15.0          # 体积聚合
PHASE_SORT_WEIGHT = 5.0          # 排序
AVG_ENTRIES_PER_DIR = 8.0        # 先验：每个目录平均条目数
AVG_SUBDIR_RATIO = 0.35          # 先验：每个目录平均子目录数
AVG_BYTES_PER_DIR = 4 << 20      # 先验：每个目录平均字节数（仅子目录扫描的兜底估算用）
PROGRESS_EMA_ALPHA = 0.05        # 自校准的滑动平均系数
PROGRESS_TICK_MS = 120           # 进度刷新间隔（毫秒）
PROGRESS_ENUM_END = 80.0         # 枚举阶段在整条进度条上占 0~80
PROGRESS_AGG_END = 95.0          # 聚合 80~95，排序 95~100
PROGRESS_EST_CAP = 99.0          # 枚举阶段的百分比上限，避免过早宣称 100%

# --- 扫描行为 ---------------------------------------------------------------
SCAN_WORKERS = max(4, min(32, (os.cpu_count() or 4) * 4))  # 扫描线程数
MAX_NODES = 1_500_000      # 节点上限，防止极端情况吃满内存
SKIP_REPARSE = True        # 跳过符号链接 / 目录联接（避免死循环与重复统计）
AUTO_SCAN = True           # 启动后自动扫描下拉框里选中的磁盘
ROW_HEIGHT = 24            # 行高
H_GAP = 8                  # 行内各段之间的水平间距


# 二、纯逻辑层（不依赖 Qt，便于单独测试）
# ==============================================================================

def age_bucket(mtime: float, now: float | None = None) -> str:
    """按最后修改时间返回 'fresh' / 'mid' / 'old'"""
    now = time.time() if now is None else now
    age_days = (now - mtime) / 86400.0
    if age_days <= DAYS_FRESH:
        return "fresh"
    if age_days <= DAYS_MAX:
        return "mid"
    return "old"


def size_bucket(size: int) -> str:
    """按占用空间返回 'fresh'(小) / 'mid'(中) / 'old'(大)"""
    if size < SIZE_MID:
        return "fresh"
    if size < SIZE_BIG:
        return "mid"
    return "old"


def fmt_size(size: int) -> str:
    """人类可读的体积，例如 4.2GB / 0.8GB（无空格，保留一位小数）"""
    n = float(size)
    if n < 1024:
        return "%dB" % int(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        n /= 1024.0
        if n < 1024 or unit == "PB":
            return "%.1f%s" % (n, unit)
    return "%dB" % size


def fmt_date(mtime: float) -> str:
    """YYYY-MM-DD"""
    try:
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "未知日期"


def metric_ts(node, metric: str = METRIC_MTIME) -> float:
    """取该指标对应的时间戳。atime 不可用（为 0）时回退到 mtime。"""
    if metric == METRIC_ATIME and node.atime:
        return node.atime
    return node.mtime


def fmt_detail(node, metric: str = METRIC_MTIME) -> str:
    """名字之后的灰色小字，例如 (修改: 2023-10-01 | 大小: 2.5GB)"""
    return "(%s: %s | 大小: %s)" % (METRIC_LABEL.get(metric, "修改"),
                                    fmt_date(metric_ts(node, metric)),
                                    fmt_size(node.size))


# ==============================================================================
# 三、数据层：节点与扫描引擎
# ==============================================================================

class Node:
    """目录/文件节点。用 __slots__ 压缩内存，百万级节点也能扛。"""

    __slots__ = ("name", "is_dir", "mtime", "atime", "size",
                 "children", "parent", "row", "vlist", "vflag")

    def __init__(self, name: str, is_dir: bool, mtime: float = 0.0,
                 size: int = 0, parent: "Node | None" = None,
                 atime: float = 0.0):
        self.name = name
        self.is_dir = is_dir
        self.mtime = mtime        # 最后修改时间
        self.atime = atime        # 最后访问时间（NTFS 可能关闭更新，见 metric_ts）
        self.size = size          # 文件=自身大小；目录=聚合后的大小
        self.children = []        # 文件为空列表；目录扫描后填充
        self.parent = parent
        self.row = 0              # 在"当前生效的子节点列表"里的下标（供模型使用）
        self.vlist = None         # 筛选后的可见子节点列表；None 表示"全部可见"
        self.vflag = True         # 最近一次筛选后：该节点自身是否可见

    @property
    def path(self) -> str:
        """按需回溯拼出完整路径（扫描时不存路径，省内存）"""
        parts = []
        n = self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        if not parts:
            return n.name
        return os.path.join(n.name, *reversed(parts))


def _is_reparse_point(st) -> bool:
    """Windows 上的符号链接 / 目录联接（Junction）"""
    if os.name != "nt":
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    flag = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attrs & flag)


class ScanEngine:
    """
    多线程广度优先扫描。

    设计要点
      · 线程只做 os.scandir + stat —— 这期间 GIL 会释放，多线程才有意义
      · 每个目录的计数先在本地累加，结束时一次性加锁汇总，避免逐条争锁
      · 用 _outstanding 计数 + Condition 精确判断"所有任务都做完"，
        避免用轮询/队列 join 造成的死等
      · 遇到 PermissionError / OSError 一律计数后跳过，绝不让扫描中断
    """

    def __init__(self, max_nodes: int = MAX_NODES):
        self._lock = threading.Lock()
        self._q = queue.Queue()
        self._outstanding = 0
        self._finished = threading.Event()
        self._stop = threading.Event()

        self.max_nodes = int(max_nodes) if max_nodes else MAX_NODES
        self.dirs_scanned = 0
        self.files_found = 0
        self.skipped = 0
        self.truncated = False     # 触达节点上限而提前收工
        self.cancelled = False     # 用户手动停止
        self.error: str | None = None
        self.root: Node | None = None

        # ---- 进度相关（全部由扫描线程更新，GUI 侧只读快照）----
        self.bytes_found = 0               # 已发现文件的字节合计
        self.pending_dirs = 0              # 队列中待处理的目录数（BFS 前沿，精确可观测）
        self.current_dir = ""              # 最近处理完的目录
        self.phase = "enum"                # enum / aggregate / sort / done
        self.phase_done = 0
        self.phase_total = 0
        # 自校准经验值：随实测收敛，用于估算"还剩多少"
        self.entries_per_dir = AVG_ENTRIES_PER_DIR
        self.subdir_ratio = AVG_SUBDIR_RATIO
        self.bytes_per_dir = AVG_BYTES_PER_DIR
        self.started_at = time.time()
        self.finished_at = 0.0
        self.disk_total = 0                # 卷容量 / 已用（来自 disk_usage，是真实分母）
        self.disk_used = 0

    # ---------------------------------------------------------------- 对外接口
    def scan(self, root_path: str) -> Node | None:
        """阻塞式执行（请在后台线程里调用）"""
        try:
            abspath = os.path.abspath(root_path)
            if not os.path.isdir(abspath):
                self.error = "路径不存在或不是目录：%s" % abspath
                self._finished.set()
                return None
            root = Node(abspath, True, mtime=_safe_mtime(abspath))
        except Exception as e:                       # noqa: BLE001
            self.error = "无法访问 %s：%s" % (root_path, e)
            self._finished.set()
            return None

        self.root = root
        with self._lock:
            self._outstanding = 1
            self.pending_dirs = 1
        self._q.put((root, abspath))

        # 卷的已用字节是"进度"里唯一带有真实分母的量，拿来做交叉校验
        try:
            usage = shutil.disk_usage(abspath)
            self.disk_total, self.disk_used = usage.total, usage.used
        except OSError:
            self.disk_total = self.disk_used = 0

        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(SCAN_WORKERS)]
        for w in workers:
            w.start()

        self._finished.wait()
        for w in workers:
            w.join(timeout=2.0)

        # 注意：截断（truncated）也要聚合与排序，否则用户调大上限撞顶后
        #       会看到一棵体积全是 0 的残树。只有"用户主动取消"才跳过。
        if self.root is not None and not self.cancelled:
            nodes = self.dirs_scanned + self.files_found
            self.phase = "aggregate"
            with self._lock:
                self.phase_done = 0
                self.phase_total = self.dirs_scanned
            aggregate_sizes(self.root, progress=self._on_phase_progress,
                            total=self.dirs_scanned)
            self.phase = "sort"
            with self._lock:
                self.phase_done = 0
                self.phase_total = nodes
            sort_tree(self.root, DEFAULT_SORT, DEFAULT_METRIC,
                      progress=self._on_phase_progress, total=nodes)

        self.phase = "done"
        self.finished_at = time.time()
        return self.root

    # ---------------------------------------------------------------- 进度
    def _on_phase_progress(self, done: int, total: int):
        """聚合 / 排序阶段的进度回调（这两个阶段的总量是已知的，可以精确显示）"""
        with self._lock:
            self.phase_done = done
            if total:
                self.phase_total = total

    def snapshot(self) -> dict:
        """给 GUI 线程读的进度快照。所有字段都在锁内取，保证是一致的一组数。"""
        with self._lock:
            return {
                "dirs": self.dirs_scanned,
                "files": self.files_found,
                "skipped": self.skipped,
                "bytes": self.bytes_found,
                "pending": self.pending_dirs,
                "current": self.current_dir,
                "entries_per_dir": self.entries_per_dir,
                "subdir_ratio": self.subdir_ratio,
                "bytes_per_dir": self.bytes_per_dir,
                "phase": self.phase,
                "phase_done": self.phase_done,
                "phase_total": self.phase_total,
                "elapsed": time.time() - self.started_at,
                "disk_total": self.disk_total,
                "disk_used": self.disk_used,
                "truncated": self.truncated,
                "cancelled": self.cancelled,
                "running": not self._finished.is_set(),
            }

    def cancel(self):
        self.cancelled = True
        self._stop.set()
        self._finished.set()

    # ---------------------------------------------------------------- 内部实现
    def _worker(self):
        while not self._stop.is_set():
            try:
                node, path = self._q.get(timeout=0.15)
            except queue.Empty:
                with self._lock:
                    if self._outstanding == 0:
                        return
                continue
            try:
                self._scan_dir(node, path)
            except Exception:                        # noqa: BLE001
                with self._lock:
                    self.skipped += 1
            finally:
                with self._lock:
                    self._outstanding -= 1
                    done = self._outstanding == 0
                if done:
                    self._finished.set()

    def _scan_dir(self, node: Node, path: str):
        local_dirs = local_files = local_skip = 0
        local_bytes = 0
        pending: list[tuple[Node, str]] = []

        try:
            it = os.scandir(path)
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError) as e:
            with self._lock:
                self.skipped += 1
                # 根目录读不了要明确报错，子目录读不了按"跳过"处理即可
                if node is self.root:
                    self.error = "无法读取 %s：%s" % (path, e)
            return

        try:
            with it:
                for entry in it:
                    if self._stop.is_set():
                        break
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (PermissionError, OSError):
                        local_skip += 1
                        continue

                    if SKIP_REPARSE and _is_reparse_point(st):
                        local_skip += 1
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        child = Node(entry.name, True, st.st_mtime, 0, node,
                                     atime=st.st_atime)
                        node.children.append(child)
                        pending.append((child, os.path.join(path, entry.name)))
                        local_dirs += 1
                    elif entry.is_file(follow_symlinks=False):
                        child = Node(entry.name, False, st.st_mtime,
                                     st.st_size, node, atime=st.st_atime)
                        node.children.append(child)
                        local_files += 1
                        local_bytes += st.st_size
                    else:
                        local_skip += 1
        except (PermissionError, OSError):
            local_skip += 1

        with self._lock:
            self.dirs_scanned += 1
            self.files_found += local_files
            self.bytes_found += local_bytes
            self.skipped += local_skip
            self.current_dir = path

            # 自校准：用实测的"每目录条目数"和"每目录子目录数"修正先验，
            # 指数滑动平均让早期的极端值不至于把估算带偏
            n_entries = local_files + local_dirs
            if n_entries:
                a = PROGRESS_EMA_ALPHA
                self.entries_per_dir += a * (n_entries - self.entries_per_dir)
                self.subdir_ratio += a * (local_dirs - self.subdir_ratio)
            self.bytes_per_dir += PROGRESS_EMA_ALPHA * (local_bytes - self.bytes_per_dir)

            total_nodes = self.dirs_scanned + self.files_found
            if total_nodes > self.max_nodes:
                self.truncated = True
                self._stop.set()
            if not self._stop.is_set():
                self._outstanding += len(pending)
                for item in pending:
                    self._q.put(item)
            # 待处理目录数即 BFS 前沿长度，是进度估算里最关键的可观测量
            self.pending_dirs = self._outstanding


def _safe_mtime(path: str) -> float:
    try:
        return os.stat(path, follow_symlinks=False).st_mtime
    except (OSError, ValueError):
        return 0.0


# ==============================================================================
# 三·五、筛选与排序（纯逻辑层，不依赖 Qt，可离线测试）
# ==============================================================================

GB = 1024 ** 3


def visible_children(node: Node) -> list:
    """取当前生效的子节点列表。未做筛选时 vlist 为 None，直接用 children，
    因此"没有筛选"时是零额外开销、零额外内存的。"""
    return node.vlist if node.vlist is not None else node.children


class NodeFilter:
    """区间筛选条件。None 表示该侧不限。"""

    __slots__ = ("size_min", "size_max", "days_min", "days_max",
                 "metric", "now")

    def __init__(self, size_min=None, size_max=None, days_min=None,
                 days_max=None, metric: str = DEFAULT_METRIC, now=None):
        self.size_min = size_min      # 单位 GB
        self.size_max = size_max
        self.days_min = days_min      # 距今天数
        self.days_max = days_max
        self.metric = metric
        self.now = time.time() if now is None else now

    @property
    def is_active(self) -> bool:
        return any(v is not None for v in
                   (self.size_min, self.size_max, self.days_min, self.days_max))

    def matches(self, node: Node) -> bool:
        """目录用聚合体积判断（与显示值一致），文件用自身体积。
        判据全部提前返回，是热路径里最紧的一环。"""
        smin, smax = self.size_min, self.size_max
        if smin is not None or smax is not None:
            gb = node.size / GB
            if smin is not None and gb < smin:
                return False
            if smax is not None and gb > smax:
                return False
        dmin, dmax = self.days_min, self.days_max
        if dmin is not None or dmax is not None:
            days = (self.now - metric_ts(node, self.metric)) / 86400.0
            if days < 0.0:
                days = 0.0            # 时钟偏差导致未来时间时按"今天"处理
            if dmin is not None and days < dmin:
                return False
            if dmax is not None and days > dmax:
                return False
        return True


def apply_filter(root: Node, flt: "NodeFilter | None" = None,
                 keep_ancestors: bool = True):
    """
    就地计算可见性并建立可见子列表。返回 (可见文件数, 可见目录数, 隐藏节点数)。

    复杂度 O(N)，只走两遍线性遍历：
      第一遍 后序 DFS —— 判定自身是否匹配，并把"有可见后代"沿父链向上传播
      第二遍 前序 DFS —— 生成可见子列表，并回写 row 下标供模型索引使用

    内存友好：只有"确有子项被隐藏"的目录才会分配一个额外列表；
    没有任何隐藏项时 vlist 保持 None，退化成直接用 children。
    """
    match = flt.matches if flt is not None else None

    # ---- 第一遍：后序 DFS，标记 vflag ----
    stack = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if not processed:
            stack.append((node, True))
            for c in node.children:
                if c.is_dir:
                    stack.append((c, False))
                else:
                    c.vflag = True if match is None else match(c)
            continue

        if match is None or match(node):
            node.vflag = True
        elif keep_ancestors:
            v = False
            for c in node.children:
                if c.vflag:
                    v = True
                    break
            node.vflag = v
        else:
            node.vflag = False

    # ---- 第二遍：前序 DFS，生成可见列表 + 回写 row ----
    n_files = n_dirs = n_hidden = 0
    stack = [root]
    while stack:
        node = stack.pop()
        children = node.children
        if not children:
            node.vlist = None
            continue

        vis = [c for c in children if c.vflag]
        n_hidden += len(children) - len(vis)
        # 被隐藏的目录，其整棵子树都不会显示，要一并计入隐藏数
        for c in children:
            if c.vflag or not c.is_dir:
                continue
            sub = [c]
            while sub:
                x = sub.pop()
                for y in x.children:
                    n_hidden += 1
                    if y.is_dir:
                        sub.append(y)
        if len(vis) == len(children):
            node.vlist = None                    # 全部可见，不额外分配
            for c in children:
                if c.is_dir:
                    stack.append(c)
        else:
            node.vlist = vis
            for c in vis:
                if c.is_dir:
                    stack.append(c)

        for i, c in enumerate(vis):
            c.row = i
            if c.is_dir:
                n_dirs += 1
            else:
                n_files += 1

    return n_files, n_dirs, n_hidden


# --- 排序 -------------------------------------------------------------------
# 说明：全部使用 list.sort(key=...)。Timsort 对每个元素只计算一次 key，
#       而 functools.cmp_to_key 要在 Python 层执行 O(n log n) 次比较调用，
#       实测慢一个数量级（见 test_scanner.py 里的基准对比）。
SORT_MODES = [
    ("size_desc", "大小 ↓（大 → 小）"),
    ("size_asc",  "大小 ↑（小 → 大）"),
    ("name_asc",  "名称 A → Z"),
    ("name_desc", "名称 Z → A"),
    ("time_new",  "时间（新 → 旧）"),
    ("time_old",  "时间（旧 → 新）"),
    ("dir_first", "目录优先（同类按大小 ↓）"),
    ("none",      "扫描顺序（不排序）"),
]
SORT_LABEL = dict(SORT_MODES)
DEFAULT_SORT = "size_desc"
TIME_SORTS = {"time_new", "time_old"}      # 这两个依赖指标，指标变了要重排


def sort_spec(mode: str, metric: str = DEFAULT_METRIC):
    """返回 (key 函数, 是否逆序)。'none' 返回 None。"""
    if mode == "size_desc":
        return (lambda n: n.size), True
    if mode == "size_asc":
        return (lambda n: n.size), False
    if mode == "name_asc":
        return (lambda n: n.name.lower()), False
    if mode == "name_desc":
        return (lambda n: n.name.lower()), True
    if mode == "time_new":
        return (lambda n: metric_ts(n, metric)), True
    if mode == "time_old":
        return (lambda n: metric_ts(n, metric)), False
    if mode == "dir_first":
        return (lambda n: (0 if n.is_dir else 1, -n.size)), False
    return None


def sort_tree(root: Node, mode: str = DEFAULT_SORT,
              metric: str = DEFAULT_METRIC, progress=None,
              total: int = 0, step: int = 4096) -> bool:
    """对每个目录的子节点就地排序，返回是否真的排了（'none' 返回 False）。
    progress(done, total) 为可选进度回调。"""
    spec = sort_spec(mode, metric)
    if spec is None:
        return False
    key, rev = spec
    done = 0
    next_report = step
    stack = [root]
    while stack:
        node = stack.pop()
        children = node.children
        if len(children) > 1:
            children.sort(key=key, reverse=rev)
        # 排序后必须回写 row：模型的 parent() 靠它定位，
        # 否则单独调用本函数会留下"位置与下标不一致"的隐患
        for i, c in enumerate(children):
            c.row = i
            if c.is_dir:
                stack.append(c)
        done += 1 + len(children)
        if progress is not None and done >= next_report:
            progress(done, total)
            next_report = done + step
    if progress is not None:
        progress(done, total or done)
    return True


# ==============================================================================
# 三·六、文件操作层（复制路径 / 打开位置 / 删除）
#       纯逻辑 + 系统调用，不依赖 Qt，可离线测试
# ==============================================================================

def aggregate_sizes(root: Node, progress=None, total: int = 0, step: int = 2048):
    """
    迭代式后序遍历，自底向上把子节点体积汇总到目录。
    扫描结束时用一次；删除节点后需要重新统计，也复用这里。

    progress(done, total) 为可选进度回调，每处理 step 个目录回调一次。
    """
    done = 0
    next_report = step
    stack = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if not node.is_dir:
            continue
        if processed:
            t = 0
            for c in node.children:
                t += c.size
            node.size = t
            done += 1
            if progress is not None and done >= next_report:
                progress(done, total)
                next_report = done + step
        else:
            stack.append((node, True))
            for c in node.children:
                if c.is_dir:
                    stack.append((c, False))
    if progress is not None:
        progress(done, total or done)


def remove_nodes(nodes):
    """
    把给定节点从各自的父节点上摘掉（只改内存中的树，不动磁盘）。
    返回 (实际摘除数, 受影响的父节点集合)。

    实现要点：先把待删节点收进 id 集合，再对每个受影响的父节点做一次
    过滤重建，而不是逐个 list.remove()——后者在"从十万个子项里删一百个"
    这种场景下是 O(n·k)，而这里只有 O(n)。
    """
    remove_ids = {id(n) for n in nodes}
    parents = []
    seen = set()
    for n in nodes:
        p = n.parent
        if p is not None and id(p) not in seen:
            seen.add(id(p))
            parents.append(p)

    removed = 0
    for p in parents:
        before = len(p.children)
        p.children = [c for c in p.children if id(c) not in remove_ids]
        removed += before - len(p.children)
    return removed, parents


def node_paths(nodes) -> list:
    """把节点集合转成去重后的路径列表（保持首次出现顺序）"""
    seen = set()
    out = []
    for n in nodes:
        p = n.path
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def path_exists(path: str) -> bool:
    """用 lexists：断链的符号链接也算"存在"，避免把它误判成"已被删除\""""
    return os.path.lexists(path)


def split_existing(nodes):
    """把选中项分成 (仍存在, 已不存在)。
    已不存在的典型来源：删除后未刷新、或在软件外面被移走/删掉了。"""
    exists, missing = [], []
    for n in nodes:
        (exists if path_exists(n.path) else missing).append(n)
    return exists, missing


def subtree_stats(node):
    """删除预览用：返回 (将删除的文件数, 将删除的目录数, 合计字节数)。
    文件节点统计自身；目录节点统计其下所有后代（不含目录自身）。"""
    if not node.is_dir:
        return 1, 0, node.size
    files = dirs = 0
    total = 0
    stack = [node]
    while stack:
        n = stack.pop()
        for c in n.children:
            if c.is_dir:
                dirs += 1
                stack.append(c)
            else:
                files += 1
                total += c.size
    return files, dirs, total


def selection_summary(nodes):
    """给确认框/多选窗口用的汇总。返回 dict。"""
    n_files = n_dirs = 0
    n_children_files = n_children_dirs = 0
    total = 0
    longest = 0
    for n in nodes:
        if n.is_dir:
            n_dirs += 1
            f, d, sz = subtree_stats(n)
            n_children_files += f
            n_children_dirs += d
            total += sz
        else:
            n_files += 1
            total += n.size
        p = n.path
        if len(p) > longest:
            longest = len(p)
    return {
        "count": len(nodes),
        "files": n_files,
        "dirs": n_dirs,
        "child_files": n_children_files,
        "child_dirs": n_children_dirs,
        "bytes": total,
        "longest_path": longest,
    }


# 三·七、扫描进度模型（纯逻辑，可离线测试）
#
# 核心难点：扫描前无法知道总条目数（先数一遍就得遍历一遍，实测两遍法要
# 1.9~2.25 倍时间，不可接受）。而实测表明基于 BFS 前沿的估算在真实文件系统上
# 完全不可用——System32 下存在含上万子目录的目录，前沿会从 2 暴涨到 10725，
# 估算百分比随之从 99% 崩到 0.1%。
#
# 因此这里按"能否拿到真实分母"自适应选择算法：
#   · 扫描卷根（C:\ 这类）：卷的已用字节来自 disk_usage，是**真实分母**，
#     用「已发现字节 / 卷已用字节」得到确定进度（实测单调、稳定、跨容量自洽）
#   · 扫描子目录：没有真实分母，退化为字节估算，并做单调压制与上限封顶
#   · 聚合 / 排序：总量已知，精确显示
# ==============================================================================

def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def is_volume_root(path: str) -> bool:
    """判断路径是不是一个卷的根（C:\\ 这种）。决定能否用卷已用字节当分母。"""
    try:
        abspath = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    drive, tail = os.path.splitdrive(abspath)
    if drive:
        return tail in ("\\", "/", "")
    return abspath == os.path.abspath(os.sep)


def estimate_scan_percent(snap: dict, volume_scan: bool = True):
    """把进度快照换算成百分比。返回 (百分比或 None, 依据标签)。

    百分比为 None 表示"无法给出可信的分母"——此时界面应显示不确定进度，
    而不是编一个数字出来。纯函数，便于针对各种规模离线验证。
    """
    phase = snap.get("phase", "enum")

    if phase == "done":
        return 100.0, "完成"
    if phase == "aggregate":
        total = snap.get("phase_total") or 0
        r = _clamp01(snap.get("phase_done", 0) / total) if total else 1.0
        lo, hi = PROGRESS_ENUM_END, PROGRESS_AGG_END
        return lo + (hi - lo) * r, "聚合体积"
    if phase == "sort":
        total = snap.get("phase_total") or 0
        r = _clamp01(snap.get("phase_done", 0) / total) if total else 1.0
        lo, hi = PROGRESS_AGG_END, 100.0
        return lo + (hi - lo) * r, "排序"

    # ---- 枚举阶段 ----
    # 只有"扫描卷根"时才有真实分母：卷的已用字节由 disk_usage 给出。
    # 扫描某个子目录时，该目录的总体积在扫完之前无从得知（实测各种估算
    # 都会在早期冲到 99% 然后卡死），因此这里诚实地返回 None。
    found = max(0, snap.get("bytes", 0))
    used = snap.get("disk_used", 0)
    if volume_scan and used > 0:
        return min(found / used * 100.0, PROGRESS_EST_CAP), "按体积"
    return None, "按实况"


def fmt_duration(seconds) -> str:
    """把秒数格式化成 1 时 05 分 / 3 分 20 秒 / 45 秒；None 表示无法估算"""
    if seconds is None:
        return "—"
    s = int(max(0.0, seconds))
    if s < 60:
        return "%d 秒" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%d 分 %02d 秒" % (m, s)
    h, m = divmod(m, 60)
    return "%d 时 %02d 分" % (h, m)


class ProgressTracker:
    """
    把原始百分比整理成"可以放心画到界面上"的进度：
      · 单调不减（估算阶段分母会波动，绝不让进度条倒退）
      · 滑动平均速率（条/秒、字节/秒），避免数字乱跳
      · 已用时间与剩余时间估算
    """

    def __init__(self, alpha: float = 0.25):
        self.alpha = alpha
        self.percent = 0.0
        self.rate = 0.0            # 条目/秒
        self.byte_rate = 0.0       # 字节/秒
        self.elapsed = 0.0
        self._t = None
        self._work = 0
        self._bytes = 0

    def reset(self):
        self.__init__(self.alpha)

    def update(self, pct, work_done: int, snap: dict, now=None) -> float:
        """pct 传 None 表示"本轮拿不到可信百分比"（例如扫描子目录），
        此时只更新速率与计时，百分比维持原值不变。"""
        if pct is not None:
            pct = max(0.0, min(100.0, pct))
            if pct > self.percent:
                self.percent = pct                 # 单调不减

        now = time.monotonic() if now is None else now
        if self._t is None:
            self._t, self._work, self._bytes = now, work_done, snap.get("bytes", 0)
        else:
            dt = now - self._t
            if dt >= 0.15:
                a = self.alpha
                inst = (work_done - self._work) / dt
                self.rate = inst if self.rate <= 0 else self.rate * (1 - a) + inst * a
                inst_b = (snap.get("bytes", 0) - self._bytes) / dt
                self.byte_rate = (inst_b if self.byte_rate <= 0
                                  else self.byte_rate * (1 - a) + inst_b * a)
                self._t, self._work, self._bytes = now, work_done, snap.get("bytes", 0)

        self.elapsed = snap.get("elapsed", self.elapsed)
        return self.percent

    def eta_seconds(self, snap: dict, volume_scan: bool):
        """剩余时间估算。只有"有真实分母 + 有稳定速率"时才给，否则返回 None。"""
        if snap.get("phase") != "enum":
            return None
        if not volume_scan or self.byte_rate <= 1024:
            return None
        used = snap.get("disk_used", 0)
        found = snap.get("bytes", 0)
        if used <= 0:
            return None
        return max(0.0, (used - found) / self.byte_rate)



# ==============================================================================
# 平台适配：安卓 / Linux 上的文件操作（替换 Windows 专有实现）
# ==============================================================================
#
# 原 Windows 版有三处不可移植，这里替换为等价的可移植写法：
#   · _long_path()        —— 加 \?\ 前缀绕过 MAX_PATH(260)。安卓 PATH_MAX
#                            是 4096，且该前缀在安卓上会让路径失效，故直接去掉
#   · send_to_recycle_bin —— 安卓没有回收站 API，按用户要求改为直接删除
#   · reveal_in_explorer  —— 安卓没有资源管理器，按用户要求移除
#
# 另外，安卓上不存在"只读文件导致 os.remove 失败"这一 Windows 特有的坑，
# 但 chmod 重试保留下来当兜底，代价极小、行为不变。


def _force_remove(func, path, _exc):
    """rmtree 的回调：遇到只读文件先摘掉只读位再删"""
    os.chmod(path, stat_mod.S_IRWXU)
    func(path)


def _remove_file(path: str):
    """删除单个文件（保留 chmod 重试兜底）"""
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat_mod.S_IRWXU)
        os.remove(path)


def delete_permanently(paths):
    """
    永久删除。逐个处理，单个失败不影响其余。
    返回 (成功数, [(路径, 错误信息), ...])。

    与 Windows 版的区别：直接删除、无回收站、不可恢复。
    """
    ok = 0
    failed = []
    for p in paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, onerror=_force_remove)
            else:
                _remove_file(p)
            ok += 1
        except Exception as e:                       # noqa: BLE001
            failed.append((p, "%s: %s" % (type(e).__name__, e)))
    return ok, failed


# ==============================================================================
# 平台适配：扫描根目录
# ==============================================================================
#
# 安卓没有盘符。第三方应用能枚举的只有共享存储：
#   /storage/emulated/0    —— 内部存储（用户可见的照片/下载/文档）
#   /storage/XXXX-XXXX     —— 外置 SD 卡（插了卡才有，卷名随机）
#
# 扫 "/" 会从第一层开始全线 PermissionError，所以根目录必须写死。
# 各 App 的私有数据 /data/data/... 无论怎么授权都枚举不到，这是系统限制。

INTERNAL_STORAGE = "/storage/emulated/0"


def scan_roots() -> list[str]:
    """返回可以被扫描的共享存储根目录（按是否存在过滤）"""
    roots = []
    if os.path.isdir(INTERNAL_STORAGE):
        roots.append(INTERNAL_STORAGE)
    try:
        base = "/storage"
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                if name == "emulated":
                    continue
                p = os.path.join(base, name)
                if os.path.isdir(p):
                    roots.append(p)
    except OSError:
        pass
    # 桌面调试兜底：非安卓环境下退回到当前目录，方便在电脑上跑起来
    if not roots:
        roots.append(os.path.abspath(os.sep))
    return roots


# ---- 移动端线程数调优 -------------------------------------------------------
# 原写法在 8 核手机上会开 32 个线程，对移动 CPU 和 IO 都过重，降到最多 16。
# 扫描线程只做 scandir+stat（期间释放 GIL），数量够覆盖 IO 等待即可。
SCAN_WORKERS = max(4, min(16, (os.cpu_count() or 4) * 2))
