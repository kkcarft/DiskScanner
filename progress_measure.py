# -*- coding: utf-8 -*-
"""验证进度模型：把模型算出的百分比与"时间进度"（实际进展）对比"""
import os, sys, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as S

def run(target):
    vol = S.is_volume_root(target)
    eng = S.ScanEngine(max_nodes=4_000_000)
    tr = S.ProgressTracker()
    rows = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            snap = eng.snapshot()
            pct, basis = S.estimate_scan_percent(snap, vol)
            shown = tr.update(pct, snap["dirs"] + snap["files"], snap)
            if pct is not None:
                rows.append((time.perf_counter(), shown, basis, snap))
            time.sleep(0.15)

    th = threading.Thread(target=poll, daemon=True); th.start()
    t0 = time.perf_counter()
    eng.scan(target)
    dur = time.perf_counter() - t0
    stop.set(); th.join(timeout=1)

    print("=" * 92)
    print("目标 %s   卷根=%s   耗时 %.1fs   条目 %s   体积 %.1fGB / 卷已用 %.1fGB"
          % (target, vol, dur, f"{eng.dirs_scanned+eng.files_found:,}",
             eng.bytes_found/1024**3, eng.disk_used/1024**3))
    print("  时间进度%   模型进度%   偏差      依据")
    step = max(1, len(rows)//14)
    errs = []
    for i in range(0, len(rows), step):
        t, shown, basis, snap = rows[i]
        tp = (t - t0)/dur*100
        errs.append(abs(shown - tp))
        print("   %6.1f     %6.1f    %+6.1f    %s" % (tp, shown, shown - tp, basis))
    if rows:
        t, shown, basis, snap = rows[-1]
        tp = (t-t0)/dur*100
        errs.append(abs(shown-tp))
        print("   %6.1f     %6.1f    %+6.1f    %s" % (tp, shown, shown-tp, basis))
    print("  平均绝对偏差 %.1f%%   最大偏差 %.1f%%" % (sum(errs)/len(errs), max(errs)))
    return errs

if __name__ == "__main__":
    for tg in (sys.argv[1:] or ["C:/"]):
        if os.path.isdir(tg):
            run(tg)
            print()
