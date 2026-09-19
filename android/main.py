# -*- coding: utf-8 -*-
"""
存储扫描可视化 —— Android 版（Kivy）
================================================================================
与 Windows 版的关系：
    核心逻辑（扫描 / 聚合 / 筛选 / 排序 / 进度 / 删除）全部复用 corescan.py，
    那是从 scanner.py 程序化抽取的、逐字节未改动的纯标准库代码。
    本文件只负责 UI 与安卓平台适配。

相比 Windows 版的功能删减（按你的要求）：
    · 多选：拖拽拉选 + 右键加选  ->  每行一个勾选框
    · 删除：回收站 / 永久二选一  ->  直接删除（无回收站，不可恢复）
    · 移除：「打开所在位置」     ->  安卓没有资源管理器
    · 扫描：选择盘符            ->  固定扫共享存储根目录（安卓没有盘符）

保留的安全设计：
    删除确认框、默认焦点在取消、超过 BULK_DELETE_THRESHOLD 项需手动键入数量。
    因为安卓没有回收站兜底，这些保护比 Windows 版更重要，不能省。
================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corescan as C                                          # noqa: E402

from kivy.app import App                                      # noqa: E402
from kivy.clock import Clock                                  # noqa: E402
from kivy.core.clipboard import Clipboard                     # noqa: E402
from kivy.lang import Builder                                 # noqa: E402
from kivy.properties import (                                 # noqa: E402
    BooleanProperty, ListProperty, NumericProperty,
    ObjectProperty, StringProperty,
)
from kivy.uix.boxlayout import BoxLayout                      # noqa: E402
from kivy.uix.popup import Popup                              # noqa: E402
from kivy.uix.recycleview import RecycleView                  # noqa: E402
from kivy.uix.recycleview.views import RecycleDataViewBehavior  # noqa: E402
from kivy.utils import get_color_from_hex, platform           # noqa: E402

IS_ANDROID = (platform == "android")


# ==============================================================================
# 一、安卓平台适配（JNI）
# ==============================================================================

def has_all_files_access() -> bool:
    """是否已获得「所有文件访问」权限（MANAGE_EXTERNAL_STORAGE）。

    桌面调试环境一律返回 True，方便直接在电脑上跑起来看界面。
    """
    if not IS_ANDROID:
        return True
    try:
        from jnius import autoclass
        Build = autoclass("android.os.Build")
        if int(Build.VERSION.SDK_INT) < 30:
            return True                       # Android 10 及以下用普通存储权限即可
        Environment = autoclass("android.os.Environment")
        return bool(Environment.isExternalStorageManager())
    except Exception:                          # noqa: BLE001
        return False


def request_all_files_access() -> bool:
    """引导用户去系统设置里开启「所有文件访问」。

    这个权限不能像普通权限那样弹窗申请，必须由用户手动在系统设置页开启，
    所以只能把设置页拉起来。返回 False 表示"还没拿到，需要用户去开"。
    """
    if not IS_ANDROID:
        return True
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        act = PythonActivity.mActivity
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        intent.setData(Uri.parse("package:" + act.getPackageName()))
        act.startActivity(intent)
        return False
    except Exception:                          # noqa: BLE001
        return False


def notify_media_scanner(paths) -> int:
    """删除文件后通知系统媒体库刷新索引。

    不做这一步的话：文件确实删了、空间也释放了，但系统相册里那张图还会显示
    （点开提示不存在），可能要几小时等系统自己重扫才消失——这就是 MediaStore
    残留条目。scanFile() 会让它立刻同步。
    """
    if not IS_ANDROID:
        return 0
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        MediaScannerConnection = autoclass(
            "android.media.MediaScannerConnection")
        arr = [str(p) for p in paths]
        if not arr:
            return 0
        MediaScannerConnection.scanFile(PythonActivity.mActivity, arr, None, None)
        return len(arr)
    except Exception:                          # noqa: BLE001
        return 0


def toast(msg: str):
    """安卓上弹个小提示；桌面上只打印"""
    if IS_ANDROID:
        try:
            from jnius import autoclass
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            Toast = autoclass("android.widget.Toast")
            Toast.makeText(PythonActivity.mActivity, str(msg),
                           Toast.LENGTH_SHORT).show()
            return
        except Exception:                      # noqa: BLE001
            pass
    print("[toast]", msg)


# ==============================================================================
# 二、列表行
# ==============================================================================

class ScanRow(RecycleDataViewBehavior, BoxLayout):
    """一行：[勾选框] [时间色标] 名称 ...... 大小 [体积色标]

    色标逻辑与 Windows 版完全一致：
        前段 = 时间档位（7天内绿 / 180天内黄 / 更久红）
        后段 = 体积档位（<1GB绿 / <3GB黄 / >=3GB红）
    手机上屏幕窄，所以省掉了灰色小字，只保留色标 + 名称 + 大小。
    """

    node = ObjectProperty(None, allownone=True)
    indent = NumericProperty(0)
    name_text = StringProperty("")
    size_text = StringProperty("")
    time_color = ListProperty([0.2, 0.2, 0.2, 1])
    size_color = ListProperty([0.2, 0.2, 0.2, 1])
    checked = BooleanProperty(False)
    is_dir = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        self._guard = True
        self.index = index
        ret = super().refresh_view_attrs(rv, index, data)
        # 手动同步勾选框；刷新期间用 _guard 挡住回调，避免"界面改数据、
        # 数据又改界面"的回环
        self.ids.cb.active = bool(data.get("checked", False))
        self._guard = False
        return ret

    def on_check(self, active):
        if getattr(self, "_guard", False):
            return
        app = App.get_running_app()
        if app is not None and self.node is not None:
            app.set_checked(self.node, bool(active))

    def on_row_tap(self):
        """点行体（非勾选框）时展开/折叠目录"""
        app = App.get_running_app()
        if app is not None and self.node is not None:
            app.toggle_expand(self.node)


Builder.load_string(r"""
<ScanRow>:
    orientation: 'horizontal'
    size_hint_y: None
    height: dp(38)
    padding: [root.indent * dp(14) + dp(4), 0, dp(4), 0]
    spacing: dp(4)

    canvas.before:
        Color:
            rgba: (0.90, 0.94, 1.0, 1) if root.checked else (0, 0, 0, 0)
        Rectangle:
            pos: self.pos
            size: self.size
        Color:
            rgba: (0.85, 0.85, 0.85, 1)
        Rectangle:
            pos: self.pos[0], self.pos[1]
            size: self.size[0], 1

    CheckBox:
        id: cb
        size_hint_x: None
        width: dp(40)
        on_active: root.on_check(self.active)

    Widget:
        size_hint_x: None
        width: dp(11)
        canvas.after:
            Color:
                rgba: root.time_color
            RoundedRectangle:
                pos: self.center_x - dp(5), self.center_y - dp(5)
                size: dp(10), dp(10)
                radius: [dp(2)]

    Button:
        text: root.name_text
        font_size: sp(13)
        bold: root.is_dir
        halign: 'left'
        valign: 'middle'
        shorten: True
        shorten_from: 'right'
        size_hint_x: 1
        background_color: (0, 0, 0, 0)
        background_normal: ''
        color: (0.08, 0.08, 0.08, 1) if not root.checked else (0.05, 0.10, 0.25, 1)
        text_size: self.width, None
        on_release: root.on_row_tap()

    Label:
        text: root.size_text
        font_size: sp(11)
        size_hint_x: None
        width: dp(62)
        halign: 'right'
        color: (0.35, 0.35, 0.35, 1)

    Widget:
        size_hint_x: None
        width: dp(11)
        canvas.after:
            Color:
                rgba: root.size_color
            RoundedRectangle:
                pos: self.center_x - dp(5), self.center_y - dp(5)
                size: dp(10), dp(10)
                radius: [dp(2)]
""")


# ==============================================================================
# 三、删除确认框
# ==============================================================================

Builder.load_string(r"""
<ConfirmDelete>:
    orientation: 'vertical'
    padding: dp(12)
    spacing: dp(8)

    Label:
        id: head
        text_size: self.width, None
        size_hint_y: None
        height: self.texture_size[1]
        font_size: sp(14)
        markup: True

    Label:
        id: warn
        text_size: self.width, None
        size_hint_y: None
        height: self.texture_size[1]
        font_size: sp(12)
        color: (0.85, 0.18, 0.15, 1)
        markup: True

    Label:
        id: danger
        text: '安卓没有回收站 —— 删除后不可恢复'
        size_hint_y: None
        height: dp(24)
        font_size: sp(12)
        color: (0.85, 0.18, 0.15, 1)
        bold: True

    BoxLayout:
        id: typebox
        orientation: 'vertical'
        size_hint_y: None
        height: dp(64)
        spacing: dp(4)

    BoxLayout:
        orientation: 'horizontal'
        size_hint_y: None
        height: dp(44)
        spacing: dp(8)
        Button:
            id: btn_cancel
            text: '取消'
            on_release: root.cancel()
        Button:
            id: btn_ok
            text: '删除'
            background_color: (0.85, 0.18, 0.15, 1)
            on_release: root.confirm()
""")


class ConfirmDelete(BoxLayout):
    """删除确认框。

    三重保护（与 Windows 版一致）：
      1. 删除按钮默认是禁用的，必须点进来先看清楚
      2. 删除量大时，必须手动键入项目数量才解禁
      3. 明确提示"安卓没有回收站，不可恢复"
    """

    def __init__(self, nodes, on_confirm=None, **kw):
        super().__init__(**kw)
        self.nodes = list(nodes)
        self.on_confirm = on_confirm
        self.summary = C.selection_summary(self.nodes)
        s = self.summary
        self.ids.head.text = (
            "即将删除 [b]%d[/b] 个项目（文件 %d 个、目录 %d 个）\n合计占用 %s"
            % (s["count"], s["files"], s["dirs"], C.fmt_size(s["bytes"])))
        warn = ""
        if s["dirs"]:
            warn = ("注意：目录会连同其下内容一并删除，牵涉 %d 个文件、%d 个子目录。"
                    % (s["child_files"], s["child_dirs"]))
        self.ids.warn.text = warn

        total_files = s["files"] + s["child_files"]
        self.need_typed = (s["count"] > C.BULK_DELETE_THRESHOLD
                           or total_files > C.BULK_DELETE_THRESHOLD)
        if self.need_typed:
            from kivy.uix.label import Label
            from kivy.uix.textinput import TextInput
            self.ids.typebox.add_widget(Label(
                text="删除量较大，请输入待删除项目数 %d 以确认：" % s["count"],
                font_size="12sp", size_hint_y=None, height="24dp"))
            self.typed = TextInput(multiline=False, input_filter="int",
                                   hint_text="键入 %d" % s["count"],
                                   font_size="14sp",
                                   size_hint_y=None, height="36dp")
            self.typed.bind(text=self._validate)
            self.ids.typebox.add_widget(self.typed)
        else:
            self.typed = None

        self.ids.btn_ok.disabled = self.need_typed

    def _validate(self, *_):
        if self.typed is None:
            self.ids.btn_ok.disabled = False
            return
        self.ids.btn_ok.disabled = (
            self.typed.text.strip() != str(self.summary["count"]))

    def confirm(self):
        if self.ids.btn_ok.disabled:
            return
        if self.on_confirm:
            self.on_confirm(self.nodes)

    def cancel(self):
        if self.popup is not None:
            self.popup.dismiss()


# ==============================================================================
# 四、主界面
# ==============================================================================

Builder.load_string(r"""
<RootWidget>:
    orientation: 'vertical'
    spacing: dp(4)
    padding: dp(4)

    BoxLayout:
        size_hint_y: None
        height: dp(46)
        spacing: dp(4)
        Button:
            id: btn_start
            text: '开始扫描'
            size_hint_x: None
            width: dp(92)
            on_release: root.start_scan()
        Button:
            id: btn_stop
            text: '停止'
            size_hint_x: None
            width: dp(72)
            disabled: True
            on_release: root.stop_scan()
        ProgressBar:
            id: prog
            max: 100
            value: 0
        Label:
            id: phase
            text: ''
            size_hint_x: None
            width: dp(96)
            font_size: sp(11)
            color: (0.3, 0.3, 0.3, 1)

    BoxLayout:
        size_hint_y: None
        height: dp(40)
        spacing: dp(3)
        Label:
            text: '大小'
            size_hint_x: None
            width: dp(30)
            font_size: sp(11)
        TextInput:
            id: smin
            hint_text: '最小'
            input_filter: 'float'
            multiline: False
            font_size: sp(12)
            size_hint_x: None
            width: dp(58)
            on_text: root.schedule_filter()
        TextInput:
            id: smax
            hint_text: '最大'
            input_filter: 'float'
            multiline: False
            font_size: sp(12)
            size_hint_x: None
            width: dp(58)
            on_text: root.schedule_filter()
        Label:
            text: 'GB'
            size_hint_x: None
            width: dp(22)
            font_size: sp(11)
        Label:
            text: '距今'
            size_hint_x: None
            width: dp(32)
            font_size: sp(11)
        TextInput:
            id: dmin
            hint_text: '最小'
            input_filter: 'int'
            multiline: False
            font_size: sp(12)
            size_hint_x: None
            width: dp(50)
            on_text: root.schedule_filter()
        TextInput:
            id: dmax
            hint_text: '最大'
            input_filter: 'int'
            multiline: False
            font_size: sp(12)
            size_hint_x: None
            width: dp(50)
            on_text: root.schedule_filter()
        Label:
            text: '天'
            size_hint_x: None
            width: dp(18)
            font_size: sp(11)
        Spinner:
            id: sort
            size_hint_x: None
            width: dp(104)
            font_size: sp(11)
            on_text: root.schedule_filter()
        Button:
            text: '重置'
            size_hint_x: None
            width: dp(56)
            font_size: sp(11)
            on_release: root.reset_filter()

    RecycleView:
        id: rv
        viewclass: 'ScanRow'
        RecycleBoxLayout:
            default_size: None, dp(38)
            default_size_hint: 1, None
            size_hint_y: None
            height: self.minimum_height
            orientation: 'vertical'

    BoxLayout:
        size_hint_y: None
        height: dp(44)
        spacing: dp(4)
        Label:
            id: sel
            text: '未选择'
            size_hint_x: 1
            font_size: sp(12)
            halign: 'left'
            text_size: self.width, None
            color: (0.15, 0.15, 0.15, 1)
        Button:
            id: btn_copy
            text: '复制路径'
            size_hint_x: None
            width: dp(84)
            font_size: sp(12)
            disabled: True
            on_release: root.copy_paths()
        Button:
            id: btn_del
            text: '删除'
            size_hint_x: None
            width: dp(72)
            font_size: sp(12)
            background_color: (0.85, 0.18, 0.15, 1)
            disabled: True
            on_release: root.delete_selected()

    Label:
        id: status
        text: '点「开始扫描」'
        size_hint_y: None
        height: dp(26)
        font_size: sp(11)
        halign: 'left'
        text_size: self.width, None
        color: (0.3, 0.3, 0.3, 1)
""")


class RootWidget(BoxLayout):

    def __init__(self, **kw):
        super().__init__(**kw)
        self.root_node = None
        self.expanded = set()          # 展开的目录（存 Node 对象本身）
        self.checked = set()           # 勾选的项
        self.engine = None
        self.tracker = C.ProgressTracker()
        self._thread = None
        self._done = False
        self._error = None
        self._tick_ev = None
        self._filter_ev = None
        self._popup = None

        self.ids.sort.values = [name for _k, name in C.SORT_MODES]
        self.ids.sort.text = dict(C.SORT_MODES).get(C.DEFAULT_SORT,
                                                    self.ids.sort.values[0])

    # ---------------------------------------------------------------- 扫描
    def start_scan(self):
        if self._thread is not None and self._thread.is_alive():
            return
        if not has_all_files_access():
            request_all_files_access()
            self.set_status("需要先在系统设置里开启「所有文件访问」，然后回来再点扫描")
            return

        self.root_node = None
        self.expanded.clear()
        self.checked.clear()
        self._done = False
        self._error = None
        self.tracker.reset()
        self.ids.prog.value = 0
        self.ids.rv.data = []
        self.ids.btn_start.disabled = True
        self.ids.btn_stop.disabled = False
        self.ids.btn_del.disabled = True
        self.ids.btn_copy.disabled = True

        root = os.environ.get("CORESCAN_ROOT") or C.scan_roots()[0]
        self.set_status("正在扫描：%s" % root)
        self.ids.phase.text = "枚举中"

        self.engine = C.ScanEngine()
        self._thread = threading.Thread(target=self._worker, args=(root,),
                                        daemon=True)
        self._thread.start()
        self._tick_ev = Clock.schedule_interval(self._tick, 0.12)

    def _worker(self, path):
        try:
            node = self.engine.scan(path)
            if node is None:
                self._error = self.engine.error or "扫描失败"
            else:
                self.root_node = node
                self.expanded.add(node)      # 默认展开第一层
        except Exception as e:                # noqa: BLE001
            self._error = str(e)
        self._done = True

    def _tick(self, _dt):
        if not self._done:
            if self.engine is None:
                return
            snap = self.engine.snapshot()
            pct, tag = C.estimate_scan_percent(snap, volume_scan=True)
            if pct is None:
                self.ids.prog.value = 0
                self.ids.phase.text = "枚举中"
            else:
                work = snap["dirs"] + snap["files"]
                self.ids.prog.value = self.tracker.update(pct, work, snap)
                self.ids.phase.text = tag
            return

        if self._tick_ev is not None:
            self._tick_ev.cancel()
            self._tick_ev = None
        self._on_finished()

    def _on_finished(self):
        self.ids.btn_start.disabled = False
        self.ids.btn_stop.disabled = True
        self.ids.phase.text = ""
        if self._error:
            self.ids.prog.value = 0
            self.set_status("扫描失败：%s" % self._error)
            return
        self.ids.prog.value = 100
        e = self.engine
        self.set_status("完成：目录 %d · 文件 %d · 合计 %s · 耗时 %s%s"
                        % (e.dirs_scanned, e.files_found,
                           C.fmt_size(e.bytes_found),
                           C.fmt_duration(e.finished_at - e.started_at),
                           "（跳过 %d）" % e.skipped if e.skipped else ""))
        self.rebuild()

    def stop_scan(self):
        if self.engine is not None:
            self.engine.cancel()
        self.set_status("已停止")

    # ---------------------------------------------------------------- 筛选
    def schedule_filter(self, *_):
        if self._filter_ev is not None:
            self._filter_ev.cancel()
        self._filter_ev = Clock.schedule_once(self.apply_filter, 0.35)

    def _num(self, widget):
        t = (widget.text or "").strip()
        if not t:
            return None
        try:
            return float(t)
        except ValueError:
            return None

    def apply_filter(self, *_):
        if self.root_node is None:
            return
        smin, smax = self._num(self.ids.smin), self._num(self.ids.smax)
        dmin, dmax = self._num(self.ids.dmin), self._num(self.ids.dmax)
        flt = None
        if any(v is not None for v in (smin, smax, dmin, dmax)):
            flt = C.NodeFilter(size_min=smin, size_max=smax,
                               days_min=dmin, days_max=dmax)
        C.apply_filter(self.root_node, flt)

        key = None
        for k, name in C.SORT_MODES:
            if name == self.ids.sort.text:
                key = k
                break
        if key:
            C.sort_tree(self.root_node, key, C.DEFAULT_METRIC)
        self.rebuild()

    def reset_filter(self):
        for w in (self.ids.smin, self.ids.smax, self.ids.dmin, self.ids.dmax):
            w.text = ""
        self.apply_filter()

    # ---------------------------------------------------------------- 展开/勾选
    def toggle_expand(self, node):
        if not node.is_dir:
            return
        if node in self.expanded:
            self.expanded.discard(node)
        else:
            self.expanded.add(node)
        self.rebuild()

    def set_checked(self, node, active):
        if active:
            self.checked.add(node)
        else:
            self.checked.discard(node)
        self._sync_selection_ui()

    def _sync_selection_ui(self):
        n = len(self.checked)
        if n == 0:
            self.ids.sel.text = "未选择"
            self.ids.btn_del.disabled = True
            self.ids.btn_copy.disabled = True
        else:
            s = C.selection_summary(list(self.checked))
            self.ids.sel.text = "已选 %d 项 · 合计 %s" % (n, C.fmt_size(s["bytes"]))
            self.ids.btn_del.disabled = False
            self.ids.btn_copy.disabled = False
        self.rebuild()

    # ---------------------------------------------------------------- 列表
    def rebuild(self):
        """把树展平成 RecycleView 的数据列表"""
        if self.root_node is None:
            self.ids.rv.data = []
            return
        now = time.time()

        def row_for(node, depth):
            ts = C.metric_ts(node, C.DEFAULT_METRIC)
            return {
                "node": node,
                "indent": depth,
                "name_text": node.name,
                "size_text": C.fmt_size(node.size),
                "time_color": get_color_from_hex(
                    C.BUCKET_COLORS[C.age_bucket(ts, now)]),
                "size_color": get_color_from_hex(
                    C.BUCKET_COLORS[C.size_bucket(node.size)]),
                "checked": node in self.checked,
                "is_dir": node.is_dir,
            }

        # 前序遍历，用显式栈（不用递归，避免极深目录爆栈）。
        # 关键：弹出一个节点 -> 输出它 -> 把它的子节点压到栈顶，
        # 这样子节点会先于"后面的兄弟节点"被处理，顺序才是正确的。
        rows = []
        stack = []

        def push_children(node, depth):
            ch = C.visible_children(node)
            for i in range(len(ch) - 1, -1, -1):
                stack.append((ch[i], depth))

        push_children(self.root_node, 0)
        while stack:
            node, depth = stack.pop()
            rows.append(row_for(node, depth))
            if node.is_dir and node in self.expanded:
                push_children(node, depth + 1)
        self.ids.rv.data = rows

    # ---------------------------------------------------------------- 操作
    def copy_paths(self):
        paths = C.node_paths(list(self.checked))
        if not paths:
            return
        try:
            Clipboard.copy(C.CLIPBOARD_SEP.join(paths))
            self.set_status("已复制 %d 条路径" % len(paths))
        except Exception as e:                 # noqa: BLE001
            self.set_status("复制失败：%s" % e)

    def delete_selected(self):
        nodes = list(self.checked)
        if not nodes:
            return
        exists, missing = C.split_existing(nodes)
        if not exists:
            self.set_status("选中的项都已不存在，建议重新扫描")
            return

        def do_delete(targets):
            if self._popup is not None:
                self._popup.dismiss()
                self._popup = None
            paths = C.node_paths(targets)
            ok, failed = C.delete_permanently(paths)
            notify_media_scanner([p for p in paths if not C.path_exists(p)])
            gone = [n for n in targets if not C.path_exists(n.path)]
            if gone:
                C.remove_nodes(gone)
                if self.root_node is not None:
                    C.aggregate_sizes(self.root_node)
                self.checked = set(n for n in self.checked
                                   if C.path_exists(n.path))
            self.rebuild()
            self._sync_selection_ui()
            self.set_status("已删除 %d 项%s（已通知媒体库刷新）"
                            % (ok, "，%d 项失败" % len(failed) if failed else ""))
            toast("已删除 %d 项" % ok)

        content = ConfirmDelete(exists, on_confirm=do_delete)
        self._popup = Popup(title="确认删除", content=content,
                            size_hint=(0.95, 0.75))
        content.popup = self._popup
        # 取消按钮默认获得焦点，回车不会误删
        self._popup.open()

    def set_status(self, msg):
        self.ids.status.text = msg
        print("[status]", msg)


class ScanApp(App):
    title = "存储扫描"

    def build(self):
        return RootWidget()

    def on_start(self):
        if not has_all_files_access():
            request_all_files_access()


def main():
    ScanApp().run()


if __name__ == "__main__":
    main()
