# -*- coding: utf-8 -*-
"""
scanner.py 离屏验证脚本
================================================================================
覆盖：
  A 纯逻辑（时间/体积分档、格式化）边界值
  B 扫描引擎：真实目录、聚合、排序、无权限目录跳过
  C 树模型：index/parent/rowCount/data 的 role 行为
  D 委托绘制：真实调用 paint()，再逐像素断言"同色/异色/顺序"
  E 委托健壮性：极窄行、空节点
运行： QT_QPA_PLATFORM=offscreen python test_scanner.py
================================================================================
"""

import os
import sys
import io
import time
import shutil
import stat as stat_mod
import tempfile
import threading
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scanner as S                                    # noqa: E402
from PyQt6.QtCore import (Qt, QRect, QModelIndex, QObject,   # noqa: E402
                          QItemSelectionModel)
from PyQt6.QtGui import QColor, QFont, QPalette, QPixmap, QPainter  # noqa: E402
from PyQt6.QtWidgets import (                          # noqa: E402
    QApplication, QTreeView, QStyleOptionViewItem, QStyle,
)

FAILED = []
PASSED = 0


def check(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [OK]   %s %s" % (name, extra))
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, extra))


# ==============================================================================
# A 纯逻辑边界
# ==============================================================================

def test_logic():
    print("\n=== A 纯逻辑边界 ===")
    now = time.time()
    d = 86400.0

    cases = [
        (0.0, "fresh", "刚刚修改"),
        (6.9 * d, "fresh", "6.9 天"),
        (7.0 * d, "fresh", "正好 7 天 -> 绿(含)"),
        (7.01 * d, "mid", "7.01 天 -> 黄"),
        (180.0 * d, "mid", "正好 180 天 -> 黄(含)"),
        (180.5 * d, "old", "180.5 天 -> 红"),
        (1000 * d, "old", "1000 天 -> 红"),
    ]
    for age, want, desc in cases:
        got = S.age_bucket(now - age, now)
        check("时间分档 %s" % desc, got == want, "-> %s" % got)

    gb = 1024 ** 3
    scases = [
        (0, "fresh"), (1024, "fresh"),
        (gb - 1, "fresh", ), (gb, "mid"), (2 * gb, "mid"),
        (3 * gb - 1, "mid"), (3 * gb, "old"), (10 * gb, "old"),
    ]
    for size, want in scases:
        got = S.size_bucket(size)
        check("体积分档 %s" % S.fmt_size(size), got == want, "-> %s" % got)

    # 体积格式对齐参考样式：4.2GB / 0.8GB / 10.2GB（无空格，一位小数）
    check("fmt_size 参考样式 4.2GB",
          S.fmt_size(int(4.2 * gb)) == "4.2GB", "-> %s" % S.fmt_size(int(4.2 * gb)))
    check("fmt_size 参考样式 2.5GB",
          S.fmt_size(int(2.5 * gb)) == "2.5GB", "-> %s" % S.fmt_size(int(2.5 * gb)))
    check("fmt_size 参考样式 10.2GB",
          S.fmt_size(int(10.2 * gb)) == "10.2GB", "-> %s" % S.fmt_size(int(10.2 * gb)))
    # 不足 1GB 时自动降到 MB：固定用 GB 会让 2KB 的文件显示成 0.0GB，丢失信息
    check("fmt_size 不足 1GB 自动降级为 MB",
          S.fmt_size(int(0.8 * gb)) == "819.2MB",
          "-> %s" % S.fmt_size(int(0.8 * gb)))
    check("fmt_size 无多余空格", " " not in S.fmt_size(int(3.5 * gb)),
          "-> %r" % S.fmt_size(int(3.5 * gb)))
    check("fmt_size 小值", S.fmt_size(512) == "512B", "-> %s" % S.fmt_size(512))
    check("fmt_date 格式", len(S.fmt_date(time.time())) == 10,
          "-> %s" % S.fmt_date(time.time()))
    check("配色表齐全",
          set(S.BUCKET_COLORS) == {"fresh", "mid", "old"})


# ==============================================================================
# B 扫描引擎
# ==============================================================================

def build_tree(base):
    """构造测试用目录树，返回 (root, 期望的总字节数)"""
    total = 0
    os.makedirs(os.path.join(base, "sub", "deep"))
    os.makedirs(os.path.join(base, "denied"))
    os.makedirs(os.path.join(base, "empty"))

    specs = [
        ("a.txt", 1000),
        ("b.bin", 2000),
        (os.path.join("sub", "c.dat"), 3000),
        (os.path.join("sub", "deep", "d.log"), 4000),
        (os.path.join("denied", "secret.txt"), 5000),   # 会被"拒绝访问"
    ]
    for rel, size in specs:
        p = os.path.join(base, rel)
        with open(p, "wb") as f:
            f.write(b"\0" * size)
        if not rel.startswith("denied"):
            total += size
    return total


def test_scan():
    print("\n=== B 扫描引擎 ===")
    base = tempfile.mkdtemp(prefix="scan_test_")
    try:
        expect_total = build_tree(base)

        # 模拟"无权限目录"：让 os.scandir 对 denied 抛 PermissionError
        real_scandir = os.scandir

        def fake_scandir(path, *a, **kw):
            if os.path.basename(str(path)) == "denied":
                raise PermissionError(13, "Access is denied")
            return real_scandir(path, *a, **kw)

        os.scandir = fake_scandir
        try:
            eng = S.ScanEngine()
            root = eng.scan(base)
        finally:
            os.scandir = real_scandir

        check("扫描返回根节点", root is not None)
        if root is None:
            return

        check("跳过计数 >=1（无权限目录）", eng.skipped >= 1,
              "-> skipped=%d" % eng.skipped)
        check("文件计数 == 4", eng.files_found == 4,
              "-> %d" % eng.files_found)
        check("目录计数 == 4（含 denied）", eng.dirs_scanned == 4,
              "-> %d" % eng.dirs_scanned)

        check("聚合体积正确", root.size == expect_total,
              "-> %d (期望 %d)" % (root.size, expect_total))

        names = {c.name for c in root.children}
        check("顶层节点齐全", names == {"sub", "denied", "empty", "a.txt", "b.bin"},
              "-> %s" % sorted(names))

        denied = [c for c in root.children if c.name == "denied"][0]
        check("无权限目录被保留为空节点", denied.size == 0 and not denied.children)

        sub = [c for c in root.children if c.name == "sub"][0]
        check("子目录递归聚合 = 7000", sub.size == 7000, "-> %d" % sub.size)

        # 排序：体积降序
        sizes = [c.size for c in root.children]
        check("同级按体积降序", sizes == sorted(sizes, reverse=True), "-> %s" % sizes)

        # 行下标回写
        ok = all(c.row == i for i, c in enumerate(root.children))
        check("row 下标回写正确", ok)

        # path 回溯
        deep = [c for c in sub.children if c.name == "deep"][0]
        check("path 回溯", os.path.normcase(deep.path) ==
              os.path.normcase(os.path.join(base, "sub", "deep")),
              "-> %s" % deep.path)

        # 不存在的路径不应抛异常
        eng2 = S.ScanEngine()
        r2 = eng2.scan(os.path.join(base, "no_such_dir_xyz"))
        check("不存在的路径安全返回", r2 is None and eng2.error is not None,
              "-> %s" % eng2.error)

        return root
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ==============================================================================
# C 树模型
# ==============================================================================

def test_model(root):
    print("\n=== C 树模型 ===")
    if root is None:
        return None
    model = S.TreeModel(root)

    check("顶层行数 == 子节点数", model.rowCount(QModelIndex()) == len(root.children))
    check("columnCount == 1", model.columnCount() == 1)

    idx0 = model.index(0, 0, QModelIndex())
    check("index 有效", idx0.isValid())
    node0 = idx0.data(S.NODE_ROLE)
    check("NODE_ROLE 返回 Node", isinstance(node0, S.Node))
    check("DisplayRole 返回文本", isinstance(idx0.data(Qt.ItemDataRole.DisplayRole), str))
    check("ToolTip 含路径", node0.path in (idx0.data(Qt.ItemDataRole.ToolTipRole) or ""))
    check("flags 可选中",
          bool(idx0.flags() & Qt.ItemFlag.ItemIsSelectable))

    sub_idx = None
    for i in range(model.rowCount()):
        it = model.index(i, 0, QModelIndex())
        if it.data(S.NODE_ROLE).name == "sub":
            sub_idx = it
            break
    if sub_idx is not None:
        check("hasChildren 对目录为真", model.hasChildren(sub_idx))
        child_idx = model.index(0, 0, sub_idx)
        check("子节点 index 有效", child_idx.isValid())
        p = model.parent(child_idx)
        check("parent 指回 sub", p.isValid() and p.data(S.NODE_ROLE).name == "sub")
    check("无效 index 安全", model.data(QModelIndex()) is None)
    return model


# ==============================================================================
# D / E 委托绘制（逐像素）
# ==============================================================================

W, H = 1200, 30   # 模拟最大化窗口的可用宽度


_ROWS = []   # 必须持有引用：QModelIndex 里存的是裸指针，
             # Python 侧 Node 一旦被 GC，index.data() 就会访问已释放内存（段错误）


class _Row:
    """把一个 Node 包成单行模型，产出真实 QModelIndex"""

    def __init__(self, node):
        root = S.Node("<test-root>", True)
        root.children = [node]
        node.parent = root
        node.row = 0
        self.root = root                 # 显式持有，防止被提前回收
        self.model = S.TreeModel(root)
        self.index = self.model.index(0, 0, QModelIndex())
        _ROWS.append(self)


def render(delegate, row, width=W, height=H):
    """把一行真实地画到 QPixmap 上，返回 QImage"""
    pm = QPixmap(width, height)
    pm.fill(QColor("#ffffff"))

    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, width, height)
    opt.state = QStyle.StateFlag.State_Enabled
    opt.font = QFont("Microsoft YaHei", 14)
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Text, QColor("#000000"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#000000"))
    opt.palette = pal

    painter = QPainter(pm)
    try:
        delegate.paint(painter, opt, row.index)
    finally:
        painter.end()
    return pm.toImage()


def near(c1, c2, tol):
    return (abs(c1.red() - c2.red()) <= tol and
            abs(c1.green() - c2.green()) <= tol and
            abs(c1.blue() - c2.blue()) <= tol)


def scan_pixels(img, target, tol=55):
    """返回命中该颜色的像素 x 坐标集合"""
    tcol = QColor(target)
    xs = set()
    for y in range(img.height()):
        for x in range(img.width()):
            if near(img.pixelColor(x, y), tcol, tol):
                xs.add(x)
    return xs


def scan_gray_pixels(img, lo=95, hi=215):
    """识别"中性灰"像素：R≈G≈B 且亮度在区间内（小字号抗锯齿会大量偏白，
    所以不能用固定色距匹配）"""
    xs = set()
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            if abs(r - g) <= 12 and abs(g - b) <= 12 and lo <= r <= hi:
                xs.add(x)
    return xs


def test_delegate():
    print("\n=== D 委托绘制（逐像素断言）===")
    view = QTreeView()
    delegate = S.DiskScanDelegate(view)

    now = time.time()
    day = 86400.0
    gb = 1024 ** 3

    GREEN = S.BUCKET_COLORS["fresh"]
    YELLOW = S.BUCKET_COLORS["mid"]
    RED = S.BUCKET_COLORS["old"]
    GRAY = S.DETAIL_COLOR

    # 案例1：近期修改 + 小体积 -> 前段绿、后段绿
    img1 = render(delegate, _Row(S.Node("fresh_small.txt", False, now - 1 * day, 500 * 1024 ** 2)))
    g1 = scan_pixels(img1, GREEN)
    r1 = scan_pixels(img1, RED)
    check("案例1 有绿色像素", len(g1) > 0, "-> %d 像素" % len(g1))
    check("案例1 无红色像素", len(r1) == 0, "-> %d 像素" % len(r1))
    check("案例1 前段从最左侧开始（时间在名字前）", g1 and min(g1) < 60,
          "-> min_x=%s" % (min(g1) if g1 else None))

    # 案例2：陈旧修改 + 超大体积 -> 前段红、后段红
    img2 = render(delegate, _Row(S.Node("old_huge.iso", False, now - 1000 * day, 5 * gb)))
    r2 = scan_pixels(img2, RED)
    g2 = scan_pixels(img2, GREEN)
    check("案例2 有红色像素", len(r2) > 0, "-> %d 像素" % len(r2))
    check("案例2 无绿色像素", len(g2) == 0, "-> %d 像素" % len(g2))
    check("案例2 前段从最左侧开始", r2 and min(r2) < 60,
          "-> min_x=%s" % (min(r2) if r2 else None))

    # 案例3：混合 -> 前段绿(时间新) + 后段红(体积大)，两段必须同现且前段在左
    img3 = render(delegate, _Row(S.Node("mixed.bin", False, now - 2 * day, 4 * gb)))
    g3 = scan_pixels(img3, GREEN)
    r3 = scan_pixels(img3, RED)
    gray3 = scan_gray_pixels(img3)
    check("案例3 同现绿+红两段", bool(g3) and bool(r3),
          "-> 绿 %d px / 红 %d px" % (len(g3), len(r3)))
    check("案例3 绿色段在红色段左侧（前段=时间，后段=体积）",
          bool(g3) and bool(r3) and max(g3) < min(r3),
          "-> 绿 max_x=%s < 红 min_x=%s" % (max(g3) if g3 else None,
                                            min(r3) if r3 else None))
    check("案例3 有灰色补充信息", len(gray3) > 0, "-> %d 像素" % len(gray3))
    check("案例3 灰色补充在红色段右侧",
          bool(gray3) and bool(r3) and min(gray3) > max(r3),
          "-> 灰 min_x=%s > 红 max_x=%s" % (min(gray3) if gray3 else None,
                                            max(r3) if r3 else None))

    # 案例4：中档 -> 黄
    img4 = render(delegate, _Row(S.Node("mid_mid.dat", False, now - 30 * day, 2 * gb)))
    y4 = scan_pixels(img4, YELLOW)
    check("案例4 有黄色像素（7~180天/1~3GB）", len(y4) > 0, "-> %d 像素" % len(y4))

    # 案例5：目录加粗显示
    img5 = render(delegate, _Row(S.Node("SomeDir", True, now - 400 * day, 8 * gb)))
    r5 = scan_pixels(img5, RED)
    check("案例5 目录也能着色", len(r5) > 0, "-> %d 像素" % len(r5))

    print("\n=== E 委托健壮性 ===")
    tiny = _Row(S.Node("narrow.txt", False, now, 0))
    for w, desc in ((20, "极窄 20px"), (60, "窄 60px"), (120, "窄 120px")):
        try:
            render(delegate, tiny, width=w, height=H)
            check("paint 不崩溃 (%s)" % desc, True)
        except Exception as e:                          # noqa: BLE001
            check("paint 不崩溃 (%s)" % desc, False, "-> %r" % e)

    try:
        idx = _Row(S.Node("ok", False, now, 1)).index
        canvas = QPixmap(10, 10)
        painter = QPainter(canvas)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 10, 10)
        opt.font = QFont("Microsoft YaHei", 14)
        delegate.paint(painter, opt, idx)
        painter.end()
        check("paint 极小画布不崩溃", True)
    except Exception as e:                              # noqa: BLE001
        check("paint 极小画布不崩溃", False, "-> %r" % e)

    # 极端时间戳
    try:
        render(delegate, _Row(S.Node("bad", False, 0.0, 0)))
        check("时间戳 0 不崩溃", True)
    except Exception as e:                              # noqa: BLE001
        check("时间戳 0 不崩溃", False, "-> %r" % e)


# ==============================================================================
# F 主窗口端到端（构建真实 MainWindow，走完整扫描流程）
# ==============================================================================

def test_mainwindow():
    print("\n=== F 主窗口端到端 ===")
    S.AUTO_SCAN = False                       # 关掉启动即扫，避免误扫真实磁盘

    base = tempfile.mkdtemp(prefix="win_test_")
    try:
        build_tree(base)

        win = S.MainWindow()
        win.timer.stop()                      # 避免测试期间刷新状态栏
        check("窗口可构建", win is not None)
        check("下拉框含盘符", win.drive_box.count() > 0,
              "-> %d 个" % win.drive_box.count())
        check("初始无模型", win.tree.model() is None)
        check("初始按钮状态正确",
              win.btn_start.isEnabled() and not win.btn_stop.isEnabled())

        # 直接驱动控制器（绕过下拉框，因为它只放真实盘符）
        win.controller.start(base)
        deadline = time.time() + 20
        while time.time() < deadline:
            QApplication.processEvents()
            if win.tree.model() is not None:
                break
            time.sleep(0.02)

        model = win.tree.model()
        check("扫描完成后模型已挂载", model is not None)
        if model is not None:
            n = model.rowCount(QModelIndex())
            check("顶层有数据行", n >= 4, "-> %d 行" % n)
            first = model.index(0, 0, QModelIndex())
            check("首行能取到 Node", isinstance(first.data(S.NODE_ROLE), S.Node))
            check("展开到第一层", win.tree.isExpanded(first) is not None)

        msg = win.status.currentMessage()
        check("状态栏显示完成", "完成" in msg, "-> %s" % msg)
        check("状态栏含统计", "跳过" in msg and "总计" in msg)
        check("按钮状态已复位",
              win.btn_start.isEnabled() and not win.btn_stop.isEnabled())

        # 新增的可调参数控件
        check("节点上限控件存在且默认正确",
              win.nodes_box.value() == S.MAX_NODES, "-> %d" % win.nodes_box.value())
        check("节点上限可调范围足够大", win.nodes_box.maximum() >= 10_000_000,
              "-> 上限 %s" % f"{win.nodes_box.maximum():,}")
        check("前段指标控件有 %d 项" % len(S.METRIC_CHOICES),
              win.metric_box.count() == len(S.METRIC_CHOICES))
        check("界面默认指标 = 最后访问时间",
              win.current_metric() == S.DEFAULT_METRIC == S.METRIC_ATIME,
              "-> %s" % win.current_metric())
        check("行内样式控件存在", win.style_box.count() == len(S.ROW_STYLE_CHOICES))
        check("界面默认样式 = 参考形状（色标）",
              win.current_style() == S.DEFAULT_ROW_STYLE == S.ROW_STYLE_MARKS,
              "-> %s" % win.current_style())
        check("委托初始样式与界面一致",
              win.delegate.style_mode == S.ROW_STYLE_MARKS,
              "-> %s" % win.delegate.style_mode)

        win.style_box.setCurrentIndex(win.style_box.findData(S.ROW_STYLE_VALUES))
        check("切换样式后委托同步", win.delegate.style_mode == S.ROW_STYLE_VALUES,
              "-> %s" % win.delegate.style_mode)
        win.style_box.setCurrentIndex(win.style_box.findData(S.ROW_STYLE_MARKS))
        check("样式可切回色标", win.delegate.style_mode == S.ROW_STYLE_MARKS)

        atime_idx = win.metric_box.findData(S.METRIC_MTIME)
        win.metric_box.setCurrentIndex(atime_idx)          # 会触发信号
        check("切换指标后委托同步", win.delegate.metric == S.METRIC_MTIME,
              "-> %s" % win.delegate.metric)
        mdl = win.tree.model()
        check("切换指标后模型同步", mdl is not None and mdl.metric == S.METRIC_MTIME)
        win.metric_box.setCurrentIndex(win.metric_box.findData(S.METRIC_ATIME))
        check("指标可切回访问时间", win.current_metric() == S.METRIC_ATIME)
        check("图例跟随指标（访问）", "7天内访问" in win.legend.text(),
              "-> %s" % win.legend.text()[:40])
        win.metric_box.setCurrentIndex(win.metric_box.findData(S.METRIC_MTIME))
        check("图例跟随指标（修改）", "7天内修改" in win.legend.text())
        win.metric_box.setCurrentIndex(win.metric_box.findData(S.METRIC_ATIME))

        # 重复扫描时旧模型必须断开并释放，否则每扫一次就多留一整棵树在内存里
        win.model = S.TreeModel(S.Node("<probe>", True))
        win.tree.setModel(win.model)
        win.clear_model()
        check("clear_model 断开并释放旧模型",
              win.model is None and win.tree.model() is None)
        win.model = S.TreeModel(S.Node("<probe2>", True))
        win.tree.setModel(win.model)
        # TreeModel 自己重写了 parent(index)，这里要显式走 QObject 的原方法
        check("模型由 Python 侧持有（view 不做 parent）",
              QObject.parent(win.model) is None)

        # 上限参数必须真的下传到引擎
        win.nodes_box.setValue(123456)
        win.controller.start(base, win.nodes_box.value())
        deadline = time.time() + 20
        while time.time() < deadline and win.controller.running:
            QApplication.processEvents()
            time.sleep(0.02)
        for _ in range(50):
            QApplication.processEvents()
            time.sleep(0.01)
        check("节点上限下传到引擎",
              win.controller.engine.max_nodes == 123456,
              "-> %d" % win.controller.engine.max_nodes)
        win.nodes_box.setValue(S.MAX_NODES)

        # ---------------- 筛选 / 排序 控件联动（用合成树，体积可控） ----
        check("筛选控件齐全",
              all(hasattr(win, n) for n in
                  ("size_min_box", "size_max_box", "days_min_box",
                   "days_max_box", "keep_parents_box", "btn_reset", "sort_box")))
        check("区间框默认 0 = 不限",
              win.size_min_box.value() == 0 and win.size_max_box.value() == 0
              and win.days_min_box.value() == 0 and win.days_max_box.value() == 0)
        check("区间框在最小值显示「不限」",
              win.size_min_box.text() == "不限" and win.days_max_box.text() == "不限",
              "-> %r" % win.size_min_box.text())
        check("默认保留上级路径", win.keep_parents_box.isChecked())
        check("排序控件含全部方式",
              win.sort_box.count() == len(S.SORT_MODES)
              and win.current_sort() == S.DEFAULT_SORT,
              "-> %d 项, 默认 %s" % (win.sort_box.count(), win.current_sort()))

        # 造一棵体积可控的树塞进窗口
        nw = time.time()
        t_root = S.Node("<t>", True, nw, 0)
        d_big = S.Node("Big", True, nw - 5 * 86400, int(2.5 * S.GB), t_root,
                       atime=nw - 5 * 86400)
        t_root.children.append(d_big)
        f_hit = S.Node("hit.bin", False, nw - 5 * 86400, int(2.0 * S.GB), d_big,
                       atime=nw - 5 * 86400)
        d_big.children.append(f_hit)
        d_huge = S.Node("Huge", True, nw - 500 * 86400, int(8.0 * S.GB), t_root,
                        atime=nw - 500 * 86400)
        t_root.children.append(d_huge)
        f_tiny = S.Node("tiny.txt", False, nw - 86400, 1024, t_root,
                        atime=nw - 86400)
        t_root.children.append(f_tiny)

        win.clear_model()
        win._sort_sig = None
        win._view_sig = None
        win._expanded_nodes = set()
        win.model = S.TreeModel(t_root)
        win.tree.setModel(win.model)

        idx_big = win.model.index(0, 0, QModelIndex())
        win.tree.expand(idx_big)
        check("合成树已展开 Big", win.tree.isExpanded(idx_big))
        check("展开记录已登记", d_big in win._expanded_nodes)

        # 设 1.0 ~ 3.0 GB
        win.size_min_box.setValue(1.0)
        win.size_max_box.setValue(3.0)
        win.filter_timer.stop()
        stats = win.refresh_view(force=True)
        names = {n.name for n in _model_nodes(win.model)}
        check("界面筛选生效", names == {"Big", "hit.bin"}, "-> %s" % sorted(names))
        check("界面筛选返回值正确",
              stats is not None and stats[0] == 1 and stats[1] == 1,
              "-> %s" % (stats,))
        check("隐藏计数含整棵子树",
              stats is not None and stats[2] == 2, "-> 隐藏 %s" % (stats[2],))
        idx_big2 = win.model.index(0, 0, QModelIndex())
        check("筛选后 Big 仍处于展开状态", win.tree.isExpanded(idx_big2))
        check("筛选后能读到子项", win.model.rowCount(idx_big2) == 1)

        # 严格模式：换一个"只有子项命中"的区间
        # Big=2.5GB 不在范围内，hit.bin=2.0GB 在范围内
        win.size_min_box.setValue(1.5)
        win.size_max_box.setValue(2.2)
        win.filter_timer.stop()
        win.refresh_view(force=True)
        names = {n.name for n in _model_nodes(win.model)}
        check("保留上级路径时深层子项可达", names == {"Big", "hit.bin"},
              "-> %s" % sorted(names))

        win.keep_parents_box.setChecked(False)
        win.filter_timer.stop()
        win.refresh_view(force=True)
        names = {n.name for n in _model_nodes(win.model)}
        check("严格模式下整枝隐藏", names == set(), "-> %s" % sorted(names))
        win.keep_parents_box.setChecked(True)

        # 排序联动
        win.sort_box.setCurrentIndex(win.sort_box.findData("name_desc"))
        win.filter_timer.stop()
        win.refresh_view(force=True)
        win.reset_filters()
        top = [win.model.index(i, 0, QModelIndex()).data(S.NODE_ROLE).name
               for i in range(win.model.rowCount())]
        check("界面排序生效（名称 Z→A）", top == ["tiny.txt", "Huge", "Big"],
              "-> %s" % top)

        # 一键重置
        check("重置后输入框归零",
              win.size_min_box.value() == 0 and win.size_max_box.value() == 0
              and win.days_min_box.value() == 0 and win.days_max_box.value() == 0)
        names = {n.name for n in _model_nodes(win.model)}
        check("重置后恢复完整树", len(names) == 4, "-> %s" % sorted(names))

        # 还原一次真实模型，避免影响后续用例
        win.clear_model()
        win.sort_box.setCurrentIndex(win.sort_box.findData(S.DEFAULT_SORT))
        win._sort_sig = None
        win._view_sig = None

        # 停止流程
        win.controller.start(base)
        QApplication.processEvents()
        win.stop_scan()
        check("停止后线程已回收", not win.controller.running)

        # 关键：手动停止后不得把半成品结果回传（否则界面会显示体积全 0 的残树）
        got = []
        ctrl = S.ScanController()
        ctrl.finished.connect(lambda r: got.append(r))
        ctrl._cancelled = True          # 模拟"用户已点停止"
        ctrl._run(base)                 # 然后扫描线程才结束
        check("取消后不回传半成品", got == [], "-> 回传了 %d 次" % len(got))

        # 对照组：未取消时必须回传
        got2 = []
        ctrl2 = S.ScanController()
        ctrl2.finished.connect(lambda r: got2.append(r))
        ctrl2._run(base)
        check("未取消时正常回传", len(got2) == 1 and got2[0] is not None)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ==============================================================================
# G 指标切换 + 节点上限
# ==============================================================================

def test_metric_and_cap():
    print("\n=== G 指标切换与节点上限 ===")
    now = time.time()
    day = 86400.0
    gb = 1024 ** 3

    n = S.Node("f.bin", False, now - 1 * day, int(2.5 * gb),
               atime=now - 300 * day)
    check("mtime 详情标签是「修改」",
          S.fmt_detail(n, S.METRIC_MTIME).startswith("(修改: "),
          "-> %s" % S.fmt_detail(n, S.METRIC_MTIME))
    check("atime 详情标签是「访问」",
          S.fmt_detail(n, S.METRIC_ATIME).startswith("(访问: "),
          "-> %s" % S.fmt_detail(n, S.METRIC_ATIME))
    check("详情里带体积", "大小: 2.5GB" in S.fmt_detail(n, S.METRIC_ATIME))
    check("metric_ts 按指标取值",
          S.metric_ts(n, S.METRIC_MTIME) == n.mtime and
          S.metric_ts(n, S.METRIC_ATIME) == n.atime)

    n0 = S.Node("g.bin", False, now - 1 * day, 100, atime=0.0)
    check("atime 为 0 时回退 mtime",
          S.metric_ts(n0, S.METRIC_ATIME) == n0.mtime)

    # 委托按指标着色：同一节点在两种指标下颜色应不同（绿 vs 红）
    view = QTreeView()
    delegate = S.DiskScanDelegate(view, metric=S.METRIC_MTIME)
    row = _Row(n)
    GREEN = S.BUCKET_COLORS["fresh"]
    RED = S.BUCKET_COLORS["old"]

    img_m = render(delegate, row)
    gm, rm = scan_pixels(img_m, GREEN), scan_pixels(img_m, RED)
    check("按修改时间着色 -> 前段命中绿(1天前)", len(gm) > 0 and len(rm) == 0,
          "-> 绿%d 红%d" % (len(gm), len(rm)))

    delegate.metric = S.METRIC_ATIME
    img_a = render(delegate, row)
    ga, ra = scan_pixels(img_a, GREEN), scan_pixels(img_a, RED)
    check("切到访问时间后 -> 前段命中红(300天前)", len(ra) > 0,
          "-> 绿%d 红%d" % (len(ga), len(ra)))
    check("两种指标渲染结果不同",
          (len(gm), len(rm)) != (len(ga), len(ra)))

    # 节点上限
    base = tempfile.mkdtemp(prefix="cap_test_")
    try:
        for i in range(40):
            with open(os.path.join(base, "f%02d.txt" % i), "wb") as f:
                f.write(b"x" * 10)
        eng = S.ScanEngine(max_nodes=10)
        root = eng.scan(base)
        check("引擎记录上限值", eng.max_nodes == 10, "-> %d" % eng.max_nodes)
        check("触顶标记 truncated", eng.truncated is True)
        check("触顶时不视为用户取消", eng.cancelled is False)
        check("触顶后仍然完成体积聚合", root is not None and root.size > 0,
              "-> root.size=%s" % (root.size if root else None))

        eng2 = S.ScanEngine(max_nodes=100000)
        eng2.scan(base)
        check("上限足够时不截断", eng2.truncated is False)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ==============================================================================
# H 行内样式（色标 / 带数值）
# ==============================================================================

def test_row_style():
    print("\n=== H 行内样式 ===")
    now = time.time()
    day = 86400.0
    gb = 1024 ** 3

    check("默认样式为参考形状（色标）",
          S.DEFAULT_ROW_STYLE == S.ROW_STYLE_MARKS, "-> %s" % S.DEFAULT_ROW_STYLE)
    check("默认指标为最后访问时间",
          S.DEFAULT_METRIC == S.METRIC_ATIME, "-> %s" % S.DEFAULT_METRIC)
    check("样式选项齐全", {k for k, _ in S.ROW_STYLE_CHOICES} ==
          {S.ROW_STYLE_MARKS, S.ROW_STYLE_VALUES})

    view = QTreeView()
    GREEN = S.BUCKET_COLORS["fresh"]
    RED = S.BUCKET_COLORS["old"]

    # 2 天前访问（绿）+ 4GB（红）
    node = S.Node("mixed.bin", False, now - 2 * day, int(4 * gb),
                  atime=now - 2 * day)
    row = _Row(node)

    d_marks = S.DiskScanDelegate(view, metric=S.METRIC_ATIME,
                                 style_mode=S.ROW_STYLE_MARKS)
    img_m = render(d_marks, row)
    g_m = scan_pixels(img_m, GREEN)
    r_m = scan_pixels(img_m, RED)
    gray_m = scan_gray_pixels(img_m)
    # 注意：scan_pixels 返回的是"去重后的 x 坐标集合"，即该颜色覆盖了多少列。
    # 色标是 ~11px 的方块，所以这里断言的是"列数落在方块宽度区间内"。
    span_m = (max(g_m) - min(g_m) + 1) if g_m else 0
    check("色标是紧凑方块（约 8~16 列宽）", 8 <= span_m <= 16,
          "-> 宽 %d 列" % span_m)
    cx = (min(g_m) + max(g_m)) // 2 if g_m else 0
    cy = img_m.height() // 2
    check("色标中心为实心填充",
          img_m.pixelColor(cx, cy).name().lower() == GREEN.lower(),
          "-> %s @(%d,%d)" % (img_m.pixelColor(cx, cy).name(), cx, cy))
    check("色标样式两档颜色不同且共存", len(g_m) > 0 and len(r_m) > 0,
          "-> 绿%d 红%d 列" % (len(g_m), len(r_m)))
    check("色标顺序：时间标在体积标左侧", bool(g_m) and bool(r_m) and max(g_m) < min(r_m),
          "-> 绿max=%s 红min=%s" % (max(g_m) if g_m else None,
                                    min(r_m) if r_m else None))
    check("色标样式仍带灰色括号", len(gray_m) > 0, "-> %d 列" % len(gray_m))

    d_vals = S.DiskScanDelegate(view, metric=S.METRIC_ATIME,
                                style_mode=S.ROW_STYLE_VALUES)
    img_v = render(d_vals, row)
    g_v = scan_pixels(img_v, GREEN)
    r_v = scan_pixels(img_v, RED)
    check("带数值样式也着色", len(g_v) > 0 and len(r_v) > 0,
          "-> 绿%d 红%d" % (len(g_v), len(r_v)))
    check("带数值样式的彩段明显更宽（装的是日期文字）",
          bool(g_v) and bool(g_m) and max(g_v) > max(g_m) * 3,
          "-> 数值样式 max_x=%s vs 色标 max_x=%s" % (max(g_v) if g_v else None,
                                                     max(g_m) if g_m else None))
    check("带数值样式也带灰色括号", len(scan_gray_pixels(img_v)) > 0)

    # 极窄行下两种样式都不能崩
    for mode in (S.ROW_STYLE_MARKS, S.ROW_STYLE_VALUES):
        d = S.DiskScanDelegate(view, metric=S.METRIC_ATIME, style_mode=mode)
        try:
            render(d, row, width=40, height=H)
            check("极窄行不崩溃 (%s)" % mode, True)
        except Exception as e:                              # noqa: BLE001
            check("极窄行不崩溃 (%s)" % mode, False, "-> %r" % e)


# ==============================================================================
# I 筛选
# ==============================================================================

def _mk(parent, name, is_dir, age_days, gb, now):
    """按"距今 N 天 / 体积 X GB"造节点，access 与 modify 用同一个时间"""
    ts = now - age_days * 86400
    n = S.Node(name, is_dir, ts, int(gb * S.GB), parent, atime=ts)
    parent.children.append(n)
    return n


def _fixture():
    """
    root
    ├─ Big        dir   2.5GB    5天
    │   ├─ a.bin  file  2.0GB    5天
    │   └─ Small  dir   0.5GB  400天
    │        └─ b.txt file 0.4GB 400天
    ├─ Huge       dir   5.0GB  500天
    │   └─ c.iso  file  4.5GB  500天
    └─ tiny.txt   file  0.001GB  1天
    """
    now = time.time()
    root = S.Node("<root>", True, now, 0)
    big = _mk(root, "Big", True, 5, 2.5, now)
    _mk(big, "a.bin", False, 5, 2.0, now)
    small = _mk(big, "Small", True, 400, 0.5, now)
    _mk(small, "b.txt", False, 400, 0.4, now)
    huge = _mk(root, "Huge", True, 500, 5.0, now)
    _mk(huge, "c.iso", False, 500, 4.5, now)
    _mk(root, "tiny.txt", False, 1, 0.001, now)
    for i, c in enumerate(root.children):
        c.row = i
    return root, now


def _model_nodes(model, parent=QModelIndex(), out=None):
    if out is None:
        out = []
    for r in range(model.rowCount(parent)):
        idx = model.index(r, 0, parent)
        node = idx.data(S.NODE_ROLE)
        out.append(node)
        if model.hasChildren(idx):
            _model_nodes(model, idx, out)
    return out


def test_filter():
    print("\n=== I 筛选 ===")

    # --- 判据边界 ---
    now = time.time()
    n2 = S.Node("x", False, now - 10 * 86400, 2 * S.GB, atime=now - 10 * 86400)
    check("区间内命中", S.NodeFilter(1.0, 3.0).matches(n2))
    check("低于下限被排除", not S.NodeFilter(2.5, 3.0).matches(n2))
    check("高于上限被排除", not S.NodeFilter(0.5, 1.5).matches(n2))
    check("边界含等号（下限）", S.NodeFilter(2.0, 3.0).matches(n2))
    check("边界含等号（上限）", S.NodeFilter(1.0, 2.0).matches(n2))
    check("None 表示不限", S.NodeFilter().matches(n2))
    check("空条件 is_active 为假", not S.NodeFilter().is_active)
    check("有条件 is_active 为真", S.NodeFilter(size_min=1.0).is_active)
    check("时间下限生效", not S.NodeFilter(days_min=30).matches(n2))
    check("时间上限生效", S.NodeFilter(days_max=5).matches(n2) is False)
    check("只限时间也命中", S.NodeFilter(days_min=5, days_max=20).matches(n2))

    nf = S.Node("future", False, now + 86400, 100, atime=now + 86400)
    check("未来时间按今天处理（不因负天数被排除）",
          S.NodeFilter(days_max=1).matches(nf))

    # --- 树结构保留 ---
    root, _ = _fixture()
    stats = S.apply_filter(root, S.NodeFilter(size_min=1.0, size_max=3.0), True)
    names = {n.name for n in _model_nodes(S.TreeModel(root))}
    check("按体积筛选：命中项可见", {"Big", "a.bin"} <= names,
          "-> %s" % sorted(names))
    check("按体积筛选：无命中后代的分支整体隐藏",
          "Huge" not in names and "c.iso" not in names)
    check("按体积筛选：目录下无命中项的也隐藏", "Small" not in names)
    check("统计 (文件,目录,隐藏)", stats == (1, 1, 5), "-> %s" % (stats,))

    # --- 保留上级路径 ---
    root, _ = _fixture()
    S.apply_filter(root, S.NodeFilter(size_min=0.3, size_max=0.45), True)
    names = {n.name for n in _model_nodes(S.TreeModel(root))}
    check("保留上级路径：深层命中项可达",
          names == {"Big", "Small", "b.txt"}, "-> %s" % sorted(names))

    root, _ = _fixture()
    S.apply_filter(root, S.NodeFilter(size_min=0.3, size_max=0.45), False)
    names = {n.name for n in _model_nodes(S.TreeModel(root))}
    check("严格模式：上级不匹配则整枝隐藏", names == set(),
          "-> %s" % sorted(names))

    # --- 时间筛选 ---
    root, _ = _fixture()
    S.apply_filter(root, S.NodeFilter(days_max=30), True)
    names = {n.name for n in _model_nodes(S.TreeModel(root))}
    check("按时间筛选（30 天内）", names == {"Big", "a.bin", "tiny.txt"},
          "-> %s" % sorted(names))

    # --- 内存优化：无隐藏项时 vlist 必须保持 None ---
    root, _ = _fixture()
    S.apply_filter(root, None, True)
    all_none = True
    stack = [root]
    while stack:
        n = stack.pop()
        if n.vlist is not None:
            all_none = False
        stack.extend(c for c in n.children if c.is_dir)
    check("无筛选时 vlist 全为 None（零额外内存）", all_none)
    check("取消筛选后全部可见",
          len(_model_nodes(S.TreeModel(root))) == 7,
          "-> %d" % len(_model_nodes(S.TreeModel(root))))

    # --- 模型一致性：rowCount/index/parent 与可见列表一致 ---
    root, _ = _fixture()
    S.apply_filter(root, S.NodeFilter(size_min=1.0, size_max=3.0), True)
    model = S.TreeModel(root)
    bad = []
    def _check_rows(parent=QModelIndex()):
        for r in range(model.rowCount(parent)):
            idx = model.index(r, 0, parent)
            back = model.parent(idx)
            if parent.isValid() != back.isValid():
                bad.append(idx.data(S.NODE_ROLE).name)
            elif parent.isValid() and back != parent:
                bad.append(idx.data(S.NODE_ROLE).name)
            if model.hasChildren(idx):
                _check_rows(idx)
    _check_rows()
    check("筛选后 index/parent 自洽", not bad, "-> 异常项 %s" % bad)

    # 目录自身命中、但子项全被过滤时：仍显示，且不应出现展开箭头
    r3 = S.Node("<r3>", True, now, 0)
    d3 = S.Node("D", True, now, int(0.5 * S.GB), r3, atime=now)
    r3.children.append(d3)
    d3.children.append(S.Node("f.bin", False, now, int(2 * S.GB), d3, atime=now))
    S.apply_filter(r3, S.NodeFilter(size_min=0.4, size_max=0.6), True)
    m3 = S.TreeModel(r3)
    idx3 = m3.index(0, 0, QModelIndex())
    check("目录自身命中但子项全隐藏时仍显示",
          idx3.isValid() and idx3.data(S.NODE_ROLE).name == "D")
    check("此时 rowCount 为 0", m3.rowCount(idx3) == 0)
    check("此时 hasChildren 为假（不画展开箭头）", not m3.hasChildren(idx3))

    # --- 一键重置 ---
    root, _ = _fixture()
    S.apply_filter(root, S.NodeFilter(size_min=1.0), True)
    before = len(_model_nodes(S.TreeModel(root)))
    S.apply_filter(root, None, True)
    after = len(_model_nodes(S.TreeModel(root)))
    check("重置后恢复完整树", after == 7 and before < after,
          "-> 筛选 %d -> 重置 %d" % (before, after))


# ==============================================================================
# J 排序（含算法性能对比）
# ==============================================================================

def test_sort():
    print("\n=== J 排序 ===")
    now = time.time()

    def flat():
        root = S.Node("<root>", True, now, 0)
        for name, is_dir, age, gb in (("b", False, 10, 3.0),
                                      ("a", False, 1, 1.0),
                                      ("c", False, 100, 2.0),
                                      ("ad", True, 50, 0.5)):
            _mk(root, name, is_dir, age, gb, now)
        return root

    def order(mode, metric=S.METRIC_ATIME):
        r = flat()
        S.sort_tree(r, mode, metric)
        return [c.name for c in r.children]

    check("大小 ↓", order("size_desc") == ["b", "c", "a", "ad"], "-> %s" % order("size_desc"))
    check("大小 ↑", order("size_asc") == ["ad", "a", "c", "b"], "-> %s" % order("size_asc"))
    check("名称 A→Z", order("name_asc") == ["a", "ad", "b", "c"], "-> %s" % order("name_asc"))
    check("名称 Z→A", order("name_desc") == ["c", "b", "ad", "a"], "-> %s" % order("name_desc"))
    check("时间 新→旧", order("time_new") == ["a", "b", "ad", "c"], "-> %s" % order("time_new"))
    check("时间 旧→新", order("time_old") == ["c", "ad", "b", "a"], "-> %s" % order("time_old"))
    df = order("dir_first")
    check("目录优先", df[0] == "ad", "-> %s" % df)
    check("扫描顺序不排序", order("none") == ["b", "a", "c", "ad"],
          "-> %s" % order("none"))

    # 递归性：不能只排顶层
    root = S.Node("<root>", True, now, 0)
    d1 = _mk(root, "D1", True, 1, 1.0, now)
    _mk(d1, "z", False, 1, 1.0, now)
    _mk(d1, "a", False, 1, 2.0, now)
    S.sort_tree(root, "name_asc")
    check("排序递归到子目录", [c.name for c in d1.children] == ["a", "z"],
          "-> %s" % [c.name for c in d1.children])

    # row 下标必须与排序结果一致（模型 parent() 依赖它）
    r = flat()
    S.sort_tree(r, "size_desc")
    check("排序后 row 下标回写正确",
          all(c.row == i for i, c in enumerate(r.children)))
    model = S.TreeModel(r)
    ok = all(model.parent(model.index(i, 0, QModelIndex())).isValid() is False
             for i in range(model.rowCount()))
    check("排序后顶层无父（row 自洽）", ok)

    # ---- 性能：key= vs cmp_to_key ----
    # 本质差异是"回调次数"：key= 每个元素只调用一次（n 次），
    # cmp_to_key 每次比较都要回到 Python 层（约 n·log n 次）。
    # 调用次数是确定的，不受机器抖动影响，所以用它作主判据，耗时作辅证。
    import functools
    import random
    N_DIR, N_CHILD = 400, 400
    big = S.Node("<big>", True, now, 0)
    for i in range(N_DIR):
        d = S.Node("d%03d" % i, True, now, 0, big)
        big.children.append(d)
        for j in range(N_CHILD):
            c = S.Node("f%04d" % j, False, now - (j * 37 % 900) * 86400,
                       (j * 7919 % 5000) * 1024 * 1024, d)
            d.children.append(c)
    total = N_DIR * N_CHILD

    key_calls = {"n": 0}
    cmp_calls = {"n": 0}

    def counting_key(n):
        key_calls["n"] += 1
        return n.size

    def counting_cmp(a, b):
        cmp_calls["n"] += 1
        return (a.size > b.size) - (a.size < b.size)

    # 注意：两边都必须先打乱。否则先跑的那一轮会把数据排好序，
    # 后一轮的 Timsort 面对有序输入只需 n 次比较，对比就失真了。
    for d in big.children:
        random.shuffle(d.children)
    t0 = time.perf_counter()
    for d in big.children:
        d.children.sort(key=counting_key, reverse=True)
    t_key = time.perf_counter() - t0

    for d in big.children:
        random.shuffle(d.children)
    t0 = time.perf_counter()
    for d in big.children:
        d.children.sort(key=functools.cmp_to_key(counting_cmp), reverse=True)
    t_cmp = time.perf_counter() - t0

    print("   [基准] %s 节点（%d 目录 × %d 项）"
          % (f"{total:,}", N_DIR, N_CHILD))
    print("          key=        回调 %-10s 次   耗时 %.3f s"
          % (f"{key_calls['n']:,}", t_key))
    print("          cmp_to_key  回调 %-10s 次   耗时 %.3f s"
          % (f"{cmp_calls['n']:,}", t_cmp))
    print("          回调倍率 %.1fx     耗时倍率 %.1fx"
          % (cmp_calls["n"] / max(key_calls["n"], 1), t_cmp / max(t_key, 1e-9)))

    check("key= 对每个元素只回调一次",
          key_calls["n"] == total,
          "-> %s 次 / %s 元素" % (f"{key_calls['n']:,}", f"{total:,}"))
    check("cmp_to_key 回调次数是元素数的数倍（n·log n 量级）",
          cmp_calls["n"] >= total * 5, "-> %s 次" % f"{cmp_calls['n']:,}")
    check("key= 实测快于 cmp_to_key", t_key < t_cmp,
          "-> key %.3fs vs cmp %.3fs（%.1f 倍）"
          % (t_key, t_cmp, t_cmp / max(t_key, 1e-9)))

    # Timsort 自适应：已有序输入接近 O(n)
    t0 = time.perf_counter()
    S.sort_tree(big, "size_desc")
    t_sorted = time.perf_counter() - t0

    for d in big.children:
        random.shuffle(d.children)
    t0 = time.perf_counter()
    S.sort_tree(big, "size_desc")
    t_shuffled = time.perf_counter() - t0
    print("          全树排序：已有序 %.3f s / 乱序 %.3f s" % (t_sorted, t_shuffled))
    check("已有序输入更快（Timsort 自适应特性）", t_sorted <= t_shuffled * 1.05,
          "-> 已有序 %.3fs vs 乱序 %.3fs" % (t_sorted, t_shuffled))


# ==============================================================================
# K 文件操作（复制路径 / 打开位置 / 删除）
# ==============================================================================

class _FakeConfirm:
    """替身确认框：安装到 S.ConfirmDeleteDialog 上，避免测试里弹真窗口"""
    decision = "accept"
    mode = "permanent"          # 测试统一用永久删除，不往回收站里塞东西

    def __init__(self, nodes, missing_count=0, parent=None):
        self.nodes = list(nodes)
        self.summary = S.selection_summary(self.nodes)
        self.typed_edit = None
        self.need_typed = False

    def exec(self):
        return (S.QDialog.DialogCode.Accepted if self.decision == "accept"
                else S.QDialog.DialogCode.Rejected)


class _Swap:
    """临时替换模块属性"""

    def __init__(self, name, value):
        self.name, self.new = name, value

    def __enter__(self):
        self.old = getattr(S, self.name)
        setattr(S, self.name, self.new)
        return self.new

    def __exit__(self, *a):
        setattr(S, self.name, self.old)
        return False


def _mkfile(base, rel, data=b"x"):
    p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def test_fileops_pure():
    print("\n=== K1 文件操作纯逻辑 ===")
    now = time.time()
    root = S.Node("<r>", True, now, 0)
    d = _mk(root, "D", True, 10, 1.0, now)
    f1 = _mk(d, "a.txt", False, 10, 0.5, now)
    f2 = _mk(d, "b.txt", False, 10, 0.5, now)
    f3 = _mk(root, "c.bin", False, 10, 2.0, now)

    check("node_paths 去重且保序",
          S.node_paths([f1, f2, f1]) == [f1.path, f2.path],
          "-> %s" % S.node_paths([f1, f2, f1]))
    check("node_paths 空输入", S.node_paths([]) == [])

    st = S.subtree_stats(d)
    check("subtree_stats 目录（文件数,目录数,字节）",
          st == (2, 0, 1024 ** 3), "-> %s" % (st,))
    check("subtree_stats 文件取自身", S.subtree_stats(f1) == (1, 0, int(0.5 * S.GB)),
          "-> %s" % (S.subtree_stats(f1),))

    sm = S.selection_summary([d, f3])
    check("selection_summary 计数",
          sm["count"] == 2 and sm["dirs"] == 1 and sm["files"] == 1,
          "-> %s" % {k: sm[k] for k in ("count", "files", "dirs")})
    check("selection_summary 含子项文件数", sm["child_files"] == 2)
    check("selection_summary 体积含子树",
          sm["bytes"] == int(1.0 * S.GB) + int(2.0 * S.GB), "-> %d" % sm["bytes"])
    check("selection_summary 记录最长路径",
          sm["longest_path"] == max(len(d.path), len(f3.path)))

    check("build_double_null 双 \\0 结尾",
          S.build_double_null(["a", "b"]) == "a\0b\0\0",
          "-> %r" % S.build_double_null(["a", "b"]))
    check("build_double_null 单项",
          S.build_double_null(["a"]) == "a\0\0")

    long_p = os.path.join(os.path.abspath("."), "x" * 300)
    check("超长路径自动加 \\\\?\\ 前缀",
          S._long_path(long_p).startswith("\\\\?\\"))
    short_p = os.path.abspath(".")
    check("短路径不加前缀", S._long_path(short_p) == short_p)

    # split_existing
    tmp = tempfile.mkdtemp(prefix="fo_")
    try:
        real = _mkfile(tmp, "there.txt")
        holder = S.Node(tmp, True, now, 0)          # 父节点名即真实临时目录
        n_real = S.Node("there.txt", False, now, 1, holder)
        holder.children.append(n_real)
        n_fake = S.Node("not_exist_" + os.urandom(4).hex() + ".txt",
                        False, now, 1, holder)
        holder.children.append(n_fake)
        ex, mi = S.split_existing([n_real, n_fake])
        check("split_existing 分离存在/不存在",
              ex == [n_real] and mi == [n_fake],
              "-> 存在 %d / 不存在 %d" % (len(ex), len(mi)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # remove_nodes 的批量正确性
    big = S.Node("<big>", True, now, 0)
    kids = [_mk(big, "k%04d" % i, False, 1, 0.001, now) for i in range(2000)]
    victims = kids[::7]                       # 约 286 个
    removed, parents = S.remove_nodes(victims)
    check("remove_nodes 摘除数量正确", removed == len(victims),
          "-> %d / %d" % (removed, len(victims)))
    check("remove_nodes 只留下未选中的",
          len(big.children) == 2000 - len(victims),
          "-> %d" % len(big.children))
    check("remove_nodes 返回受影响父节点", len(parents) == 1)
    check("remove_nodes 对无父节点安全", S.remove_nodes([S.Node("orphan", False)])[0] == 0)


def test_fileops_real():
    print("\n=== K2 真实删除与定位 ===")
    tmp = tempfile.mkdtemp(prefix="del_")
    try:
        # --- 永久删除 ---
        f1 = _mkfile(tmp, "a.txt", b"hello")
        f2 = _mkfile(tmp, "sub/b.txt", b"world")
        d1 = os.path.join(tmp, "sub/nested")
        os.makedirs(d1, exist_ok=True)
        _mkfile(tmp, "sub/nested/c.txt", b"deep")

        # 只读文件：验证 _force_remove 能摘掉只读位再删
        ro = _mkfile(tmp, "readonly.txt", b"ro")
        os.chmod(ro, stat_mod.S_IRUSR)

        missing = os.path.join(tmp, "not_exist.txt")
        ok, failed = S.delete_permanently([f1, f2, d1, ro, missing])
        check("永久删除：成功 4 项", ok == 4, "-> ok=%d failed=%s" % (ok, failed))
        check("永久删除：不存在的项进入失败清单",
              len(failed) == 1 and failed[0][0] == missing, "-> %s" % (failed,))
        check("永久删除：文件真的没了", not os.path.exists(f1))
        check("永久删除：目录被递归删除", not os.path.exists(d1))
        check("永久删除：只读文件也被删掉", not os.path.exists(ro))
        check("永久删除：父目录仍在", os.path.isdir(os.path.join(tmp, "sub")))

        # --- 回收站（真实调用一次；对象是本次新建的临时文件）---
        rb = _mkfile(tmp, "to_recycle.txt", b"rb")
        submitted, aborted, err = S.send_to_recycle_bin([rb])
        if os.name == "nt":
            check("移入回收站：调用成功", submitted, "-> %s" % err)
            check("移入回收站：文件已从原位置消失", not os.path.exists(rb))
            check("移入回收站：未被中途取消", aborted is False)
        else:
            check("非 Windows 平台给出明确提示", (not submitted) and err)

        # --- 打开所在位置：拦下 Popen，校验参数与退化逻辑 ---
        captured = []

        class _FakePopen:
            def __init__(self, args, *a, **kw):
                captured.append(args)

        live = _mkfile(tmp, "live.txt")
        fake_dir = os.path.join(tmp, "adir")
        os.makedirs(fake_dir, exist_ok=True)
        dead = os.path.join(tmp, "dead.txt")

        with _Swap("subprocess", type("P", (), {"Popen": _FakePopen})):
            good, _e = S.reveal_in_explorer(live)
            check("定位文件：使用 /select 参数",
                  good and captured[-1][1] == "/select," + live,
                  "-> %s" % (captured[-1],))
            good, _e = S.reveal_in_explorer(fake_dir)
            check("定位目录：直接打开目录本身",
                  good and captured[-1][1] == fake_dir, "-> %s" % (captured[-1],))
            good, _e = S.reveal_in_explorer(dead)
            check("文件已消失：退化为打开父目录",
                  good and captured[-1][1] == tmp, "-> %s" % (captured[-1],))
            good, msg = S.reveal_in_explorer(
                os.path.join(tmp, "no_dir", "no_file.txt"))
            check("文件与父目录都不在：返回失败并给出原因",
                  (not good) and "删除或移动" in msg, "-> %s" % msg)

        # 特殊字符不做 shell 转义（以列表形式传参）
        with _Swap("subprocess", type("P", (), {"Popen": _FakePopen})):
            weird = _mkfile(tmp, "a&b%c^d!e 'f'.txt")
            S.reveal_in_explorer(weird)
            check("特殊字符路径原样传入（无 shell 转义问题）",
                  captured[-1][1] == "/select," + weird,
                  "-> %s" % (captured[-1],))
    finally:
        for root_, dirs_, files_ in os.walk(tmp):
            for f in files_:
                try:
                    os.chmod(os.path.join(root_, f), stat_mod.S_IRWXU)
                except OSError:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_fileops_ui():
    print("\n=== K3 界面联动（选中 / 复制 / 删除）===")
    S.AUTO_SCAN = False
    win = S.MainWindow()
    win.timer.stop()
    # 无头环境没有"确定"按钮，模态 QMessageBox 会永久阻塞，这里替换成记录器
    warned = []
    win._warn = lambda title, text: warned.append((title, text))

    tmp = tempfile.mkdtemp(prefix="ui_")
    cb = QApplication.clipboard()
    saved_cb = cb.text()                 # 测试会用到剪贴板，事后还原
    try:
        # 造一棵树挂到窗口上，路径都指向真实临时文件
        now = time.time()
        # 根节点名用真实临时目录，这样 node.path 才是绝对且真实存在的路径
        t_root = S.Node(tmp, True, now, 0)
        nodes = []
        for i in range(5):
            p = _mkfile(tmp, "f%d.txt" % i, b"x" * (i + 1))
            n = S.Node("f%d.txt" % i, False, now, i + 1, t_root)
            t_root.children.append(n)
            nodes.append(n)
        sub = S.Node("subdir", True, now, 0, t_root)
        t_root.children.append(sub)
        os.makedirs(os.path.join(tmp, "subdir"), exist_ok=True)
        for i in range(3):
            _mkfile(tmp, "subdir/g%d.txt" % i, b"y")
            c = S.Node("g%d.txt" % i, False, now, 1, sub)
            sub.children.append(c)

        win.clear_model()
        win._sort_sig = None
        win._view_sig = None
        win._expanded_nodes = set()
        win.model = S.TreeModel(t_root)
        win.tree.setModel(win.model)
        sm = win.tree.selectionModel()
        sm.selectionChanged.connect(win.on_selection_changed)
        win._update_selection_ui()

        check("初始无选中时按钮置灰",
              not win.btn_delete.isEnabled() and not win.btn_copy_path.isEnabled())
        check("初始标签为 0", win.btn_selection.text() == "查看选中 (0)",
              "-> %s" % win.btn_selection.text())

        # 选中 3 个（模拟 Ctrl 多选）
        flags = (QItemSelectionModel.SelectionFlag.Select
                 | QItemSelectionModel.SelectionFlag.Rows)
        for r in (0, 2, 4):
            sm.select(win.model.index(r, 0, QModelIndex()), flags)
        check("选中 3 项后标签更新", win.btn_selection.text() == "查看选中 (3)",
              "-> %s" % win.btn_selection.text())
        check("选中后按钮解禁", win.btn_delete.isEnabled())
        check("selected_nodes 数量正确", len(win.selected_nodes()) == 3)

        # 复制路径
        n = win.copy_selected_paths()
        check("复制返回条数 = 选中数", n == 3, "-> %d" % n)
        expect = "\n".join([nodes[0].path, nodes[2].path, nodes[4].path])
        check("剪贴板内容为完整路径、每行一个",
              cb.text() == expect, "-> %r" % cb.text()[:80])
        check("复制内容确为绝对路径",
              all(os.path.isabs(p) for p in cb.text().split("\n")))

        # 右键多选：右键点未选中的第 1 行，应「加入」而非替换
        before = len(win.selected_nodes())
        sm.select(win.model.index(1, 0, QModelIndex()), flags)
        win._update_selection_ui()
        check("右键加入选择后数量增加",
              len(win.selected_nodes()) == before + 1,
              "-> %d" % len(win.selected_nodes()))
        check("原有选择未被清空",
              set(id(x) for x in win.selected_nodes()) >=
              {id(nodes[0]), id(nodes[2]), id(nodes[4])})

        # 多选窗口
        dlg = S.SelectionDialog(win)
        dlg.set_nodes(win.selected_nodes())
        check("多选窗口行数 = 选中数",
              dlg.table.rowCount() == len(win.selected_nodes()),
              "-> %d" % dlg.table.rowCount())
        check("多选窗口列出名称与完整路径",
              dlg.table.item(0, 0) is not None and
              os.path.isabs(dlg.table.item(0, 1).text()),
              "-> %s" % (dlg.table.item(0, 1).text() if dlg.table.item(0, 1) else None))
        check("多选窗口有类型列",
              dlg.table.item(0, 2).text() in ("文件", "目录"))
        cnt = dlg.copy_all_paths()
        check("多选窗口复制全部路径", cnt == len(win.selected_nodes()) and
              len(cb.text().split("\n")) == cnt, "-> %d" % cnt)

        dlg.set_nodes([])
        check("空选择时多选窗口按钮置灰",
              not dlg.btn_delete.isEnabled() and not dlg.btn_copy_all.isEnabled())
        check("空选择时表头提示为 0", "0</b>" in dlg.head.text(),
              "-> %s" % dlg.head.text())
        dlg.deleteLater()

        # ---- 删除：用户取消 -> 一个文件都不能少 ----
        class _Cancel(_FakeConfirm):
            decision = "reject"

        before_exists = os.path.exists(nodes[0].path)
        with _Swap("ConfirmDeleteDialog", _Cancel):
            r = win.delete_nodes([nodes[0]])
        check("取消删除时返回 None", r is None)
        check("取消删除后文件仍在", os.path.exists(nodes[0].path) == before_exists)
        check("取消删除后状态栏说明未改动",
              "已取消删除" in win.status.currentMessage(),
              "-> %s" % win.status.currentMessage())

        # ---- 删除：全部已不存在 -> 不弹确认框，直接提示 ----
        ghost = S.Node("ghost_%s.txt" % os.urandom(4).hex(), False, now, 0, t_root)
        ghost.parent = t_root
        with _Swap("ConfirmDeleteDialog", _FakeConfirm):
            r = win.delete_nodes([ghost])
        check("目标不存在时不执行删除", r is None)
        check("目标不存在时给出明确提示（非静默）",
              any("不存在" in t for _t, t in warned),
              "-> 提示 %s" % [t for _t, t in warned])

        # ---- 删除：确认后真的删，并从树里摘掉、父级体积重算 ----
        S.aggregate_sizes(t_root)      # 夹具建好后先聚合，否则基线是 0
        root_size_before = t_root.size
        sub_size_before = sub.size
        check("夹具聚合后根体积 = 各子项之和",
              root_size_before == 1 + 2 + 3 + 4 + 5 + 3,
              "-> %d" % root_size_before)
        with _Swap("ConfirmDeleteDialog", _FakeConfirm):
            r = win.delete_nodes([nodes[3]])
        check("确认后删除成功返回 1", r == 1, "-> %s" % r)
        check("文件已从磁盘删除", not os.path.exists(nodes[3].path))
        check("节点已从树里摘掉",
              all(c is not nodes[3] for c in t_root.children),
              "-> 顶层 %d 项" % len(t_root.children))
        check("根节点体积已重算", t_root.size == root_size_before - 4,
              "-> %d -> %d" % (root_size_before, t_root.size))

        # ---- 删除目录：整枝从树里消失，父级体积扣掉子树 ----
        with _Swap("ConfirmDeleteDialog", _FakeConfirm):
            r = win.delete_nodes([sub])
        check("删除目录返回 1", r == 1, "-> %s" % r)
        check("目录已从磁盘递归删除", not os.path.exists(os.path.join(tmp, "subdir")))
        check("目录节点已摘掉",
              all(c is not sub for c in t_root.children))
        check("根体积扣掉了整棵子树",
              t_root.size == root_size_before - 4 - sub_size_before,
              "-> %d" % t_root.size)

        # ---- 未选中任何项 ----
        check("未选中时删除返回 None", win.delete_nodes([]) is None)
        check("未选中时复制返回 0", win.copy_paths_of([]) == 0)
        check("未选中时定位返回 0", win.reveal_nodes([]) == 0)

        win.deleteLater()
    finally:
        cb.setText(saved_cb)
        shutil.rmtree(tmp, ignore_errors=True)


def test_confirm_dialog():
    print("\n=== K4 删除确认框 ===")
    now = time.time()
    # 1 个文件：不需要键入数量
    one = S.Node("a.txt", False, now, 1024)
    d = S.ConfirmDeleteDialog([one])
    check("默认方式是移入回收站", d.mode == "recycle" and d.rb_recycle.isChecked())
    check("小量删除无需键入", d.typed_edit is None)
    check("删除按钮可用", d.btn_ok.isEnabled())
    check("默认按钮是取消（回车不会误删）", d.btn_cancel.isDefault())
    check("切换永久删除后 mode 同步",
          (d.rb_perm.setChecked(True), d.mode)[1] == "permanent")
    d.deleteLater()

    # 1 个目录内含大量文件：必须键入数量
    root = S.Node("<r>", True, now, 0)
    dd = S.Node("Big", True, now, 0, root)
    root.children.append(dd)
    for i in range(S.BULK_DELETE_THRESHOLD + 10):
        dd.children.append(S.Node("f%d" % i, False, now, 1, dd))
    d2 = S.ConfirmDeleteDialog([dd])
    check("涉及大量文件时需要键入数量", d2.typed_edit is not None)
    check("未键入时删除按钮禁用", not d2.btn_ok.isEnabled())
    d2.typed_edit.setText("abc")
    check("键入错误内容仍禁用", not d2.btn_ok.isEnabled())
    d2.typed_edit.setText(d2.required_text())
    check("键入正确数量后解禁", d2.btn_ok.isEnabled())
    check("确认文案包含目录连带信息",
          any("连同" in c.text() for c in d2.findChildren(S.QLabel)),
          "-> %s" % [c.text()[:24] for c in d2.findChildren(S.QLabel)][:3])
    d2.deleteLater()

    # 超过阈值数量：也要键入
    many = [S.Node("f%d.txt" % i, False, now, 1) for i in
            range(S.BULK_DELETE_THRESHOLD + 1)]
    d3 = S.ConfirmDeleteDialog(many)
    check("超过阈值数量需要键入", d3.typed_edit is not None)
    d3.typed_edit.setText(str(len(many)))
    check("大量项目键入后解禁", d3.btn_ok.isEnabled())
    d3.deleteLater()


# ==============================================================================
# L 扫描进度模型
# ==============================================================================

def _snap(**kw):
    base = {"phase": "enum", "bytes": 0, "disk_used": 0, "pending": 0,
            "bytes_per_dir": 0, "phase_done": 0, "phase_total": 0,
            "dirs": 0, "files": 0, "elapsed": 0.0, "skipped": 0,
            "current": "", "truncated": False}
    base.update(kw)
    return base


def test_progress_model():
    print("\n=== L 扫描进度模型 ===")

    # --- 卷根判定（决定用哪种算法）---
    check("根路径判定为卷根", S.is_volume_root(os.path.abspath(os.sep)),
          "-> %s" % os.path.abspath(os.sep))
    check("普通子目录判定为非卷根",
          not S.is_volume_root(os.path.abspath(".")),
          "-> %s" % os.path.abspath("."))
    if os.name == "nt":
        check("C:\\\\ 判定为卷根", S.is_volume_root("C:\\"))
        check("C:\\\\Windows 判定为非卷根", not S.is_volume_root("C:\\Windows"))

    # --- 聚合 / 排序阶段：总量已知，应精确 ----
    p, basis = S.estimate_scan_percent(_snap(phase="aggregate",
                                             phase_done=0, phase_total=100))
    check("聚合阶段起点 = 枚举终点", abs(p - S.PROGRESS_ENUM_END) < 0.01,
          "-> %.2f (%s)" % (p, basis))
    p, _ = S.estimate_scan_percent(_snap(phase="aggregate",
                                         phase_done=100, phase_total=100))
    check("聚合阶段终点 = %.0f" % S.PROGRESS_AGG_END,
          abs(p - S.PROGRESS_AGG_END) < 0.01, "-> %.2f" % p)
    p, _ = S.estimate_scan_percent(_snap(phase="aggregate",
                                         phase_done=50, phase_total=100))
    check("聚合阶段中点", abs(p - (S.PROGRESS_ENUM_END + S.PROGRESS_AGG_END) / 2) < 0.01,
          "-> %.2f" % p)
    p, basis = S.estimate_scan_percent(_snap(phase="sort",
                                             phase_done=100, phase_total=100))
    check("排序阶段收尾 = 100", abs(p - 100.0) < 0.01, "-> %.2f (%s)" % (p, basis))
    p, _ = S.estimate_scan_percent(_snap(phase="done"))
    check("完成后恒为 100", p == 100.0)

    # --- 卷扫描：体积覆盖是真实分母 ---
    gb = S.GB
    p, basis = S.estimate_scan_percent(
        _snap(bytes=gb, disk_used=4 * gb), volume_scan=True)
    check("卷扫描按体积算百分比", abs(p - 25.0) < 0.01 and basis == "按体积",
          "-> %.1f (%s)" % (p, basis))
    p, _ = S.estimate_scan_percent(
        _snap(bytes=10 * gb, disk_used=4 * gb), volume_scan=True)
    check("体积超过分母时封顶在 %.0f%%" % S.PROGRESS_EST_CAP,
          abs(p - S.PROGRESS_EST_CAP) < 0.01, "-> %.1f" % p)
    p, basis = S.estimate_scan_percent(_snap(bytes=gb, disk_used=0),
                                       volume_scan=True)
    check("拿不到卷用量时不编百分比", p is None, "-> %s (%s)" % (p, basis))

    # --- 子目录扫描：没有真实分母，必须返回 None 而不是编数字 ---
    p, basis = S.estimate_scan_percent(
        _snap(bytes=2 * gb, disk_used=200 * gb, pending=500),
        volume_scan=False)
    check("子目录扫描不给可信分母时返回 None", p is None,
          "-> %s (%s)" % (p, basis))
    check("依据标签说明是实况", basis == "按实况", "-> %s" % basis)

    # --- ProgressTracker：单调不减 ---
    tr = S.ProgressTracker()
    t = 1000.0
    seq = [10.0, 40.0, 35.0, 0.0, 60.0, 55.0]
    outs = []
    for i, v in enumerate(seq):
        t += 0.2
        outs.append(tr.update(v, i * 100, _snap(elapsed=t - 1000.0), now=t))
    check("百分比单调不减", outs == sorted(outs), "-> %s" % outs)
    check("最终取到出现过的最大值", abs(outs[-1] - 60.0) < 0.01,
          "-> %.1f" % outs[-1])

    # --- pct=None 不应崩溃，也不应改变已有进度 ---
    tr2 = S.ProgressTracker()
    tr2.update(45.0, 100, _snap(), now=1.0)
    v = tr2.update(None, 200, _snap(), now=1.3)
    check("传 None 时保持原百分比", abs(v - 45.0) < 0.01, "-> %.1f" % v)

    # --- 速率与剩余时间 ---
    tr3 = S.ProgressTracker()
    snap = _snap(bytes=0, disk_used=10 * gb, elapsed=0.0)
    tr3.update(0.0, 0, snap, now=0.0)
    for i in range(1, 8):
        snap = _snap(bytes=i * 1000, disk_used=10 * gb, elapsed=i * 0.5)
        tr3.update(i, i * 100, snap, now=i * 0.5)
    check("速率已估算出来", tr3.rate > 0, "-> %.0f 项/秒" % tr3.rate)
    check("字节速率已估算出来", tr3.byte_rate > 0, "-> %.0f B/s" % tr3.byte_rate)
    snap2 = _snap(bytes=1 * gb, disk_used=10 * gb, elapsed=5.0)
    eta = tr3.eta_seconds(snap2, volume_scan=True)
    check("卷扫描给出剩余时间", eta is not None and eta >= 0,
          "-> %s" % S.fmt_duration(eta))
    check("子目录扫描不给剩余时间",
          tr3.eta_seconds(snap2, volume_scan=False) is None)
    check("非枚举阶段不给剩余时间",
          tr3.eta_seconds(_snap(phase="aggregate", bytes=gb, disk_used=10 * gb),
                          volume_scan=True) is None)

    # --- 时长格式化 ---
    check("时长 45 秒", S.fmt_duration(45) == "45 秒", "-> %s" % S.fmt_duration(45))
    check("时长 3 分 05 秒", S.fmt_duration(185) == "3 分 05 秒",
          "-> %s" % S.fmt_duration(185))
    check("时长 1 时 02 分", S.fmt_duration(3720) == "1 时 02 分",
          "-> %s" % S.fmt_duration(3720))
    check("时长未知显示占位符", S.fmt_duration(None) == "—")
    check("负数时长安全", S.fmt_duration(-5) == "0 秒")

    # --- 端到端：真实扫描的进度必须单调不减且最终到达 100 ---
    tmp = tempfile.mkdtemp(prefix="prog_")
    try:
        for i in range(60):
            d = os.path.join(tmp, "d%02d" % i)
            os.makedirs(d, exist_ok=True)
            for j in range(20):
                with open(os.path.join(d, "f%02d.txt" % j), "wb") as f:
                    f.write(b"x" * 64)
        eng = S.ScanEngine(max_nodes=1_000_000)
        tr4 = S.ProgressTracker()
        seen = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                s = eng.snapshot()
                pct, _b = S.estimate_scan_percent(s, S.is_volume_root(tmp))
                seen.append(tr4.update(pct, s["dirs"] + s["files"], s))
                time.sleep(0.01)

        th = threading.Thread(target=poll, daemon=True)
        th.start()
        eng.scan(tmp)
        stop.set()
        th.join(timeout=1)

        check("真实扫描：进度单调不减",
              all(seen[i] <= seen[i + 1] + 1e-9 for i in range(len(seen) - 1)),
              "-> %d 个采样点" % len(seen))
        final, _ = S.estimate_scan_percent(eng.snapshot(), False)
        check("真实扫描：结束时百分比为 100",
              final == 100.0, "-> %s" % final)
        check("真实扫描：collect 到目录与文件计数",
              eng.dirs_scanned == 61 and eng.files_found == 1200,
              "-> 目录 %d / 文件 %d" % (eng.dirs_scanned, eng.files_found))
        check("真实扫描：累计字节正确",
              eng.bytes_found == 1200 * 64, "-> %d" % eng.bytes_found)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==============================================================================

def main():
    app = QApplication(sys.argv)
    print("Qt平台:", app.platformName())

    test_logic()
    root = None
    try:
        root = test_scan()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_scan 抛异常")
    test_model(root)
    try:
        test_delegate()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_delegate 抛异常")
    try:
        test_metric_and_cap()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_metric_and_cap 抛异常")
    try:
        test_row_style()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_row_style 抛异常")
    try:
        test_filter()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_filter 抛异常")
    try:
        test_sort()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_sort 抛异常")
    try:
        test_progress_model()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_progress_model 抛异常")
    try:
        test_fileops_pure()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_fileops_pure 抛异常")
    try:
        test_fileops_real()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_fileops_real 抛异常")
    try:
        test_confirm_dialog()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_confirm_dialog 抛异常")
    try:
        test_fileops_ui()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_fileops_ui 抛异常")
    try:
        test_mainwindow()
    except Exception:                                   # noqa: BLE001
        traceback.print_exc()
        FAILED.append("test_mainwindow 抛异常")

    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
    if FAILED:
        print("失败列表：")
        for f in FAILED:
            print("   -", f)
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
