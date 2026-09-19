# -*- coding: utf-8 -*-
"""
corescan 验证脚本 —— 在真实文件系统上跑完整流程。

不依赖任何 GUI 库，桌面和手机上都能跑。用法：
    python test_core.py
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corescan as C                                    # noqa: E402

PASS = 0
FAIL = []


def check(cond, label):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(label)
        print("   [失败] %s" % label)


# ==============================================================================
# 1. 造一棵已知结构的树
# ==============================================================================
def build_tree(root):
    """root/
         big/          (3 个子文件)
            a.bin      3 MB   新
            b.bin      2 MB   新
            c.bin      1 MB   新
         small/
            d.txt      100 B  新
            e.txt      200 B  新
         old/
            f.dat      500 KB 很久以前(400 天)
            g.dat      300 KB 很久以前(400 天)
         top.bin       1 MB   新
    """
    def w(path, nbytes, age_days=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * nbytes)
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(path, (t, t))

    w(os.path.join(root, "big", "a.bin"), 3 * 1024 * 1024)
    w(os.path.join(root, "big", "b.bin"), 2 * 1024 * 1024)
    w(os.path.join(root, "big", "c.bin"), 1 * 1024 * 1024)
    w(os.path.join(root, "small", "d.txt"), 100)
    w(os.path.join(root, "small", "e.txt"), 200)
    w(os.path.join(root, "old", "f.dat"), 500 * 1024, age_days=400)
    w(os.path.join(root, "old", "g.dat"), 300 * 1024, age_days=400)
    w(os.path.join(root, "top.bin"), 1024 * 1024)


# ==============================================================================
# 2. 主流程
# ==============================================================================
def main():
    print("=" * 64)
    print("corescan 验证（在真实文件系统上跑）")
    print("=" * 64)

    # ---- 纯函数 ----
    print("\n[1] 纯函数")
    check(C.fmt_size(512) == "512B", "fmt_size(512)")
    check(C.fmt_size(1024) == "1.0KB", "fmt_size(1024)")
    check(C.fmt_size(1536 ** 2 * 2)[-2:] in ("KB", "MB", "GB"), "fmt_size 有单位")
    check(C.size_bucket(0) == "fresh", "size_bucket 小=green")
    check(C.size_bucket(C.SIZE_MID) == "mid", "size_bucket 中=yellow")
    check(C.size_bucket(C.SIZE_BIG) == "old", "size_bucket 大=red")
    now = time.time()
    check(C.age_bucket(now) == "fresh", "age_bucket 新=green")
    check(C.age_bucket(now - 30 * 86400) == "mid", "age_bucket 30天=yellow")
    check(C.age_bucket(now - 400 * 86400) == "old", "age_bucket 400天=red")
    check(C.fmt_date(0) is not None, "fmt_date 不抛异常")

    # ---- 扫描 ----
    print("[2] 多线程扫描")
    root = tempfile.mkdtemp(prefix="corescan_")
    try:
        build_tree(root)
        eng = C.ScanEngine()
        tree = eng.scan(root)
        check(tree is not None, "scan 返回非空")
        check(eng.error is None, "scan 无错误")
        check(eng.files_found == 8, "文件数 = 8（实得 %d）" % eng.files_found)
        check(eng.dirs_scanned == 4, "目录数 = 4（实得 %d）" % eng.dirs_scanned)
        check(eng.skipped == 0, "无跳过项（实得 %d）" % eng.skipped)

        # ---- 体积聚合 ----
        print("[3] 体积聚合")
        expect_total = (3 + 2 + 1 + 1) * 1024 * 1024 + 300 + (500 + 300) * 1024
        check(tree.size == expect_total,
              "根目录聚合体积 = %d（实得 %d）" % (expect_total, tree.size))
        by = {c.name: c.size for c in tree.children}
        check(by["big"] == 6 * 1024 * 1024, "big/ 聚合 = 6MB（实得 %d）" % by.get("big"))
        check(by["small"] == 300, "small/ 聚合 = 300B（实得 %d）" % by.get("small"))
        check(by["old"] == 800 * 1024, "old/ 聚合 = 800KB（实得 %d）" % by.get("old"))
        check(by["top.bin"] == 1024 * 1024, "top.bin = 1MB（实得 %d）" % by.get("top.bin"))

        # ---- 排序 ----
        print("[4] 排序")
        C.sort_tree(tree, "size_desc")
        names = [c.name for c in tree.children]
        check(names[0] == "big", "按体积降序：最大的是 big（实得 %s）" % names[0])
        C.sort_tree(tree, "size_asc")
        names = [c.name for c in tree.children]
        check(names[0] == "small", "按体积升序：最小的是 small（实得 %s）" % names[0])

        # ---- 筛选 ----
        print("[5] 筛选")
        flt = C.NodeFilter(size_min=0.0005)          # >= 0.5MB
        C.apply_filter(tree, flt)
        vis = [c.name for c in C.visible_children(tree)]
        check("small" not in vis, "筛选掉小于 0.5MB 的 small（可见：%s）" % vis)
        check("big" in vis, "保留 big")
        C.apply_filter(tree, None)
        vis = [c.name for c in C.visible_children(tree)]
        check(len(vis) == 4, "清除筛选后恢复 4 项（实得 %d）" % len(vis))

        flt2 = C.NodeFilter(days_min=200)            # 200 天以上
        C.apply_filter(tree, flt2)
        vis = [c.name for c in C.visible_children(tree)]
        check("old" in vis, "时间筛选保留 old（可见：%s）" % vis)
        C.apply_filter(tree, None)

        # ---- 路径与统计 ----
        print("[6] 路径 / 统计")
        big = [c for c in tree.children if c.name == "big"][0]
        check(big.path == os.path.join(root, "big"),
              "Node.path 拼接正确（实得 %s）" % big.path)
        # subtree_stats 返回 (文件数, 目录数, 字节数) 三元组
        n_files, n_dirs, n_bytes = C.subtree_stats(big)
        check(n_files == 3, "subtree_stats 文件数 = 3（实得 %s）" % n_files)
        check(n_dirs == 0, "subtree_stats 目录数 = 0（实得 %s）" % n_dirs)
        check(n_bytes == 6 * 1024 * 1024, "subtree_stats 字节 = 6MB（实得 %s）" % n_bytes)
        sel = C.selection_summary([big])
        check(sel["count"] == 1, "selection_summary 计数 = 1")
        check(sel["child_files"] == 3, "selection_summary 子文件 = 3（实得 %s）"
              % sel.get("child_files"))

        # ---- 删除（真实删文件）----
        print("[7] 删除")
        target = os.path.join(root, "small")
        ok, failed = C.delete_permanently([target])
        check(ok == 1, "删除成功数 = 1（实得 %d）" % ok)
        check(not failed, "无失败项（实得 %s）" % failed)
        check(not os.path.exists(target), "目录确实从磁盘消失")
        check(C.path_exists(target) is False, "path_exists 返回 False")
        ok2, failed2 = C.delete_permanently([os.path.join(root, "不存在的文件")])
        check(ok2 == 0 and len(failed2) == 1, "删除不存在的项计入失败")

        # ---- 进度模型 ----
        print("[8] 进度模型")
        tr = C.ProgressTracker()
        tr.reset()
        snap = {"phase": "enum", "dirs": 10, "files": 100, "skipped": 0,
                "bytes": 1024, "pending": 5, "entries_per_dir": 8.0,
                "subdir_ratio": 0.35, "bytes_per_dir": 1000.0,
                "elapsed": 1.0, "disk_total": 0, "disk_used": 0,
                "phase_done": 0, "phase_total": 0, "current": "",
                "truncated": False, "cancelled": False, "running": True}
        # estimate_scan_percent 返回 (百分比 或 None, 依据标签)
        p1, tag1 = C.estimate_scan_percent(snap, volume_scan=False)
        check(p1 is None or 0.0 <= p1 <= 100.0,
              "子目录扫描：百分比在 0~100 或为 None（实得 %s / %s）" % (p1, tag1))

        # 卷根扫描能拿到真实分母，此时应给出确定数值
        snap2 = dict(snap, disk_total=1000, disk_used=500)
        p2, tag2 = C.estimate_scan_percent(snap2, volume_scan=True)
        check(p2 is not None and 0.0 <= p2 <= 100.0,
              "卷根扫描：给出确定百分比（实得 %s / %s）" % (p2, tag2))

        # done 阶段必定是 100
        p3, _ = C.estimate_scan_percent(dict(snap, phase="done"), volume_scan=False)
        check(p3 == 100.0, "done 阶段 = 100（实得 %s）" % p3)

        # ProgressTracker.update(pct, work_done, snap, now=None) -> float
        v1 = tr.update(10.0, 110, snap)
        check(0.0 <= v1 <= 100.0, "tracker 输出在 0~100（实得 %s）" % v1)
        v2 = tr.update(90.0, 500, snap)
        check(v2 >= v1, "进度单调不回退（%s -> %s）" % (v1, v2))
        v3 = tr.update(5.0, 600, snap)          # 故意给更小的值
        check(v3 >= v2, "给了更小的值也不会倒退（%s -> %s）" % (v2, v3))
        check(C.fmt_duration(65) is not None, "fmt_duration 不抛异常")
        check(C._clamp01(-1) == 0.0 and C._clamp01(2) == 1.0, "_clamp01 边界")

        # ---- 扫描根目录 ----
        print("[9] 扫描根目录")
        roots = C.scan_roots()
        check(len(roots) >= 1, "scan_roots 至少返回一个根目录（实得 %s）" % roots)

        # ---- 取消 ----
        print("[10] 取消")
        eng2 = C.ScanEngine()
        import threading as _th
        _th.Thread(target=lambda: eng2.scan(root), daemon=True).start()
        time.sleep(0.05)
        eng2.cancel()
        time.sleep(0.2)
        check(eng2.cancelled is True, "cancel() 后 cancelled=True")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    print("=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
    if FAIL:
        print("\n失败列表：")
        for x in FAIL:
            print("  -", x)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
