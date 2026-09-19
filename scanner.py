# -*- coding: utf-8 -*-
"""
磁盘扫描可视化工具
================================================================================
功能
    1. 选择磁盘后自动扫描（多线程，界面不卡死；无权限的目录/文件自动跳过并计数）
    2. 树状结构展示扫描结果
    3. 用 QStyledItemDelegate 重写 paint()，在「同一行内」绘制两段独立着色的元素：
         · 名字【前段】= 时间档位（默认取最后访问时间）
                         7 天内=绿 / 7~180 天=黄 / 180 天以上=红
         · 名字【后段】= 占用空间档位
                         <1GB=绿 / 1~3GB=黄 / >=3GB=红
    4. 名字之后再以灰色小字显示附加信息，格式形如：(访问: 2025-11-23 | 大小: 2.5GB)
    5. 界面内可调「节点数上限」(MAX_NODES)、「前段指标」、「行内样式」
    6. 筛选：按体积区间（GB）与时间区间（距今天数）过滤，
       筛选后保留树状目录结构，不符合条件的文件与目录被隐藏
    7. 排序：8 种排序方式，递归作用于全树每个目录
    8. 一键重置筛选

行内排版（默认样式，对应参考形状）
    [彩标]  文件名  [彩标]  (访问: 2025-11-23 | 大小: 2.5GB)
     ↑时间档位      ↑体积档位   ↑灰色小字，数值只在这里出现一次，不重复

    另一种样式可在界面切换（色段直接带数值）：
    2025-11-23  文件名  2.5GB  (访问: 2025-11-23 | 大小: 2.5GB)

关于"访问次数"
    Windows 文件系统只存"最后访问时间"，不存在"访问次数"字段，故此处用访问时间对应；
    且部分磁盘默认关闭了访问时间更新，读到 0 时会自动回退用修改时间。

依赖
    PyQt6        (GUI)
    标准库       (扫描 / 聚合，无第三方依赖)
================================================================================
"""

from __future__ import annotations

import os
import sys
import queue
import string
import threading
import time
import datetime
import subprocess
import shutil
import stat as stat_mod

from PyQt6.QtCore import (
    Qt, QAbstractItemModel, QItemSelectionModel, QModelIndex, QObject, QTimer,
    QRect, QRectF, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontMetrics, QPainter, QPalette, QPen,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTreeView, QStyledItemDelegate, QStyleOptionViewItem,
    QStyle, QComboBox, QPushButton, QLabel, QProgressBar, QWidget, QSpinBox,
    QDoubleSpinBox, QCheckBox, QHBoxLayout, QVBoxLayout, QSizePolicy,
    QDialog, QDialogButtonBox, QRadioButton, QLineEdit, QGroupBox, QMenu,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox,
)

# ==============================================================================
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

NODE_ROLE = Qt.ItemDataRole.UserRole + 1   # 通过该 role 把 Node 对象传给 delegate


# ==============================================================================
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


def _long_path(path: str) -> str:
    """超过 MAX_PATH(260) 时加 \\\\?\\ 前缀，让 Win32 API 能处理长路径"""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    p = os.path.abspath(path)
    if len(p) >= LONG_PATH_WARN:
        return "\\\\?\\" + p
    return path


def _force_remove(func, path, _exc):
    """rmtree 的回调：遇到只读文件先摘掉只读位再删"""
    os.chmod(path, stat_mod.S_IRWXU)
    func(path)


def _remove_file(path):
    """
    删除单个文件。Windows 上只读文件 os.remove 会直接抛 PermissionError，
    所以先摘掉只读属性再重试一次——这也是资源管理器删除只读文件的处理方式。
    """
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat_mod.S_IRWXU)
        os.remove(path)


def delete_permanently(paths):
    """
    永久删除（不进回收站）。逐个处理，单个失败不影响其余。
    返回 (成功数, [(路径, 错误信息), ...])。
    """
    ok = 0
    failed = []
    for p in paths:
        try:
            q = _long_path(p)
            if os.path.isdir(q) and not os.path.islink(q):
                shutil.rmtree(q, onerror=_force_remove)
            else:
                _remove_file(q)
            ok += 1
        except Exception as e:                       # noqa: BLE001
            failed.append((p, "%s: %s" % (type(e).__name__, e)))
    return ok, failed


def build_double_null(paths) -> str:
    """SHFileOperationW 的 pFrom 必须是双 \\0 结尾的路径串，这是唯一正确构造方式"""
    return "\0".join(paths) + "\0\0"


def send_to_recycle_bin(paths):
    """
    调用 Windows Shell 的 SHFileOperationW + FOF_ALLOWUNDO，即「移入回收站」。

    返回 (是否成功提交, 是否被中途取消, 错误信息)。

    注意两点：
      · 该 API 的路径长度上限约 260（老 Win32 限制），超长路径请改用永久删除
      · 路径以列表形式交给系统 API，不经过 shell，因此 % & ^ ! 引号 等
        特殊字符都不需要转义，也避免了命令注入
    """
    if os.name != "nt":
        return False, False, "移入回收站仅在 Windows 上可用"
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        FOF_WANTNUKEWARNING = 0x4000

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = build_double_null(paths)
        op.pTo = None
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                     | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        aborted = bool(op.fAnyOperationsAborted)
        if rc != 0:
            return False, aborted, "Shell 返回错误码 %d" % rc
        return True, aborted, ""
    except Exception as e:                          # noqa: BLE001
        return False, False, "%s: %s" % (type(e).__name__, e)


def reveal_in_explorer(path):
    """
    在资源管理器中定位并选中该文件。

    退化策略：
      · 目标是目录        -> 直接打开该目录
      · 文件已不存在但父目录还在 -> 打开父目录（用户至少能看到"文件没了"）
      · 父目录也不在了    -> 返回失败，由界面提示"已被删除或移动"

    用列表形式启动 explorer.exe，不经过 shell，特殊字符无需转义。
    返回 (是否成功, 错误信息)。
    """
    if os.name != "nt":
        return False, "该功能仅在 Windows 上可用"
    try:
        target = path
        select = True
        if not os.path.exists(path):
            parent = os.path.dirname(path.rstrip("\\/"))
            if parent and os.path.isdir(parent):
                target, select = parent, False
            else:
                return False, "文件已被删除或移动，且所在目录也不存在"
        elif os.path.isdir(path):
            select = False

        arg = ("/select," + target) if select else target
        subprocess.Popen(["explorer.exe", arg])
        return True, ""
    except Exception as e:                          # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


# ==============================================================================
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
# 四、视图层：QStyledItemDelegate（核心：一行内分段着色）
# ==============================================================================

class DiskScanDelegate(QStyledItemDelegate):
    """
    重写 paint()，把一行拆成若干段分别绘制：

        [修改时间·按龄着色]  [文件名]  [占用空间·按量着色]  [(修改: ... | 大小: ...) 灰]

    实现方式：
        · 先用 style.drawControl(CE_ItemViewItem) 画好背景/选中/焦点（把文本清空）
        · 再用 QPainter 逐段 setPen + drawText，每段用自己的颜色与字体
        · 空间不够时先省略文件名，仍不够则丢弃灰色补充信息
    """

    def __init__(self, parent=None, metric: str = DEFAULT_METRIC,
                 style_mode: str = DEFAULT_ROW_STYLE):
        super().__init__(parent)
        self.metric = metric              # 前段指标：mtime / atime
        self.style_mode = style_mode      # 行内样式：marks / values

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        s.setHeight(max(ROW_HEIGHT, s.height()))
        return s

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        node: Node | None = index.data(NODE_ROLE)

        # --- 1) 先让样式画背景（选中高亮、交替行色、焦点框） ---
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""                        # 关键：清空默认文本，后面自己画
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        if node is None:
            return

        rect = option.rect.adjusted(H_GAP // 2, 0, -(H_GAP // 2), 0)
        if rect.width() <= 8:
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        group = QPalette.ColorGroup.Normal
        text_role = (QPalette.ColorRole.HighlightedText if selected
                     else QPalette.ColorRole.Text)
        base_color = opt.palette.color(group, text_role)

        # --- 2) 准备字体与度量 ---
        name_font = QFont(option.font)
        name_font.setBold(node.is_dir)                # 目录加粗，一眼区分
        detail_font = QFont(option.font)
        detail_font.setPointSizeF(max(6.0, option.font.pointSizeF() - 1.5))

        fm_name = QFontMetrics(name_font)
        fm_detail = QFontMetrics(detail_font)

        # --- 3) 计算行内各段 ---
        ts = metric_ts(node, self.metric)
        detail = fmt_detail(node, self.metric)
        color_time = QColor(BUCKET_COLORS[age_bucket(ts)])
        color_size = QColor(BUCKET_COLORS[size_bucket(node.size)])

        marks_mode = (self.style_mode == ROW_STYLE_MARKS)
        if marks_mode:
            # 参考形状：两个色标只承载颜色档位，具体数值都在灰色括号里，不重复显示
            mark_px = max(MARK_MIN_PX, int(option.font.pointSizeF() * 0.8))
            w_prefix = w_suffix = mark_px
            prefix_text = suffix_text = ""
        else:
            # 色段直接带数值
            prefix_text = fmt_date(ts)
            suffix_text = fmt_size(node.size)
            w_prefix = fm_name.horizontalAdvance(prefix_text)
            w_suffix = fm_name.horizontalAdvance(suffix_text)

        w_detail = fm_detail.horizontalAdvance(detail)

        # 空间预算。优先级：两段着色 > 灰色补充 > 文件名展开
        # 也就是空间紧张时先压缩文件名，而不是丢掉灰色补充信息。
        inner = rect.width()
        MIN_NAME = 30                                   # 文件名最多压到这么窄
        space = inner - w_prefix - w_suffix             # 扣掉前段 + 后段
        if space - H_GAP * 3 - w_detail >= MIN_NAME:
            show_detail = True
            name_budget = space - H_GAP * 3 - w_detail
        else:
            show_detail = False
            name_budget = space - H_GAP * 2
        name_budget = max(MIN_NAME, name_budget)

        name_text = fm_name.elidedText(node.name, Qt.TextElideMode.ElideRight,
                                       name_budget)
        w_name = fm_name.horizontalAdvance(name_text)

        # --- 4) 开始绘制 ---
        painter.save()
        painter.setClipRect(option.rect)

        top, height = rect.top(), rect.height()
        vflags = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        x = rect.left()

        # 4.1 前段：时间档位（绿 / 黄 / 红）
        painter.setFont(name_font)
        if marks_mode:
            self._draw_mark(painter, x, top, height, mark_px, color_time)
        else:
            painter.setPen(QPen(color_time))
            painter.drawText(QRect(x, top, w_prefix, height), vflags, prefix_text)
        x += w_prefix + H_GAP

        # 4.2 文件名（默认/高亮前景色）
        painter.setPen(QPen(base_color))
        painter.drawText(QRect(x, top, w_name, height), vflags, name_text)
        x += w_name + H_GAP

        # 4.3 后段：体积档位（绿 / 黄 / 红）
        if marks_mode:
            self._draw_mark(painter, x, top, height, mark_px, color_size)
        else:
            painter.setPen(QPen(color_size))
            painter.drawText(QRect(x, top, w_suffix, height), vflags, suffix_text)
        x += w_suffix + H_GAP

        # 4.4 灰色小字补充信息
        if show_detail:
            painter.setFont(detail_font)
            painter.setPen(QPen(QColor(DETAIL_COLOR)))
            painter.drawText(QRect(x, top, w_detail, height), vflags, detail)

        painter.restore()

    @staticmethod
    def _draw_mark(painter: QPainter, x: int, top: int, height: int,
                   px: int, color: QColor):
        """画一个小方块作为颜色档位标记（不依赖字体，任何环境都能渲染）"""
        rect = QRectF(x, top + (height - px) / 2.0, px, px)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(rect, 2.0, 2.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)


# ==============================================================================
# 五、模型层：惰性树模型（节点已在内存里，模型只负责按需生成 QModelIndex）
# ==============================================================================

class TreeModel(QAbstractItemModel):

    def __init__(self, root: Node, parent=None, metric: str = METRIC_MTIME):
        super().__init__(parent)
        self._root = root
        self.metric = metric

    # ------------------------------------------------------------------ 结构
    # 所有结构方法都走 visible_children()：未筛选时它就等于 children，
    # 筛选后自动变成可见子列表，因此模型不需要任何额外分支。
    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        pnode = parent.internalPointer() if parent.isValid() else self._root
        if pnode is None:
            return QModelIndex()
        vc = visible_children(pnode)
        if row >= len(vc):
            return QModelIndex()
        return self.createIndex(row, column, vc[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node: Node = index.internalPointer()
        p = node.parent
        if p is None or p is self._root:
            return QModelIndex()
        # node.row 由 apply_filter/sort_tree 维护为"在当前生效列表中的下标"
        return self.createIndex(node.row, 0, p)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        pnode = parent.internalPointer() if parent.isValid() else self._root
        return len(visible_children(pnode)) if pnode else 0

    def columnCount(self, parent=QModelIndex()):
        return 1

    def hasChildren(self, parent=QModelIndex()):
        pnode = parent.internalPointer() if parent.isValid() else self._root
        return bool(pnode and visible_children(pnode))

    def index_for_node(self, node: Node) -> QModelIndex:
        """由节点反查 QModelIndex（用于筛选后恢复展开状态）。
        祖先只要有一级不可见就返回无效索引。"""
        chain = []
        n = node
        while n is not None and n is not self._root:
            chain.append(n)
            n = n.parent
        if n is not self._root:
            return QModelIndex()
        idx = QModelIndex()
        for n in reversed(chain):
            idx = self.index(n.row, 0, idx)
            if not idx.isValid():
                return QModelIndex()
        return idx

    @property
    def root(self) -> Node:
        return self._root

    def refresh(self):
        """树被就地改动（排序 / 筛选）后通知视图全量重建"""
        self.beginResetModel()
        self.endResetModel()

    # ------------------------------------------------------------------ 数据
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node: Node = index.internalPointer()
        if role == NODE_ROLE:
            return node
        if role == Qt.ItemDataRole.DisplayRole:
            # 纯文本版（复制/Accessibility 用；实际绘制由 delegate 接管）
            return "%s  %s  %s  %s" % (fmt_date(metric_ts(node, self.metric)),
                                       node.name, fmt_size(node.size),
                                       fmt_detail(node, self.metric))
        if role == Qt.ItemDataRole.ToolTipRole:
            return "%s\n最后修改: %s\n最后访问: %s\n大小: %s\n类型: %s" % (
                node.path, fmt_date(node.mtime), fmt_date(metric_ts(node, METRIC_ATIME)),
                fmt_size(node.size), "目录" if node.is_dir else "文件")
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return "名称"
        return None


# ==============================================================================
# 六、扫描控制器（把后台线程的结果用信号送回主线程）
# ==============================================================================

class ScanController(QObject):
    finished = pyqtSignal(object)      # 参数：根 Node（失败时为 None）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = ScanEngine()
        self._thread: threading.Thread | None = None
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, path: str, max_nodes: int | None = None):
        self.stop()
        self.engine = ScanEngine(max_nodes if max_nodes else MAX_NODES)
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, args=(path,), daemon=True)
        self._thread.start()

    def _run(self, path: str):
        root = None
        try:
            root = self.engine.scan(path)
        except Exception as e:                       # noqa: BLE001
            self.engine.error = str(e)
        if self._cancelled:
            return                                   # 用户已手动停止，不要回传半成品
        self.finished.emit(root)

    def stop(self):
        self._cancelled = True
        if self.running:
            self.engine.cancel()
            if self._thread is not None:
                self._thread.join(timeout=3.0)
        self._thread = None


# ==============================================================================
# 六·五、对话框：删除确认 / 多选窗口
# ==============================================================================

class ConfirmDeleteDialog(QDialog):
    """
    删除确认框。

    确认机制（三重）：
      1. 默认按钮是「取消」，回车不会误删
      2. 默认方式是「移入回收站」（可恢复）；永久删除必须显式切换
      3. 删除量大（超过 BULK_DELETE_THRESHOLD 项或涉及的文件总数超过阈值）时，
         必须手动键入项目数量，删除按钮才会解禁

    同时把风险信息前置：目录会连带的文件数、已不存在的项、超长路径。
    """

    PREVIEW_ROWS = 200

    def __init__(self, nodes, missing_count: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认删除")
        self.setMinimumWidth(720)
        self.nodes = list(nodes)
        self.summary = selection_summary(self.nodes)
        self.mode = "recycle"
        self._build(missing_count)

    # ------------------------------------------------------------------ 构建
    def _build(self, missing_count: int):
        lay = QVBoxLayout(self)
        s = self.summary

        head = QLabel("即将删除 <b>%d</b> 个项目（文件 %d 个、目录 %d 个）。"
                      % (s["count"], s["files"], s["dirs"]))
        head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(head)

        if s["dirs"]:
            warn = QLabel("注意：选中的目录会连同其下内容一并删除，"
                          "合计牵涉 %d 个文件、%d 个子目录。"
                          % (s["child_files"], s["child_dirs"]))
            warn.setStyleSheet("color:#d93025;")
            lay.addWidget(warn)

        lay.addWidget(QLabel("合计占用：%s" % fmt_size(s["bytes"])))

        if missing_count:
            m = QLabel("其中 %d 项在磁盘上已不存在（可能已被删除或移动），"
                       "将自动跳过。" % missing_count)
            m.setStyleSheet("color:#b8860b;")
            lay.addWidget(m)

        if s["longest_path"] > LONG_PATH_WARN:
            lp = QLabel("最长路径 %d 字符。超过 %d 字符的路径在「移入回收站」时"
                        "可能失败，这类项建议改用永久删除。"
                        % (s["longest_path"], LONG_PATH_WARN))
            lp.setStyleSheet("color:#b8860b;")
            lp.setWordWrap(True)
            lay.addWidget(lp)

        tbl = QTableWidget()
        rows = min(len(self.nodes), self.PREVIEW_ROWS)
        tbl.setColumnCount(2)
        tbl.setHorizontalHeaderLabels(["名称", "完整路径"])
        tbl.setRowCount(rows)
        for i in range(rows):
            n = self.nodes[i]
            tbl.setItem(i, 0, QTableWidgetItem(n.name))
            tbl.setItem(i, 1, QTableWidgetItem(n.path))
        tbl.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        tbl.setMinimumHeight(170)
        lay.addWidget(tbl)
        if len(self.nodes) > rows:
            lay.addWidget(QLabel("列表中仅预览前 %d 项，其余 %d 项同样会被删除。"
                                 % (rows, len(self.nodes) - rows)))

        box = QGroupBox("删除方式")
        bv = QVBoxLayout(box)
        self.rb_recycle = QRadioButton("移入回收站（可恢复，推荐）")
        self.rb_recycle.setChecked(True)
        self.rb_perm = QRadioButton("永久删除（不进回收站，不可恢复）")
        bv.addWidget(self.rb_recycle)
        bv.addWidget(self.rb_perm)
        lay.addWidget(box)

        # 大量删除 / 涉及目录 -> 需要键入数量
        total_files = s["files"] + s["child_files"]
        self.need_typed = (s["count"] > BULK_DELETE_THRESHOLD
                           or total_files > BULK_DELETE_THRESHOLD)
        self.typed_edit = None
        if self.need_typed:
            tip = QLabel("本次删除量较大，请输入待删除项目数 <b>%d</b> 以确认："
                         % s["count"])
            tip.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(tip)
            self.typed_edit = QLineEdit()
            self.typed_edit.setPlaceholderText("键入 %d" % s["count"])
            self.typed_edit.textChanged.connect(self.validate)
            lay.addWidget(self.typed_edit)

        bb = QDialogButtonBox()
        self.btn_cancel = bb.addButton("取消",
                                       QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_ok = bb.addButton("删除",
                                   QDialogButtonBox.ButtonRole.DestructiveRole)
        bb.rejected.connect(self.reject)
        self.btn_ok.clicked.connect(self._on_ok)
        lay.addWidget(bb)

        self.rb_perm.toggled.connect(self._on_mode_changed)
        self.btn_cancel.setDefault(True)
        self.btn_cancel.setFocus()
        self.validate()

    # ------------------------------------------------------------------ 逻辑
    def required_text(self) -> str:
        return str(self.summary["count"])

    def validate(self, *_):
        """只有键入的数量完全正确，删除按钮才可用"""
        if self.typed_edit is None:
            self.btn_ok.setEnabled(True)
        else:
            self.btn_ok.setEnabled(
                self.typed_edit.text().strip() == self.required_text())

    def _on_mode_changed(self, perm_checked: bool):
        self.mode = "permanent" if perm_checked else "recycle"

    def _on_ok(self):
        self.mode = "permanent" if self.rb_perm.isChecked() else "recycle"
        self.accept()


class SelectionDialog(QDialog):
    """
    多选窗口：列出所有选中项的名称与完整路径，并提供批量操作。

    设计取舍：这是打开时刻的**快照**，不会随主窗口选择变化而跳动
    （避免你在窗口里操作时列表突然被换掉）；需要同步时点「刷新」。

    大选择集处理：表格最多渲染 SELECTION_LIST_LIMIT 行，但复制/打开/删除
    等操作针对的始终是**完整集合**，不是表格里看得见的那部分。
    """

    delete_requested = pyqtSignal(list)     # 参数：待删除的 Node 列表
    reveal_requested = pyqtSignal(list)     # 参数：要定位的 Node 列表
    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选中项")
        self.resize(900, 560)
        self.setModal(False)                 # 非模态：不挡主窗口继续操作
        self.nodes = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)

        self.head = QLabel()
        self.head.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.head)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["名称", "完整路径", "类型", "大小", "访问时间"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

        self.note = QLabel()
        self.note.setStyleSheet("color:#b8860b;")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        bar = QHBoxLayout()
        self.btn_copy_all = QPushButton("复制全部路径")
        self.btn_copy_all.clicked.connect(self.copy_all_paths)
        bar.addWidget(self.btn_copy_all)

        self.btn_copy_sel = QPushButton("复制选中行路径")
        self.btn_copy_sel.clicked.connect(self.copy_selected_paths)
        bar.addWidget(self.btn_copy_sel)

        self.btn_reveal = QPushButton("打开所在位置")
        self.btn_reveal.clicked.connect(self._on_reveal)
        bar.addWidget(self.btn_reveal)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh_requested.emit)
        bar.addWidget(self.btn_refresh)

        bar.addStretch(1)
        self.btn_delete = QPushButton("删除选中项")
        self.btn_delete.clicked.connect(self._on_delete)
        bar.addWidget(self.btn_delete)

        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        bar.addWidget(self.btn_close)
        lay.addLayout(bar)

    # ------------------------------------------------------------------ 数据
    def set_nodes(self, nodes):
        """更新快照。nodes 为 None 时表示主窗口没有可用的选中项。"""
        self.nodes = list(nodes or [])
        n = len(self.nodes)

        files = sum(1 for x in self.nodes if not x.is_dir)
        dirs = n - files
        self.head.setText(
            "共选中 <b>%d</b> 项（文件 %d 个、目录 %d 个）。"
            % (n, files, dirs))

        shown = min(n, SELECTION_LIST_LIMIT)
        self.table.setRowCount(shown)
        for i in range(shown):
            node = self.nodes[i]
            self.table.setItem(i, 0, QTableWidgetItem(node.name))
            self.table.setItem(i, 1, QTableWidgetItem(node.path))
            self.table.setItem(i, 2, QTableWidgetItem("目录" if node.is_dir else "文件"))
            self.table.setItem(i, 3, QTableWidgetItem(fmt_size(node.size)))
            self.table.setItem(i, 4,
                               QTableWidgetItem(fmt_date(metric_ts(node, DEFAULT_METRIC))))

        if n > shown:
            self.note.setText(
                "列表仅渲染前 %d 行；下方的复制 / 打开 / 删除针对完整选中的 %d 项，"
                "不受表格行数限制。" % (shown, n))
        else:
            self.note.setText("")

        empty = (n == 0)
        for b in (self.btn_copy_all, self.btn_reveal, self.btn_delete):
            b.setEnabled(not empty)
        self.btn_copy_sel.setEnabled(not empty)

    def selected_rows(self) -> list:
        """取表格中被高亮选中的行对应的节点；没有高亮则返回空列表"""
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        return [self.nodes[r] for r in rows if 0 <= r < len(self.nodes)]

    # ------------------------------------------------------------------ 操作
    def copy_all_paths(self):
        paths = node_paths(self.nodes)
        if not paths:
            return 0
        QApplication.clipboard().setText(CLIPBOARD_SEP.join(paths))
        return len(paths)

    def copy_selected_paths(self):
        nodes = self.selected_rows()
        paths = node_paths(nodes)
        if not paths:
            return 0
        QApplication.clipboard().setText(CLIPBOARD_SEP.join(paths))
        return len(paths)

    def _on_reveal(self):
        nodes = self.selected_rows() or list(self.nodes)
        if nodes:
            self.reveal_requested.emit(nodes)

    def _on_delete(self):
        nodes = self.selected_rows() or list(self.nodes)
        if nodes:
            self.delete_requested.emit(nodes)


# ==============================================================================
# 七、主窗口
# ==============================================================================

def list_drives() -> list[str]:
    """列出可扫描的盘符（跳过多媒体/虚拟盘）"""
    drives = []
    if os.name == "nt":
        import ctypes
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        for letter in string.ascii_uppercase:
            root = "%s:\\" % letter
            if not os.path.exists(root):
                continue
            if get_type(ctypes.c_wchar_p(root)) in (2, 3, 4):   # 可移动/固定/网络
                drives.append(root)
    else:
        drives.append(os.path.abspath(os.sep))
    return drives


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("磁盘扫描可视化")
        self.resize(1100, 700)

        self.controller = ScanController(self)
        self.controller.finished.connect(self.on_finished)
        self.model: TreeModel | None = None    # 由 Python 持有，便于旧模型及时回收

        # 视图状态（排序/筛选）与展开记录。必须在 _build_ui 之前准备好，
        # 因为控件初始化过程中可能触发信号回调。
        self._sort_sig = None
        self._view_sig = None
        self._expanded_nodes: set = set()
        self._scan_summary = ""
        self.sel_dialog: SelectionDialog | None = None    # 多选窗口（惰性创建）

        # 进度模型：tracker 负责单调化与速率平滑；_volume_scan 决定用哪种算法
        self.tracker = ProgressTracker()
        self._volume_scan = False

        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(320)      # 防抖：别每敲一个键就全树遍历
        self.filter_timer.timeout.connect(lambda: self.refresh_view())

        self._build_ui()

        # 定时刷新进度（worker 线程只改计数器，不碰 Qt 对象）
        self.timer = QTimer(self)
        self.timer.setInterval(PROGRESS_TICK_MS)
        self.timer.timeout.connect(self.refresh_progress)

        if AUTO_SCAN:
            QTimer.singleShot(250, self.start_scan)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        bar.addWidget(QLabel("扫描磁盘："))
        self.drive_box = QComboBox()
        self.drive_box.setMinimumWidth(120)
        drives = list_drives()
        self.drive_box.addItems(drives)
        sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
        if sysdrive in drives:
            self.drive_box.setCurrentText(sysdrive)
        bar.addWidget(self.drive_box)

        self.btn_start = QPushButton("开始扫描")
        self.btn_start.clicked.connect(self.start_scan)
        bar.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)
        bar.addWidget(self.btn_stop)

        bar.addStretch(1)

        self.legend = QLabel()
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        bar.addWidget(self.legend)

        root_layout.addLayout(bar)

        # ---- 第二行：可调参数 ----
        bar2 = QHBoxLayout()
        bar2.setSpacing(8)

        bar2.addWidget(QLabel("节点数上限："))
        self.nodes_box = QSpinBox()
        self.nodes_box.setRange(10_000, 50_000_000)
        self.nodes_box.setSingleStep(100_000)
        self.nodes_box.setValue(MAX_NODES)
        self.nodes_box.setGroupSeparatorShown(True)
        self.nodes_box.setSuffix(" 个")
        self.nodes_box.setMinimumWidth(150)
        self.nodes_box.setToolTip(
            "单次扫描最多记录多少个节点（目录 + 文件）。\n"
            "触顶后扫描会提前结束并在状态栏提示。\n"
            "调大能扫更全，但占用内存也线性增加。\n"
            "下一 次点击「开始扫描」时生效。")
        bar2.addWidget(self.nodes_box)

        bar2.addSpacing(16)
        bar2.addWidget(QLabel("前段指标："))
        self.metric_box = QComboBox()
        for key, name, _label in METRIC_CHOICES:
            self.metric_box.addItem(name, key)
        self.metric_box.setMinimumWidth(130)
        self.metric_box.setCurrentIndex(
            max(0, self.metric_box.findData(DEFAULT_METRIC)))
        self.metric_box.setToolTip(
            "决定每行最前面那个色标按哪个时间着色，\n"
            "灰色括号里的第一个字段也会跟着变。\n"
            "注：Windows 文件系统只记录「最后访问时间」，\n"
            "并没有「访问次数」这种字段；且部分磁盘默认\n"
            "关闭了访问时间更新，此时会回退用修改时间。")
        self.metric_box.currentIndexChanged.connect(self.on_metric_changed)
        bar2.addWidget(self.metric_box)

        bar2.addSpacing(16)
        bar2.addWidget(QLabel("行内样式："))
        self.style_box = QComboBox()
        for key, name in ROW_STYLE_CHOICES:
            self.style_box.addItem(name, key)
        self.style_box.setMinimumWidth(140)
        self.style_box.setCurrentIndex(
            max(0, self.style_box.findData(DEFAULT_ROW_STYLE)))
        self.style_box.setToolTip(
            "色标（参考形状）：[色标]名字[色标] (访问: … | 大小: …)\n"
            "色段带数值：2023-10-01 名字 2.5GB (访问: … | 大小: …)")
        self.style_box.currentIndexChanged.connect(self.on_style_changed)
        bar2.addWidget(self.style_box)

        bar2.addSpacing(16)
        bar2.addWidget(QLabel("排序："))
        self.sort_box = QComboBox()
        for key, name in SORT_MODES:
            self.sort_box.addItem(name, key)
        self.sort_box.setMinimumWidth(180)
        self.sort_box.setCurrentIndex(max(0, self.sort_box.findData(DEFAULT_SORT)))
        self.sort_box.setToolTip(
            "对每个目录的子节点分别排序（递归全树）。\n"
            "实现用 list.sort(key=...)：Timsort 对每个元素只计算一次键，\n"
            "而不是在 Python 层做 O(n log n) 次比较——后者实测慢约一个数量级。")
        self.sort_box.currentIndexChanged.connect(self.on_sort_changed)
        bar2.addWidget(self.sort_box)

        bar2.addStretch(1)
        hint = QLabel("除「节点数上限」外均立即生效")
        hint.setStyleSheet("color:#8a8a8a;")
        bar2.addWidget(hint)

        root_layout.addLayout(bar2)

        # ---- 第三行：筛选 ----
        bar3 = QHBoxLayout()
        bar3.setSpacing(6)

        bar3.addWidget(QLabel("筛选 · 大小："))
        self.size_min_box = self._make_size_box()
        self.size_max_box = self._make_size_box()
        bar3.addWidget(self.size_min_box)
        bar3.addWidget(QLabel("~"))
        bar3.addWidget(self.size_max_box)
        bar3.addWidget(QLabel("GB"))

        bar3.addSpacing(14)
        bar3.addWidget(QLabel("距今："))
        self.days_min_box = self._make_days_box()
        self.days_max_box = self._make_days_box()
        bar3.addWidget(self.days_min_box)
        bar3.addWidget(QLabel("~"))
        bar3.addWidget(self.days_max_box)
        bar3.addWidget(QLabel("天"))

        bar3.addSpacing(14)
        self.keep_parents_box = QCheckBox("保留上级路径")
        self.keep_parents_box.setChecked(True)
        self.keep_parents_box.setToolTip(
            "勾选（默认）：文件夹即使自身不在范围内，只要下面有符合条件的项，\n"
            "就保留下来作为路径，树形结构不会散架。\n"
            "取消勾选：严格模式，只要自身不符合条件就整枝隐藏\n"
            "（此时藏在不符合条件的文件夹里的文件也会一起看不见）。")
        self.keep_parents_box.stateChanged.connect(self.on_filter_changed)
        bar3.addWidget(self.keep_parents_box)

        self.btn_reset = QPushButton("重置筛选")
        self.btn_reset.setToolTip("清空四个区间并把树恢复成完整显示")
        self.btn_reset.clicked.connect(self.reset_filters)
        bar3.addWidget(self.btn_reset)

        bar3.addStretch(1)
        fhint = QLabel("留 0 或显示「不限」表示该侧不限制；目录按聚合体积判定")
        fhint.setStyleSheet("color:#8a8a8a;")
        bar3.addWidget(fhint)

        root_layout.addLayout(bar3)

        # ---- 第四行：选中项操作 ----
        bar4 = QHBoxLayout()
        bar4.setSpacing(8)

        bar4.addWidget(QLabel("选中操作："))

        self.btn_selection = QPushButton("查看选中 (0)")
        self.btn_selection.setMinimumWidth(150)
        self.btn_selection.setToolTip(
            "打开多选窗口：列出所有选中项的名称与完整路径，\n"
            "并支持批量复制路径 / 打开所在位置 / 删除")
        self.btn_selection.clicked.connect(self.open_selection_dialog)
        bar4.addWidget(self.btn_selection)

        self.btn_copy_path = QPushButton("复制完整路径")
        self.btn_copy_path.setToolTip("把选中项的完整路径复制到剪贴板，每行一个")
        self.btn_copy_path.clicked.connect(self.copy_selected_paths)
        bar4.addWidget(self.btn_copy_path)

        self.btn_reveal = QPushButton("打开所在位置")
        self.btn_reveal.setToolTip("在资源管理器中定位并选中该文件")
        self.btn_reveal.clicked.connect(self.reveal_selected)
        bar4.addWidget(self.btn_reveal)

        self.btn_delete = QPushButton("删除")
        self.btn_delete.setStyleSheet("color:#a32d2d;")
        self.btn_delete.setToolTip(
            "删除选中项。执行前会弹出确认框；\n"
            "默认「移入回收站」可恢复，永久删除需在确认框里显式切换")
        self.btn_delete.clicked.connect(self.delete_selected)
        bar4.addWidget(self.btn_delete)

        bar4.addStretch(1)
        self.sel_hint = QLabel(
            "左键拖拽可连续拉选；Ctrl 点选可多选；右键点击可把该项加入选择")
        self.sel_hint.setStyleSheet("color:#8a8a8a;")
        bar4.addWidget(self.sel_hint)

        root_layout.addLayout(bar4)

        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)      # 行高统一，百万节点也能流畅滚
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.tree.setIndentation(16)

        # --- 选择交互 ---
        # ExtendedSelection：既支持按下左键拖拽连续拉选，也支持 Ctrl/Shift 点选
        self.tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        # 关掉拖放，否则左键拖拽会被解释成"拖动条目"而不是"拉选"
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        # 拖拽选择时不自动展开途经目录，避免视野乱跳
        self.tree.setAutoExpandDelay(-1)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_tree_context_menu)

        self.delegate = DiskScanDelegate(self.tree, metric=self.current_metric(),
                                         style_mode=self.current_style())
        self.tree.setItemDelegate(self.delegate)
        self.tree.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        # 记录展开状态：用信号维护集合（O(1)），
        # 比每次筛选前全树扫描 isExpanded() 快得多
        self.tree.expanded.connect(self._on_node_expanded)
        self.tree.collapsed.connect(self._on_node_collapsed)
        root_layout.addWidget(self.tree, 1)

        self.update_legend()          # 需等 metric_box 建好之后再刷新图例
        self._update_selection_ui()   # 初始无选中：相关按钮置灰

        self.setCentralWidget(central)
        self.status = self.statusBar()

        # 进度条 + 实时明细常驻状态栏右侧。放这里而不是工具栏：
        # 进度属于"状态"，和消息在同一行才读得顺。
        self.progress_label = QLabel()
        self.status.addPermanentWidget(self.progress_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)      # 千分制，刻度到 0.1%
        self.progress.setFormat("%p%")
        self.progress.setFixedWidth(280)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)

        self.status.showMessage("就绪")

    # ------------------------------------------------------------------ 扫描
    def current_metric(self) -> str:
        """当前选中的前段指标（paint 与详情文本都用它）"""
        return self.metric_box.currentData() or DEFAULT_METRIC

    def current_style(self) -> str:
        """当前选中的行内样式"""
        return self.style_box.currentData() or DEFAULT_ROW_STYLE

    def update_legend(self):
        """图例跟随当前指标，避免出现"7天内修改"却在按访问时间着色"""
        word = "访问" if self.current_metric() == METRIC_ATIME else "修改"
        self.legend.setText(
            '<span style="color:%s">■</span> 7天内%s / &lt;1GB&nbsp;&nbsp;'
            '<span style="color:%s">■</span> 7~180天 / 1~3GB&nbsp;&nbsp;'
            '<span style="color:%s">■</span> 180天以上 / ≥3GB'
            % (BUCKET_COLORS["fresh"], word,
               BUCKET_COLORS["mid"], BUCKET_COLORS["old"]))

    # ------------------------------------------------------- 筛选 / 排序控件
    def _make_size_box(self) -> QDoubleSpinBox:
        b = QDoubleSpinBox()
        b.setRange(0.0, 1_000_000.0)
        b.setDecimals(2)
        b.setSingleStep(0.5)
        b.setSpecialValueText("不限")      # 取到最小值时直接显示"不限"
        b.setMinimumWidth(84)
        b.setValue(0.0)
        b.valueChanged.connect(self.on_filter_changed)
        return b

    def _make_days_box(self) -> QSpinBox:
        b = QSpinBox()
        b.setRange(0, 36500)
        b.setSingleStep(1)
        b.setSpecialValueText("不限")
        b.setMinimumWidth(76)
        b.setValue(0)
        b.valueChanged.connect(self.on_filter_changed)
        return b

    def current_sort(self) -> str:
        return self.sort_box.currentData() or DEFAULT_SORT

    def current_filter(self) -> NodeFilter:
        """把四个输入框读成筛选条件。0 / "不限" 表示该侧不限制。"""
        smin = self.size_min_box.value()
        smax = self.size_max_box.value()
        dmin = self.days_min_box.value()
        dmax = self.days_max_box.value()
        return NodeFilter(
            size_min=smin if smin > 0 else None,
            size_max=smax if smax > 0 else None,
            days_min=dmin if dmin > 0 else None,
            days_max=dmax if dmax > 0 else None,
            metric=self.current_metric())

    # ------------------------------------------------------- 展开状态跟踪
    def _on_node_expanded(self, index):
        node = index.internalPointer()
        if node is not None:
            self._expanded_nodes.add(node)

    def _on_node_collapsed(self, index):
        node = index.internalPointer()
        if node is not None:
            self._expanded_nodes.discard(node)

    # ------------------------------------------------------- 回调
    def on_filter_changed(self, *_):
        """区间输入变化 -> 先防抖，避免每敲一个键就全树遍历一遍"""
        self.filter_timer.start()

    def on_sort_changed(self, *_):
        self.filter_timer.stop()
        self.refresh_view(force=True)

    def reset_filters(self):
        """一键重置：清空四个区间（0 即"不限"）并取消筛选。
        排序方式保持不变，展开状态也会尽量保留。"""
        self.filter_timer.stop()
        for b in (self.size_min_box, self.size_max_box,
                  self.days_min_box, self.days_max_box):
            b.blockSignals(True)      # 四个一起改，避免触发四次刷新
            b.setValue(0)
            b.blockSignals(False)
        self.refresh_view(force=True)

    # ------------------------------------------------------- 视图刷新
    def refresh_view(self, force: bool = False):
        """
        按当前排序方式与筛选条件刷新视图。
        返回 (可见文件数, 可见目录数, 隐藏项数)；无变化或尚无数据时返回 None。

        性能设计：
          · 排序只在「排序模式或时间指标变化」时执行，Timsort + key= ，
            每个元素只算一次键（不用 cmp_to_key）
          · 筛选是两遍线性遍历 O(N)，且只有"确有子项被隐藏"的目录才额外分配列表
          · 状态未变化时靠 signature 比对直接跳过
        """
        if self.model is None:
            return None

        root = self.model.root
        metric = self.current_metric()
        mode = self.current_sort()
        flt = self.current_filter()
        keep = self.keep_parents_box.isChecked()

        sig = (mode, metric, flt.size_min, flt.size_max,
               flt.days_min, flt.days_max, keep)
        if not force and sig == self._view_sig:
            return None

        # 1) 排序（仅在需要时）
        sort_sig = (mode, metric)
        if force or sort_sig != self._sort_sig:
            sort_tree(root, mode, metric)
            self._sort_sig = sort_sig

        # 2) 筛选：就地改可见性并重写 row 下标
        stats = apply_filter(root, flt if flt.is_active else None, keep)
        self._view_sig = sig

        # 3) 模型复位 + 恢复展开状态
        self.model.refresh()
        self._restore_expanded()

        # 4) 状态栏反馈
        self._show_view_status(stats, flt)
        return stats

    def _restore_expanded(self):
        """筛选后尽量把原来展开的目录重新展开（父级被隐藏的自然丢弃）"""
        if self.model is None or not self._expanded_nodes:
            return
        alive = set()
        for node in self._expanded_nodes:
            idx = self.model.index_for_node(node)
            if idx.isValid():
                self.tree.expand(idx)
                alive.add(node)
        self._expanded_nodes = alive

    def _show_view_status(self, stats, flt):
        if stats is None:
            return
        n_files, n_dirs, n_hidden = stats
        if flt is not None and flt.is_active:
            self.status.showMessage(
                "筛选生效：显示 %s 个文件 / %s 个目录，隐藏 %s 项    |    %s"
                % (f"{n_files:,}", f"{n_dirs:,}", f"{n_hidden:,}",
                   self._scan_summary))
        else:
            self.status.showMessage(self._scan_summary or "就绪")

    # ============================================================== 选中项管理
    def selected_nodes(self) -> list:
        """当前树里被选中的节点（按行序去重）。无模型/无选择时返回空列表。"""
        if self.model is None:
            return []
        sm = self.tree.selectionModel()
        if sm is None:
            return []
        out, seen = [], set()
        for idx in sm.selectedRows(0):
            node = idx.data(NODE_ROLE)
            if node is not None and id(node) not in seen:
                seen.add(id(node))
                out.append(node)
        return out

    def on_selection_changed(self, *_):
        self._update_selection_ui()

    def _update_selection_ui(self):
        n = len(self.selected_nodes())
        self.btn_selection.setText("查看选中 (%d)" % n)
        for b in (self.btn_copy_path, self.btn_reveal, self.btn_delete):
            b.setEnabled(n > 0)
        if n:
            # 多选后给一个明确去向，而不是自动弹窗打断操作
            self.sel_hint.setText("已选中 %d 项 —— 点左侧「查看选中」可打开多选窗口" % n)
        else:
            self.sel_hint.setText(
                "左键拖拽可连续拉选；Ctrl 点选可多选；右键点击可把该项加入选择")

    # -------------------------------------------------------------- 右键菜单
    def on_tree_context_menu(self, pos):
        if self.model is None:
            return
        sm = self.tree.selectionModel()
        if sm is None:
            return
        index = self.tree.indexAt(pos)

        # 右键多选：点在未选中的条目上时，把该项「加入」已有选择而不是替换掉。
        # （Qt 默认行为已接近这样，这里显式做一次，避免不同版本差异）
        if index.isValid() and index not in sm.selectedIndexes():
            sm.select(index,
                      QItemSelectionModel.SelectionFlag.Select
                      | QItemSelectionModel.SelectionFlag.Rows)
        self._update_selection_ui()

        nodes = self.selected_nodes()
        if not nodes:
            return

        menu = QMenu(self)
        head = menu.addAction("已选中 %d 项" % len(nodes))
        head.setEnabled(False)
        menu.addSeparator()
        menu.addAction("打开多选窗口…", self.open_selection_dialog)
        menu.addAction("复制完整路径", self.copy_selected_paths)
        menu.addAction("打开所在位置", self.reveal_selected)
        menu.addSeparator()
        menu.addAction("删除选中项…", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------- 多选窗口
    def open_selection_dialog(self, *_):
        nodes = self.selected_nodes()
        if not nodes:
            self.status.showMessage("没有选中任何项目")
            return
        if self.sel_dialog is None:
            self.sel_dialog = SelectionDialog(self)
            self.sel_dialog.delete_requested.connect(self.delete_nodes)
            self.sel_dialog.reveal_requested.connect(self.reveal_nodes)
            self.sel_dialog.refresh_requested.connect(self._refresh_selection_dialog)
        self.sel_dialog.set_nodes(nodes)
        self.sel_dialog.show()
        self.sel_dialog.raise_()
        self.sel_dialog.activateWindow()
        self.status.showMessage("多选窗口已列出 %d 项" % len(nodes))

    def _refresh_selection_dialog(self):
        if self.sel_dialog is not None:
            self.sel_dialog.set_nodes(self.selected_nodes())

    # ---------------------------------------------------------------- 复制路径
    def copy_selected_paths(self, *_):
        return self.copy_paths_of(self.selected_nodes())

    def copy_paths_of(self, nodes):
        nodes = list(nodes or [])
        if not nodes:
            self.status.showMessage("没有选中任何项目，未复制")
            return 0
        paths = node_paths(nodes)
        QApplication.clipboard().setText(CLIPBOARD_SEP.join(paths))
        self.status.showMessage("已复制 %d 条完整路径到剪贴板" % len(paths))
        return len(paths)

    # -------------------------------------------------------------- 打开位置
    def reveal_selected(self, *_):
        return self.reveal_nodes(self.selected_nodes())

    def reveal_nodes(self, nodes):
        nodes = list(nodes or [])
        if not nodes:
            self.status.showMessage("没有选中任何项目")
            return 0

        exists, missing = split_existing(nodes)
        if not exists:
            self._warn("无法定位",
                       "选中的 %d 项在磁盘上都不存在了（可能已被删除或移动）。\n"
                       "建议重新扫描以刷新列表。" % len(nodes))
            self.status.showMessage("打开所在位置失败：目标已不存在")
            return 0

        # 同一个目录只开一次窗口，避免选中几十个文件时弹出一堆资源管理器
        groups, order = {}, []
        for n in exists:
            par = n.path if n.is_dir else (os.path.dirname(n.path) or n.path)
            if par not in groups:
                groups[par] = n
                order.append(par)

        opened, errors = 0, []
        for par in order[:MAX_REVEAL_DIRS]:
            good, err = reveal_in_explorer(groups[par].path)
            if good:
                opened += 1
            else:
                errors.append(err)

        parts = ["已打开 %d 个位置" % opened]
        if len(order) > MAX_REVEAL_DIRS:
            parts.append("另有 %d 个不同目录未打开（避免弹出过多窗口）"
                         % (len(order) - MAX_REVEAL_DIRS))
        if missing:
            parts.append("%d 项已不存在，已跳过" % len(missing))
        if errors:
            parts.append("失败：%s" % errors[0])
        self.status.showMessage("；".join(parts))
        return opened

    # ------------------------------------------------------------------ 删除
    def delete_selected(self, *_):
        return self.delete_nodes(self.selected_nodes())

    def delete_nodes(self, nodes):
        """
        删除流程：
          未选中            -> 直接返回，不弹框
          全部已不存在      -> 提示，不弹确认框
          其余              -> 弹确认框（默认回收站、默认焦点在取消、量大需键入数量）
          确认后            -> 执行删除，并以「磁盘上是否还在」为准判定结果
          删除成功          -> 从内存树里摘掉节点，重新聚合、重排、重筛
        返回实际删除成功的项数；未执行时返回 None。
        """
        nodes = list(nodes or [])
        if not nodes:
            self.status.showMessage("没有选中任何项目，未执行删除")
            return None

        exists, missing = split_existing(nodes)
        if not exists:
            self._warn("无法删除",
                       "选中的 %d 项在磁盘上都不存在了（可能已被删除或移动）。\n"
                       "建议重新扫描以刷新列表。" % len(nodes))
            self.status.showMessage("删除未执行：目标已不存在")
            return None

        dlg = ConfirmDeleteDialog(exists, missing_count=len(missing), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.status.showMessage("已取消删除，未改动任何文件")
            return None

        paths = node_paths(exists)
        aborted = False
        if dlg.mode == "recycle":
            submitted, aborted, err = send_to_recycle_bin(paths)
            if not submitted:
                self._warn("删除失败",
                           "移入回收站失败：%s\n\n"
                           "常见原因：路径过长（超过 %d 字符）、权限不足，"
                           "或文件被其他程序占用。" % (err, LONG_PATH_WARN))
                self.status.showMessage("删除失败：%s" % err)
                return None
        else:
            delete_permanently(paths)

        # 一律以"磁盘上是否还在"为准判定成效，而不是只看 API 的返回码：
        # 批量删除可能部分是成功的（尤其是被中途取消时）
        gone_set = set(p for p in paths if not path_exists(p))
        gone_nodes = [n for n in exists if n.path in gone_set]

        if gone_nodes:
            remove_nodes(gone_nodes)
            if self.model is not None:
                aggregate_sizes(self.model.root)
                self.refresh_view(force=True)
            self._update_selection_ui()

        still = [p for p in paths if p not in gone_set]
        parts = ["已删除 %d 项" % len(gone_set)]
        if still:
            parts.append("%d 项未能删除" % len(still))
        if aborted:
            parts.append("操作被中途取消")
        if missing:
            parts.append("%d 项此前已不存在" % len(missing))
        self.status.showMessage("；".join(parts))

        if still:
            self._warn("部分删除失败",
                       "有 %d 项未能删除：\n%s%s\n\n"
                       "常见原因：文件被其他程序占用、权限不足、"
                       "或路径过长（可改用永久删除）。"
                       % (len(still), "\n".join(still[:8]),
                          "\n……" if len(still) > 8 else ""))

        # 多选窗口若开着，同步成删除后剩下的项
        if self.sel_dialog is not None and self.sel_dialog.isVisible():
            self.sel_dialog.set_nodes([n for n in exists if n.path not in gone_set])

        return len(gone_set)

    # ------------------------------------------------------------------ 杂项
    def _warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def on_metric_changed(self, _index: int):
        """切换指标后立即重绘，并同步模型的文本层"""
        metric = self.current_metric()
        self.delegate.metric = metric
        if self.model is not None:
            self.model.metric = metric
        self.update_legend()
        self.tree.viewport().update()

    def on_style_changed(self, _index: int):
        """切换行内样式后立即重绘"""
        self.delegate.style_mode = self.current_style()
        self.tree.viewport().update()

    def clear_model(self):
        """
        断开并释放旧模型。
        模型不交给 view 做 parent（view 不拥有模型），这样重复扫描时
        上一棵树会被 Python 正常回收，而不是一直挂在 view 下面吃内存。
        """
        self.tree.setModel(None)
        self.model = None
        self._update_selection_ui()

    def start_scan(self):
        if self.controller.running:
            return
        path = self.drive_box.currentText().strip()
        if not path:
            self.status.showMessage("没有可扫描的磁盘")
            return

        max_nodes = self.nodes_box.value()
        # 新的一棵树：清掉排序/筛选缓存与展开记录（同时也释放对旧节点的引用）
        self._sort_sig = None
        self._view_sig = None
        self._expanded_nodes = set()
        self._scan_summary = ""
        self.clear_model()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.drive_box.setEnabled(False)
        self.progress.setVisible(True)
        # 卷根扫描能用"卷已用字节"当真实分母；子目录扫描只能估算
        self._volume_scan = is_volume_root(path)
        self.tracker.reset()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.progress_label.setText("  准备中…  ")
        self.status.showMessage("正在扫描 %s（节点上限 %s）"
                                % (path, f"{max_nodes:,}"))
        self.timer.start()
        self.controller.start(path, max_nodes)

    def stop_scan(self):
        if self.controller.running:
            self.controller.stop()
            self.timer.stop()
            self.progress.setVisible(False)
            self.progress_label.setText("")
            self.status.showMessage("已手动停止（结果未聚合）")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.drive_box.setEnabled(True)

    def refresh_progress(self):
        """
        进度刷新：把引擎快照换算成百分比与一排实时数字。
        进度条的百分比来自 estimate_scan_percent（按能否拿到真实分母自适应），
        再经 ProgressTracker 做单调化与速率平滑。
        """
        snap = self.controller.engine.snapshot()
        pct, basis = estimate_scan_percent(snap, self._volume_scan)
        if pct is None:
            # 拿不到可信分母（扫描子目录）：显示不确定进度，绝不编造百分比
            if self.progress.maximum() != 0:
                self.progress.setRange(0, 0)
            self.progress.setFormat("扫描中")
        else:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 1000)
            shown = self.tracker.update(pct, snap["dirs"] + snap["files"], snap)
            self.progress.setValue(int(round(shown * 10)))
            self.progress.setFormat("%.1f%% · %s" % (shown, basis))

        bits = []
        if snap["disk_used"]:
            bits.append("%s / %s" % (fmt_size(snap["bytes"]),
                                     fmt_size(snap["disk_used"])))
        else:
            bits.append(fmt_size(snap["bytes"]))
        bits.append("目录 %s" % f"{snap['dirs']:,}")
        bits.append("文件 %s" % f"{snap['files']:,}")
        if snap["skipped"]:
            bits.append("跳过 %s" % f"{snap['skipped']:,}")
        if self.tracker.rate > 0:
            bits.append("%s 项/秒" % f"{int(self.tracker.rate):,}")
        bits.append("已用 %s" % fmt_duration(snap["elapsed"]))
        eta = self.tracker.eta_seconds(snap, self._volume_scan)
        if eta is not None:
            bits.append("剩余约 %s" % fmt_duration(eta))
        self.progress_label.setText("   " + "   ".join(bits) + "   ")

        cur = snap["current"] or self.drive_box.currentText()
        if len(cur) > 64:
            cur = "…" + cur[-63:]
        tail = ""
        if snap["truncated"]:
            tail = "   [已达节点上限，正在收尾]"
        self.status.showMessage("%s ▸ %s%s" % (basis, cur, tail))

    # ------------------------------------------------------------------ 完成
    def on_finished(self, root):
        self.timer.stop()
        # 收尾时把进度补满再撤下，避免"停在 97%"的错觉
        if self.progress.isVisible():
            self.progress.setValue(1000)
            self.progress.setFormat("100.0% · 完成")
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.drive_box.setEnabled(True)

        e = self.controller.engine
        if root is None:
            self.status.showMessage(e.error or "扫描失败")
            return

        t0 = time.time()
        self.model = TreeModel(root, metric=self.current_metric())
        self.tree.setModel(self.model)
        sm = self.tree.selectionModel()
        if sm is not None:
            sm.selectionChanged.connect(self.on_selection_changed)
        self.tree.expandToDepth(0)

        self._scan_summary = (
            "完成  %s   总计 %s   目录 %s   文件 %s   跳过 %s（无权限/链接）"
            % (root.name, fmt_size(root.size), f"{e.dirs_scanned:,}",
               f"{e.files_found:,}", f"{e.skipped:,}"))
        if e.truncated:
            self._scan_summary += "   [已达节点上限 %s，结果被截断]" % f"{e.max_nodes:,}"

        # 排序 + 应用当前筛选条件（并把展开状态恢复回来）
        stats = self.refresh_view(force=True)
        t_view = time.time() - t0

        if stats is not None:
            n_files, n_dirs, n_hidden = stats
            self._scan_summary += ("   可见 %s 文件 / %s 目录"
                                   % (f"{n_files:,}", f"{n_dirs:,}"))
        self.status.showMessage("%s   视图 %.2fs" % (self._scan_summary, t_view))


# ==============================================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("磁盘扫描可视化")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
