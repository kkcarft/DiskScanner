# -*- coding: utf-8 -*-
"""
界面冒烟测试：真的把 Kivy App 跑起来，建一棵测试树，自动扫描，然后退出。

会短暂弹出一个窗口（几秒），这是唯一能真正验证 KV 语法、控件构建、
扫描回调、列表刷新、删除流程的办法 —— 静态检查测不出这些。

用法：
    python smoke_ui.py
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 造一棵小测试树，让 App 扫它而不是扫整个盘
ROOT = tempfile.mkdtemp(prefix="smoke_")


def build():
    def w(rel, n, age=0):
        p = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * n)
        if age:
            t = time.time() - age * 86400
            os.utime(p, (t, t))
    w("big/a.bin", 3 * 1024 * 1024)
    w("big/b.bin", 2 * 1024 * 1024)
    w("small/c.txt", 100)
    w("small/d.txt", 200)
    w("old/e.dat", 500 * 1024, age=400)
    w("top.bin", 1024 * 1024)


build()
os.environ["CORESCAN_ROOT"] = ROOT

import main                                          # noqa: E402  (KV 在此解析)
from kivy.clock import Clock                         # noqa: E402

ERRORS = []
STEPS = []


def step(name, cond, extra=""):
    STEPS.append((name, bool(cond), extra))
    print("  %-42s %s %s" % (name, "OK" if cond else "失败", extra))


app = main.ScanApp()
state = {"t": 0}


def phase_1_scan(dt):
    try:
        app.root.start_scan()
        step("调用 start_scan()", True)
    except Exception as e:                            # noqa: BLE001
        step("调用 start_scan()", False, repr(e))
        ERRORS.append(e)


def phase_2_check(dt):
    """等扫描结束，检查结果"""
    try:
        r = app.root
        if r.root_node is None:
            step("扫描产出结果树", False, "root_node 仍为空")
            return
        step("扫描产出结果树", True,
             "文件 %d / 目录 %d" % (r.engine.files_found, r.engine.dirs_scanned))
        rows = r.ids.rv.data
        step("列表已填充", len(rows) > 0, "%d 行" % len(rows))

        # 展开一个目录
        dirs = [x for x in rows if x["is_dir"]]
        if dirs:
            r.toggle_expand(dirs[0]["node"])
            n2 = len(r.ids.rv.data)
            step("展开目录后行数增加", n2 > len(rows), "%d -> %d 行" % (len(rows), n2))

        # 勾选一项
        r.set_checked(rows[0]["node"], True)
        step("勾选生效", len(r.checked) == 1, "已选 %d" % len(r.checked))
        step("删除按钮解锁", r.ids.btn_del.disabled is False)

        # 筛选
        r.ids.smin.text = "0.5"
        r.apply_filter()
        n3 = len(r.ids.rv.data)
        step("筛选后行数变化", n3 != len(rows), "筛选前 %d -> %d 行" % (len(rows), n3))
        r.reset_filter()

        # 复制路径
        try:
            r.copy_paths()
            step("复制路径不报错", True)
        except Exception as e:                        # noqa: BLE001
            step("复制路径不报错", False, repr(e))

        # 真实删除一个小文件（不弹确认框，直接调底层）
        import corescan as C
        tgt = [x for x in rows if not x["is_dir"]][0]["node"]
        p = tgt.path
        ok, failed = C.delete_permanently([p])
        step("删除真实文件", ok == 1 and not os.path.exists(p), str(failed))
    except Exception as e:                            # noqa: BLE001
        ERRORS.append(e)
        step("检查阶段", False, repr(e))


def phase_3_quit(dt):
    app.stop()


Clock.schedule_once(phase_1_scan, 0.5)
Clock.schedule_once(phase_2_check, 3.0)
Clock.schedule_once(phase_3_quit, 4.5)

print("=" * 64)
print("界面冒烟测试（会弹窗几秒）  扫描目标：", ROOT)
print("=" * 64)
app.run()

shutil.rmtree(ROOT, ignore_errors=True)

print()
print("=" * 64)
bad = [s for s in STEPS if not s[1]]
print("步骤：通过 %d / %d" % (len(STEPS) - len(bad), len(STEPS)))
if bad:
    print("\n失败项：")
    for n, _ok, ex in bad:
        print("  -", n, ex)
if ERRORS:
    print("\n异常：")
    for e in ERRORS:
        print("  -", repr(e))
print("=" * 64)
sys.exit(1 if (bad or ERRORS) else 0)
