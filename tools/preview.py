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

if tab == 1 and not app.ip_items:
    demo = [("本机回环", "127.0.0.1", ""), ("内网网关", "10.254.217.254", ""),
            ("Google DNS", "8.8.8.8", ""), ("阿里 DNS", "223.5.5.5", "")]
    for i, (n, ip, note) in enumerate(demo, 1):
        app.ip_items.append({"id": i, "name": n, "ip": ip, "note": note,
                             "ok": None, "ms": 0.0, "err": "", "ts": "",
                             "web": None, "web_label": "", "web_detail": "", "web_ts": 0.0})
    app._ip_seq = len(demo)
    app._render_ip_table()
    root.after(1500, lambda: app._check_ips(app.ip_items, force_web=True))

if tab == 2 and not app.probes:
    demo = [("本机 SMB", "127.0.0.1", 445), ("网关 DNS", "192.168.61.254", 53)]
    for i, (n, h, p) in enumerate(demo, 1):
        app.probes.append({"id": i, "name": n, "host": h, "port": p,
                           "ok": None, "ms": 0.0, "err": "", "ts": ""})
    app._probe_seq = len(demo)
    app._refresh_probe_table()
    root.after(2000, app._probe_all)

root.after(1200, lambda: app._select_tab(tab))
root.mainloop()
