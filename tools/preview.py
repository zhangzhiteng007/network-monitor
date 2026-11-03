# -*- coding: utf-8 -*-
"""开发预览辅助：启动主程序并自动切换到指定页签。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import tkinter as tk  # noqa: E402
import netmon  # noqa: E402

root = tk.Tk()
app = netmon.App(root)
tab = int(sys.argv[1]) if len(sys.argv) > 1 else 0

if tab == 2 and not app.probes:
    demo = [("本机 SMB", "127.0.0.1", 445), ("网关 DNS", "192.168.61.254", 53)]
    for i, (n, h, p) in enumerate(demo, 1):
        app.probes.append({"id": i, "name": n, "host": h, "port": p,
                           "ok": None, "ms": 0.0, "err": "", "ts": ""})
    app._probe_seq = len(demo)
    app._refresh_probe_table()
    root.after(2000, app._probe_all)

root.after(1200, lambda: app.nb.select(tab))
root.mainloop()
