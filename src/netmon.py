# -*- coding: utf-8 -*-
"""
网络监控中心 (NetMonitor) — Windows 11 绿色便携版
功能：实时监控本机网卡 / IP / 端口状态，查看并修改网卡跃点数（Metric）。
"""
import ctypes
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog

import netinfo

APP_NAME = "网络监控中心"
APP_VER = "1.3.1"
FONT = "Microsoft YaHei UI"

# ---------- 配色（浅色简洁风） ----------
BG = "#EEF2F7"
CARD = "#FFFFFF"
LINE = "#E2E8F0"
TEXT = "#1B2733"
MUTED = "#77869B"
PRIMARY = "#0F766E"
PRIMARY_DK = "#0B5B55"
PRIMARY_BG = "#E6F4F1"
OK = "#0F7B4F"
WARN = "#B45309"
ERR = "#C23B32"
ROW_ALT = "#F8FAFC"


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    d = app_dir()
    probe = os.path.join(d, ".wtest")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return os.path.join(d, "config.json")
    except Exception:
        fallback = os.path.join(os.environ.get("LOCALAPPDATA", d), "NetMonitor")
        os.makedirs(fallback, exist_ok=True)
        return os.path.join(fallback, "config.json")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fmt_rate(v):
    if v >= 1048576:
        return f"{v / 1048576:.1f} MB/s"
    if v >= 1024:
        return f"{v / 1024:.0f} KB/s"
    return f"{v:.0f} B/s"


def fmt_link(bps):
    if not bps or bps > 1e11:
        return "—"
    if bps >= 1e9:
        return f"{bps / 1e9:.1f} Gbps"
    if bps >= 1e6:
        return f"{bps / 1e6:.0f} Mbps"
    return f"{bps / 1e3:.0f} Kbps"


def parse_bulk_text(text):
    """解析批量导入文本，返回 [(设备名, IP, 备注), ...]。

    支持「设备名,IP」「设备名 IP」「IP」三种写法；逗号/分号/Tab/空格分隔。
    """
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.search(r"[,;\t]", line):
            parts = [p.strip() for p in re.split(r"[,;\t]+", line) if p.strip()]
            ip = next((p for p in parts if netinfo.is_ip(p)), "")
            others = [p for p in parts if p != ip]
            name = others[0] if others else ""
            note = " ".join(others[1:]) if len(others) > 1 else ""
        else:
            toks = line.split()
            ip = next((t for t in toks if netinfo.is_ip(t)), "")
            if not ip:
                continue
            idx = toks.index(ip)
            name = " ".join(toks[:idx])
            note = " ".join(toks[idx + 1:])
        if ip:
            items.append((name, ip, note))
    return items


class Collector(threading.Thread):
    """后台采集线程，避免阻塞界面。"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_evt = threading.Event()
        self._prev_io = {}
        self._prev_t = None

    def run(self):
        while not self.stop_evt.is_set():
            started = time.time()
            if not self.app.paused:
                try:
                    self.app.push_snapshot(self._collect())
                except Exception as exc:  # 采集失败不应终止线程
                    self.app.push_error(str(exc))
            self.stop_evt.wait(max(0.3, self.app.interval - (time.time() - started)))

    def _collect(self):
        import psutil

        adapters = netinfo.get_adapters()
        ports = netinfo.get_ports()
        try:
            io = psutil.net_io_counters(pernic=True)
        except Exception:
            io = {}
        now = time.time()
        dt = (now - self._prev_t) if self._prev_t else 0
        rates = {}
        for name, c in io.items():
            prev = self._prev_io.get(name)
            if prev and dt > 0:
                rates[name] = (max(0.0, (c.bytes_recv - prev.bytes_recv) / dt),
                               max(0.0, (c.bytes_sent - prev.bytes_sent) / dt))
            else:
                rates[name] = (0.0, 0.0)
        self._prev_io = io
        self._prev_t = now
        return {"adapters": adapters, "ports": ports, "rates": rates, "ts": now}


class App:
    def __init__(self, root):
        self.root = root
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.snap = None
        self.adapters = []
        self.ports = []
        self.rates = {}
        self.last_ts = None
        self.paused = False
        self.interval = 2.0
        self.select_guid = None
        self._metric_dirty = False
        self.switch_cfg = {}
        self.switch_mode = "unknown"
        self._switching = False
        self._busy = False
        self._port_sig = None
        self._ad_sig = None
        self._error_shown = False

        self.q = queue.Queue()
        self.probe_q = queue.Queue()
        self.probes = []
        self._probe_seq = 0
        self._probing = set()

        self.ip_items = []
        self._ip_seq = 0
        self.ip_q = queue.Queue()
        self.auto_check = True

        self.admin = is_admin()

        self.root.title(f"{APP_NAME} v{APP_VER}")
        self.root.configure(bg=BG)
        self._set_icon()
        w, h = self.px(1240), self.px(820)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.root.geometry(f"{min(w, sw - 60)}x{min(h, sh - 90)}+{(sw - min(w, sw - 60)) // 2}+{max(20, (sh - min(h, sh - 90)) // 2 - 30)}")
        self.root.minsize(self.px(1000), self.px(640))

        self._build_style()
        self._load_config()
        self.var_interval = tk.StringVar(
            value=f"{int(self.interval)} 秒" if int(self.interval) in (1, 2, 3, 5) else "2 秒")
        self._build_ui()
        self._refresh_probe_table()

        self.collector = Collector(self)
        self.collector.start()
        self._tick()
        self._auto_probe()
        self._auto_check_loop()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 尺寸/字体辅助 ----------
    def px(self, v):
        return int(round(v * self.scale))

    def fp(self, size, bold=False):
        return (FONT, -self.px(size), "bold" if bold else "normal")

    def _set_icon(self):
        try:
            ico = resource_path("icon.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    # ---------- 样式 ----------
    def _build_style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", font=self.fp(12), background=BG, foreground=TEXT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=TEXT, font=self.fp(12))
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=self.fp(11))
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=self.fp(11))
        st.configure("Title.TLabel", background=CARD, foreground=TEXT, font=self.fp(17, True))
        st.configure("H2.TLabel", background=CARD, foreground=TEXT, font=self.fp(12, True))

        st.configure("TButton", font=self.fp(12), padding=(self.px(12), self.px(6)),
                     background="#F4F6F9", foreground=TEXT, borderwidth=1,
                     bordercolor=LINE, focusthickness=0, relief="flat")
        st.map("TButton",
               background=[("active", "#E9EEF5"), ("disabled", "#F4F6F9")],
               foreground=[("disabled", "#A9B4C2")],
               bordercolor=[("active", "#CBD5E1")])

        st.configure("Primary.TButton", background=PRIMARY, foreground="white",
                     bordercolor=PRIMARY, font=self.fp(12, True))
        st.map("Primary.TButton",
               background=[("active", PRIMARY_DK), ("disabled", "#A9C6C1")],
               bordercolor=[("active", PRIMARY_DK), ("disabled", "#A9C6C1")])

        st.configure("Ghost.TButton", background=CARD, foreground=PRIMARY,
                     bordercolor="#CFE3DF", font=self.fp(12))
        st.map("Ghost.TButton", background=[("active", PRIMARY_BG)])

        st.configure("TEntry", fieldbackground=CARD, bordercolor="#CBD5E1",
                     lightcolor="#CBD5E1", darkcolor="#CBD5E1",
                     insertcolor=TEXT, padding=(self.px(7), self.px(5)), font=self.fp(12))
        st.map("TEntry", bordercolor=[("focus", PRIMARY)], lightcolor=[("focus", PRIMARY)])

        st.configure("TCombobox", fieldbackground=CARD, background=CARD, bordercolor="#CBD5E1",
                     arrowcolor=MUTED, padding=(self.px(6), self.px(4)), font=self.fp(12))
        st.map("TCombobox", fieldbackground=[("readonly", CARD)], bordercolor=[("focus", PRIMARY)])
        self.root.option_add("*TCombobox*Listbox.font", self.fp(12))
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.selectBackground", PRIMARY_BG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", PRIMARY_DK)

        st.configure("TCheckbutton", background=CARD, foreground=TEXT, font=self.fp(12),
                     indicatorcolor=CARD, focuscolor=BG)
        st.map("TCheckbutton", indicatorcolor=[("selected", PRIMARY)],
               background=[("active", CARD)])

        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED, borderwidth=0,
                     padding=(self.px(18), self.px(9)), font=self.fp(12))
        st.map("TNotebook.Tab",
               background=[("selected", CARD)],
               foreground=[("selected", PRIMARY)],
               font=[("selected", self.fp(12, True))],
               expand=[("selected", (0, 0, 0, 0))])

        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                     borderwidth=0, rowheight=self.px(29), font=self.fp(12))
        st.configure("Treeview.Heading", background="#F5F8FB", foreground="#5C6B7E",
                     relief="flat", borderwidth=0, font=self.fp(11, True),
                     padding=(self.px(8), self.px(7)))
        st.map("Treeview.Heading", background=[("active", "#EBF1F7")])
        st.map("Treeview", background=[("selected", PRIMARY_BG)],
               foreground=[("selected", PRIMARY_DK)])

        st.configure("Slim.Vertical.TScrollbar", background="#D8E0E9", troughcolor=CARD,
                     borderwidth=0, arrowsize=self.px(11), width=self.px(9),
                     relief="flat", arrowcolor=MUTED)
        st.map("Slim.Vertical.TScrollbar", background=[("active", "#C3CEDB")])

    # ---------- 界面 ----------
    def _build_ui(self):
        self._build_header()
        self.statusbar = tk.Frame(self.root, bg=CARD, height=self.px(32))
        self.statusbar.pack(side="bottom", fill="x")
        self.statusbar.pack_propagate(False)
        tk.Frame(self.statusbar, bg=LINE, height=1).place(x=0, y=0, relwidth=1)

        self.lbl_stat_left = tk.Label(self.statusbar, bg=CARD, fg=MUTED, font=self.fp(11), anchor="w")
        self.lbl_stat_left.pack(side="left", padx=self.px(22))
        self.lbl_stat_right = tk.Label(self.statusbar, bg=CARD, fg=MUTED, font=self.fp(11), anchor="e")
        self.lbl_stat_right.pack(side="right", padx=self.px(22))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)
        self._build_tabbar(body)

        self.tab_ad = tk.Frame(self._page_holder, bg=BG)
        self.tab_ip = tk.Frame(self._page_holder, bg=BG)
        self.tab_port = tk.Frame(self._page_holder, bg=BG)
        self.tab_probe = tk.Frame(self._page_holder, bg=BG)
        self._tab_pages = [self.tab_ad, self.tab_ip, self.tab_port, self.tab_probe]

        self._build_tab_adapters()
        self._build_tab_ip()
        self._build_tab_ports()
        self._build_tab_probe()
        self._select_tab(0)

    def _build_tabbar(self, parent):
        self._tab_idx = -1
        self._tab_hover = -1
        titles = ["网卡与 IP", "IP 连通检测", "端口状态", "端口探活"]

        self._tabbar_h = self.px(68)
        cv = tk.Canvas(parent, bg=BG, height=self._tabbar_h, highlightthickness=0,
                       bd=0, cursor="hand2")
        cv.pack(fill="x", padx=self.px(18))
        self._tabbar = cv

        f_big = tkfont.Font(font=self.fp(15, True))
        gap = self.px(3)
        self._tab_cards = []
        x = 0
        for i, title in enumerate(titles):
            w = f_big.measure(title) + self.px(46)
            self._tab_cards.append({"idx": i, "x": x, "w": w, "title": title})
            x += w + gap
        self._tabbar_end = x

        cv.bind("<Button-1>", self._on_tab_click)
        cv.bind("<Motion>", self._on_tab_motion)
        cv.bind("<Leave>", lambda e: self._set_tab_hover(-1))
        cv.bind("<Configure>", lambda e: self._redraw_tabs())

        self._page_holder = tk.Frame(parent, bg=BG)
        self._page_holder.pack(fill="both", expand=True,
                               padx=self.px(18), pady=(self.px(2), self.px(12)))
        self._redraw_tabs()

    # ---------- 页签：抽牌效果 ----------
    def _round_fill(self, cv, x1, y1, x2, y2, r, color):
        r = int(min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        if r < 2:
            cv.create_rectangle(x1, y1, x2, y2, fill=color, outline=color)
            return
        # Tk 角度以 3 点钟方向为 0 度、逆时针为正；每个角取「朝盒心」的那一象限
        corners = [((x1, y1, x1 + 2 * r, y1 + 2 * r), 90),          # 左上
                   ((x2 - 2 * r, y1, x2, y1 + 2 * r), 0),           # 右上
                   ((x2 - 2 * r, y2 - 2 * r, x2, y2), 270),         # 右下
                   ((x1, y2 - 2 * r, x1 + 2 * r, y2), 180)]         # 左下
        cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline=color)
        cv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=color, outline=color)
        for box, start in corners:
            cv.create_arc(box[0], box[1], box[2], box[3], start=start, extent=90,
                          style="pieslice", fill=color, outline=color)

    def _round_rect(self, cv, x1, y1, x2, y2, r, fill, outline=None):
        # 用「描边色底层 + 填充色内层」套出 1px 边线，避免线条接缝处的毛刺
        if outline and outline != fill:
            self._round_fill(cv, x1, y1, x2, y2, r, outline)
            self._round_fill(cv, x1 + 1, y1 + 1, x2 - 1, y2 - 1, max(r - 1, 1), fill)
        else:
            self._round_fill(cv, x1, y1, x2, y2, r, fill)

    def _draw_card(self, cv, card, selected):
        x, w = card["x"], card["w"]
        h = self._tabbar_h
        r = self.px(7)
        hover = self._tab_hover == card["idx"]
        if selected:
            # 抽出的牌：更高、正立、白底，底部压住整叠牌与内容区衔接
            y1 = h - self.px(60)
            self._round_rect(cv, x + 1, y1, x + w - 1, h, r, CARD, "#C7D4E3")
            band = self.px(5)
            # 顶部色带：上沿随卡片圆角走，下沿平直
            self._round_fill(cv, x + 1, y1, x + w - 1, y1 + band + r, r, PRIMARY)
            cv.create_rectangle(x + 1, y1 + band, x + w - 1, y1 + band + r,
                                fill=PRIMARY, outline=PRIMARY)
            cv.create_text(x + w / 2, (y1 + band + h) / 2, text=card["title"],
                           fill=PRIMARY, font=self.fp(15, True))
        else:
            # 未抽出的牌：矮一截沉在下面，悬停时向上抽起一点
            lift = self.px(5) if hover else 0
            y1 = h - self.px(28) - lift
            fill = "#DEE7F2" if hover else "#D3DDEA"
            self._round_rect(cv, x + 2, y1, x + w - 2, h - 2, r, fill, "#B7C7DA")
            cv.create_text(x + w / 2, (y1 + h - 2) / 2, text=card["title"],
                           fill="#44566D" if hover else "#6B7B91",
                           font=self.fp(12, hover))

    def _redraw_tabs(self):
        cv = self._tabbar
        if not cv.winfo_exists():
            return
        cv.delete("all")
        w = cv.winfo_width()
        h = self._tabbar_h
        if w <= 1:
            return
        cv.create_line(0, h - 1, w, h - 1, fill=LINE)
        for card in self._tab_cards:
            if card["idx"] != self._tab_idx:
                self._draw_card(cv, card, False)
        if 0 <= self._tab_idx < len(self._tab_cards):
            self._draw_card(cv, self._tab_cards[self._tab_idx], True)

    def _tab_hit(self, x):
        for card in self._tab_cards:
            if card["x"] <= x < card["x"] + card["w"]:
                return card["idx"]
        return -1

    def _on_tab_click(self, ev):
        idx = self._tab_hit(ev.x)
        if idx >= 0:
            self._select_tab(idx)

    def _on_tab_motion(self, ev):
        self._set_tab_hover(self._tab_hit(ev.x))

    def _set_tab_hover(self, idx):
        if idx != self._tab_hover:
            self._tab_hover = idx
            self._redraw_tabs()

    def _select_tab(self, idx):
        if idx == self._tab_idx:
            return
        self._tab_idx = idx
        for i, page in enumerate(self._tab_pages):
            if i == idx:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        self._redraw_tabs()

    def _build_header(self):
        head = tk.Frame(self.root, bg=CARD, height=self.px(70))
        head.pack(fill="x")
        head.pack_propagate(False)

        left = tk.Frame(head, bg=CARD)
        left.pack(side="left", fill="y", padx=self.px(22))
        tk.Label(left, text=APP_NAME, bg=CARD, fg=TEXT, font=self.fp(17, True)).pack(anchor="w", pady=(self.px(12), 0))
        tk.Label(left, text=f"适配器 · IP 连通 · 端口 · 跃点数    实时监控   ·   v{APP_VER}",
                 bg=CARD, fg=MUTED, font=self.fp(11)).pack(anchor="w")

        right = tk.Frame(head, bg=CARD)
        right.pack(side="right", fill="y", padx=self.px(22))

        badge_bg = PRIMARY_BG if self.admin else "#FDF1E3"
        badge_fg = PRIMARY_DK if self.admin else WARN
        badge_txt = "管理员模式" if self.admin else "受限模式 · 点击提权"
        self.badge = tk.Label(right, text=badge_txt, bg=badge_bg, fg=badge_fg,
                              font=self.fp(11, True), padx=self.px(10), pady=self.px(4), cursor="hand2")
        self.badge.pack(side="right", pady=(self.px(19), 0))
        if not self.admin:
            self.badge.bind("<Button-1>", lambda e: self._elevate())

        self.btn_pause = ttk.Button(right, text="暂停", width=6, command=self._toggle_pause)
        self.btn_pause.pack(side="right", padx=(self.px(8), self.px(10)), pady=(self.px(18), 0))

        cb = ttk.Combobox(right, textvariable=self.var_interval, values=["1 秒", "2 秒", "3 秒", "5 秒"],
                          width=6, state="readonly")
        cb.pack(side="right", pady=(self.px(18), 0))
        cb.bind("<<ComboboxSelected>>", self._on_interval)

        ttk.Label(right, text="刷新间隔", style="CardMuted.TLabel").pack(side="right", padx=(self.px(10), self.px(6)), pady=(self.px(19), 0))

        ttk.Button(right, text="立即刷新", command=self._force_refresh).pack(
            side="right", padx=(0, self.px(8)), pady=(self.px(18), 0))

        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x")

    # --- Tab1 顶部：内外网一键切换 ---
    def _build_switch_bar(self, tab):
        bar = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        bar.pack(fill="x", pady=(0, self.px(10)))

        row = tk.Frame(bar, bg=CARD)
        row.pack(fill="x", padx=self.px(16), pady=(self.px(11), 0))
        tk.Label(row, text="内外网优先", bg=CARD, fg=TEXT, font=self.fp(12, True)).pack(side="left")
        self.btn_switch = ttk.Button(row, text="检测中…", style="Primary.TButton", width=16,
                                     command=self._toggle_mode)
        self.btn_switch.pack(side="left", padx=(self.px(14), self.px(12)))
        self.lbl_mode = tk.Label(row, text="", bg=CARD, fg=MUTED, font=self.fp(12, True))
        self.lbl_mode.pack(side="left")
        self.lbl_switch_info = tk.Label(row, text="", bg=CARD, fg=MUTED, font=self.fp(11))
        self.lbl_switch_info.pack(side="right")

        tk.Label(bar, text="切换原理：优先网卡跃点设为 10、另一张设为 100（仅调整 IPv4 默认路由，直连网段与静态路由不受影响）。",
                 bg=CARD, fg=MUTED, font=self.fp(11), anchor="w").pack(
                     fill="x", padx=self.px(16), pady=(self.px(5), self.px(10)))

    # --- Tab1 网卡 ---
    def _build_tab_adapters(self):
        tab = self.tab_ad
        self._build_switch_bar(tab)
        card = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="both", expand=True)

        cols = [("name", "适配器", 200, "w"), ("state", "状态", 86, "w"), ("fam", "类型", 62, "w"),
                ("ip", "IP 地址", 230, "w"), ("prefix", "前缀", 62, "w"),
                ("gw", "默认网关", 150, "w"), ("metric", "跃点", 64, "center"),
                ("down", "下行", 100, "e"), ("up", "上行", 100, "e")]
        self.tv_ad = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, title, width, anchor in cols:
            self.tv_ad.heading(key, text=title, anchor=anchor)
            self.tv_ad.column(key, width=self.px(width), anchor=anchor,
                              stretch=(key in ("ip", "gw")))
        self.tv_ad.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        sb = ttk.Scrollbar(card, orient="vertical", command=self.tv_ad.yview,
                           style="Slim.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
        self.tv_ad.configure(yscrollcommand=sb.set)
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)

        for i, bg in ((0, CARD), (1, ROW_ALT)):
            self.tv_ad.tag_configure(f"on{i}", background=bg, foreground=TEXT)
            self.tv_ad.tag_configure(f"off{i}", background=bg, foreground="#9AA6B5")
            self.tv_ad.tag_configure(f"loop{i}", background=bg, foreground="#9AA6B5")
        self.tv_ad.tag_configure("sep", background="#F1F5F9")

        self.tv_ad.bind("<<TreeviewSelect>>", self._on_ad_select)
        self.tv_ad.bind("<Button-3>", self._ad_menu)
        self.tv_ad.bind("<Double-1>", lambda e: self.ent_m4.focus_set())

        # 跃点数配置卡片
        conf = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        conf.pack(fill="x", pady=(self.px(10), 0))

        row1 = tk.Frame(conf, bg=CARD)
        row1.pack(fill="x", padx=self.px(16), pady=(self.px(12), 0))
        tk.Label(row1, text="跃点数配置", bg=CARD, fg=TEXT, font=self.fp(12, True)).pack(side="left")
        self.lbl_sel = tk.Label(row1, text="未选择适配器（在上方列表中单击一行）",
                                bg=CARD, fg=MUTED, font=self.fp(11))
        self.lbl_sel.pack(side="left", padx=self.px(12))

        row2 = tk.Frame(conf, bg=CARD)
        row2.pack(fill="x", padx=self.px(16), pady=(self.px(8), 0))
        tk.Label(row2, text="IPv4 跃点", bg=CARD, fg=TEXT, font=self.fp(12)).pack(side="left")
        self.var_m4 = tk.StringVar()
        self.ent_m4 = ttk.Entry(row2, textvariable=self.var_m4, width=9)
        self.ent_m4.pack(side="left", padx=(self.px(8), self.px(18)))
        tk.Label(row2, text="IPv6 跃点", bg=CARD, fg=TEXT, font=self.fp(12)).pack(side="left")
        self.var_m6 = tk.StringVar()
        self.ent_m6 = ttk.Entry(row2, textvariable=self.var_m6, width=9)
        self.ent_m6.pack(side="left", padx=(self.px(8), self.px(18)))
        for ent in (self.ent_m4, self.ent_m6):
            ent.bind("<Key>", self._mark_metric_dirty)

        self.btn_apply = ttk.Button(row2, text="应用", style="Primary.TButton", command=self._apply_metric)
        self.btn_apply.pack(side="left")
        self.btn_auto = ttk.Button(row2, text="恢复自动", command=lambda: self._apply_metric(auto=True))
        self.btn_auto.pack(side="left", padx=(self.px(8), 0))
        self.btn_reset = ttk.Button(row2, text="撤销输入", command=self._reset_metric_input)
        self.btn_reset.pack(side="left", padx=(self.px(8), 0))

        hint = ("跃点数（Metric）越大优先级越低；同一目标存在多条路由时，系统选择跃点数最小的链路。"
                "取值范围 1–9999，修改立即生效。")
        tk.Label(conf, text=hint, bg=CARD, fg=MUTED, font=self.fp(11),
                 anchor="w").pack(fill="x", padx=self.px(16), pady=(self.px(6), self.px(12)))

        self._set_metric_enabled(False)

    # --- Tab: IP 连通检测 ---
    def _build_tab_ip(self):
        tab = self.tab_ip
        self.var_auto_check = tk.BooleanVar(value=self.auto_check)

        bar = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        bar.pack(fill="x", pady=(0, self.px(10)))

        r1 = tk.Frame(bar, bg=CARD)
        r1.pack(fill="x", padx=self.px(16), pady=(self.px(11), 0))
        tk.Label(r1, text="设备名", bg=CARD, fg=MUTED, font=self.fp(11)).pack(side="left")
        self.var_ip_name = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_ip_name, width=15).pack(
            side="left", padx=(self.px(5), self.px(14)))
        tk.Label(r1, text="IP 地址", bg=CARD, fg=MUTED, font=self.fp(11)).pack(side="left")
        self.var_ip_addr = tk.StringVar()
        e_ip = ttk.Entry(r1, textvariable=self.var_ip_addr, width=17)
        e_ip.pack(side="left", padx=(self.px(5), self.px(12)))
        e_ip.bind("<Return>", lambda ev: self._add_ip_item())
        ttk.Button(r1, text="添加", style="Primary.TButton",
                   command=self._add_ip_item).pack(side="left")
        ttk.Button(r1, text="批量导入…", command=self._bulk_dialog).pack(
            side="left", padx=(self.px(8), 0))
        ttk.Button(r1, text="全部检测",
                   command=lambda: self._check_ips(self.ip_items, force_web=True)).pack(
            side="left", padx=(self.px(8), 0))
        ttk.Button(r1, text="删除选中", command=self._del_ip_items).pack(
            side="left", padx=(self.px(8), 0))
        ttk.Button(r1, text="清空", command=self._clear_ip_items).pack(
            side="left", padx=(self.px(8), 0))

        r2 = tk.Frame(bar, bg=CARD)
        r2.pack(fill="x", padx=self.px(16), pady=(self.px(8), self.px(10)))
        tk.Checkbutton(r2, text="每 10 秒自动检测", variable=self.var_auto_check, bg=CARD, fg=TEXT,
                       font=self.fp(12), activebackground=CARD, selectcolor=CARD,
                       highlightthickness=0, bd=0,
                       command=self._on_auto_check).pack(side="left")
        tk.Label(r2, text="双击行可修改设备名/备注，右键更多操作", bg=CARD, fg=MUTED,
                 font=self.fp(11)).pack(side="left", padx=(self.px(16), 0))
        self.lbl_ip_stat = tk.Label(r2, text="", bg=CARD, fg=MUTED, font=self.fp(11))
        self.lbl_ip_stat.pack(side="right")

        card = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="both", expand=True)
        cols = [("name", "设备名", 168, "w"), ("ip", "IP 地址", 142, "w"),
                ("state", "状态", 100, "w"), ("ms", "延迟", 82, "e"),
                ("gap1", "", 28, "center"),
                ("web", "Web 可达", 128, "w"),
                ("gap2", "", 22, "center"),
                ("ts", "最近检测", 96, "w"), ("note", "备注", 160, "w")]
        self.tv_ip = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings",
                                  selectmode="extended")
        for key, title, width, anchor in cols:
            self.tv_ip.heading(key, text=title, anchor=anchor)
            self.tv_ip.column(key, width=self.px(width), anchor=anchor,
                              stretch=(key in ("name", "note")))
        self.tv_ip.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        sb = ttk.Scrollbar(card, orient="vertical", command=self.tv_ip.yview,
                           style="Slim.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
        self.tv_ip.configure(yscrollcommand=sb.set)
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)
        for i, bg in ((0, CARD), (1, ROW_ALT)):
            self.tv_ip.tag_configure(f"ok{i}", background=bg, foreground=OK)
            self.tv_ip.tag_configure(f"warn{i}", background=bg, foreground=WARN)
            self.tv_ip.tag_configure(f"bad{i}", background=bg, foreground=ERR)
            self.tv_ip.tag_configure(f"wait{i}", background=bg, foreground=MUTED)
        self.tv_ip.bind("<Double-1>", self._on_ip_double)
        self.tv_ip.bind("<Button-3>", self._ip_menu)

        tk.Label(tab, text="状态 = ICMP 网络层可达（1.2 秒超时）；Web 可达 = DNS 解析 → TCP 443 → HTTPS 状态码。",
                 bg=BG, fg=MUTED, font=self.fp(11), anchor="w").pack(fill="x", pady=(self.px(8), 0))
        tk.Label(tab, text="「仅端口通」= 443 开放但非网页服务；「不可达」= 没有网页，如 8.8.8.8 本身是 DNS 服务器。"
                           "整行橙色 = 网络层在线但网页打不开。",
                 bg=BG, fg=MUTED, font=self.fp(11), anchor="w").pack(fill="x", pady=(self.px(2), 0))

        self._render_ip_table()

    # --- IP 检测逻辑 ---
    def _add_ip_item(self):
        ip = self.var_ip_addr.get().strip()
        name = self.var_ip_name.get().strip()
        if not ip:
            messagebox.showinfo(APP_NAME, "请输入 IP 地址。")
            return
        if not netinfo.is_ip(ip):
            messagebox.showwarning(APP_NAME, f"“{ip}” 不是合法的 IP 地址。")
            return
        if any(it["ip"] == ip for it in self.ip_items):
            self._toast(f"{ip} 已在列表中", "info")
            return
        self._ip_seq += 1
        item = {"id": self._ip_seq, "name": name or ip, "ip": ip, "note": "",
                "ok": None, "ms": 0.0, "err": "", "ts": "",
                "web": None, "web_label": "", "web_detail": "", "web_ts": 0.0}
        self.ip_items.append(item)
        self.var_ip_name.set("")
        self.var_ip_addr.set("")
        self._render_ip_table()
        self._save_config()
        self._check_ips([item])

    def _del_ip_items(self):
        sel = set(self.tv_ip.selection())
        if not sel:
            return
        self.ip_items = [it for it in self.ip_items if f"ip{it['id']}" not in sel]
        self._render_ip_table()
        self._save_config()

    def _clear_ip_items(self):
        if not self.ip_items:
            return
        if messagebox.askyesno(APP_NAME, f"确定清空全部 {len(self.ip_items)} 个 IP 吗？"):
            self.ip_items = []
            self._render_ip_table()
            self._save_config()

    def _on_auto_check(self):
        self._save_config()
        if self.var_auto_check.get() and self.ip_items:
            self._check_ips(self.ip_items)

    def _render_ip_table(self):
        tv = self.tv_ip
        yv = tv.yview()
        tv.delete(*tv.get_children())
        for i, it in enumerate(self.ip_items):
            kind, state = self._ip_state(it)
            tv.insert("", "end", iid=f"ip{it['id']}", tags=(f"{kind}{i % 2}",),
                      values=self._ip_values(it, state))
        self._update_ip_stat()
        try:
            tv.yview_moveto(yv[0])
        except Exception:
            pass

    def _web_text(self, it):
        lvl = it.get("web")
        if not lvl or not it.get("web_ts"):
            return "○ 待检测"
        if lvl == "ok":
            return f"● {it.get('web_label', '')} 可达"
        if lvl == "tcp":
            return "◐ 仅端口通"
        if lvl == "dns":
            return "○ 解析失败"
        return "○ 不可达"

    def _ip_values(self, it, state):
        return (it["name"] or it["ip"], it["ip"], state,
                f"{it['ms']:.1f} ms" if it["ok"] is True else "—",
                "", self._web_text(it), "",
                it["ts"] or "—", it["note"])

    def _update_ip_stat(self):
        online = sum(1 for it in self.ip_items if it["ok"] is True)
        offline = sum(1 for it in self.ip_items if it["ok"] is False)
        web_ok = sum(1 for it in self.ip_items
                     if it.get("web") == "ok" and it.get("web_ts"))
        self.lbl_ip_stat.configure(
            text=f"在线 {online}   ·   离线 {offline}   ·   网页可达 {web_ok}   ·   共 {len(self.ip_items)}")

    @staticmethod
    def _ip_state(it):
        if it["ok"] is True:
            if it.get("web_ts") and it.get("web") in ("tcp", "none", "dns"):
                return "warn", "● 在线"
            return "ok", "● 在线"
        if it["ok"] is False:
            return "bad", "● 离线"
        return "wait", "○ 检测中"

    def _check_ips(self, items, force_web=False):
        if not items:
            return
        targets = [(it["id"], it["ip"]) for it in items]
        now = time.time()
        web_targets = [(it["id"], it["ip"]) for it in items
                       if force_web or now - it.get("web_ts", 0) > 55]

        def one_ping(iid, ip):
            try:
                ok, ms, err = netinfo.ping_host(ip, 1200)
            except Exception as exc:
                ok, ms, err = False, 0.0, str(exc)
            self.ip_q.put(("ping", iid, ok, ms, err))

        def one_web(iid, ip):
            try:
                lvl, label, detail = netinfo.web_check(ip, 443, 2.5)
            except Exception as exc:
                lvl, label, detail = "none", "检测异常", str(exc)
            self.ip_q.put(("web", iid, lvl, label, detail))

        def worker():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=16) as ex:
                for iid, ip in targets:
                    ex.submit(one_ping, iid, ip)
                for iid, ip in web_targets:
                    ex.submit(one_web, iid, ip)

        threading.Thread(target=worker, daemon=True).start()

    def _drain_ip(self):
        changed = False
        try:
            while True:
                msg = self.ip_q.get_nowait()
                kind, iid = msg[0], msg[1]
                it = next((x for x in self.ip_items if x["id"] == iid), None)
                if it is None:
                    continue
                if kind == "ping":
                    _, _, ok, ms, err = msg
                    it["ok"], it["ms"], it["err"] = ok, ms, err
                    it["ts"] = time.strftime("%H:%M:%S")
                else:
                    _, _, lvl, label, detail = msg
                    it["web"], it["web_label"] = lvl, label
                    it["web_detail"] = detail
                    it["web_ts"] = time.time()
                self._refresh_ip_row(it)
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._update_ip_stat()

    def _refresh_ip_row(self, it):
        tv = self.tv_ip
        iid = f"ip{it['id']}"
        if not tv.exists(iid):
            return
        idx = tv.index(iid)
        kind, state = self._ip_state(it)
        tv.item(iid, tags=(f"{kind}{idx % 2}",), values=self._ip_values(it, state))

    def _auto_check_loop(self):
        try:
            if self.var_auto_check.get() and self.ip_items:
                self._check_ips(self.ip_items)
        except Exception:
            pass
        self.root.after(10000, self._auto_check_loop)

    def _merge_items(self, parsed):
        added = updated = 0
        index = {it["ip"]: it for it in self.ip_items}
        for name, ip, note in parsed:
            if not netinfo.is_ip(ip):
                continue
            if ip in index:
                it = index[ip]
                if name:
                    it["name"] = name
                if note:
                    it["note"] = note
                updated += 1
                continue
            self._ip_seq += 1
            it = {"id": self._ip_seq, "name": name or ip, "ip": ip, "note": note,
                  "ok": None, "ms": 0.0, "err": "", "ts": "",
                  "web": None, "web_label": "", "web_detail": "", "web_ts": 0.0}
            self.ip_items.append(it)
            index[ip] = it
            added += 1
        self._render_ip_table()
        self._save_config()
        return added, updated

    def _bulk_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("批量导入 IP")
        dlg.configure(bg=CARD)
        dlg.transient(self.root)
        w, h = self.px(580), self.px(460)
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + max(30, (self.root.winfo_height() - h) // 3)
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        try:
            ico = resource_path("icon.ico")
            if os.path.exists(ico):
                dlg.iconbitmap(ico)
        except Exception:
            pass

        tk.Label(dlg, text="批量导入 IP 地址", bg=CARD, fg=TEXT,
                 font=self.fp(13, True)).pack(anchor="w", padx=self.px(18), pady=(self.px(14), self.px(4)))
        tk.Label(dlg, text="每行一条。支持「设备名,IP」「设备名 IP」「IP」；逗号 / 分号 / Tab / 空格均可分隔，\n"
                           "第三个字段作为备注。重复 IP 只更新设备名，不会重复添加。",
                 bg=CARD, fg=MUTED, font=self.fp(11), justify="left").pack(anchor="w", padx=self.px(18))

        txt = tk.Text(dlg, height=12, font=self.fp(12), bg="#FBFCFE", fg=TEXT, relief="flat",
                      highlightthickness=1, highlightbackground=LINE, highlightcolor=PRIMARY,
                      wrap="none", padx=self.px(8), pady=self.px(6))
        txt.pack(fill="both", expand=True, padx=self.px(18), pady=(self.px(10), 0))
        txt.focus_set()

        btns = tk.Frame(dlg, bg=CARD)
        btns.pack(fill="x", padx=self.px(18), pady=self.px(12))
        msg = tk.Label(btns, text="", bg=CARD, fg=MUTED, font=self.fp(11))
        msg.pack(side="left")

        def do_import():
            parsed = parse_bulk_text(txt.get("1.0", "end"))
            if not parsed:
                msg.configure(text="没有解析到有效 IP", fg=ERR)
                return
            added, updated = self._merge_items(parsed)
            dlg.destroy()
            self._toast(f"导入完成：新增 {added} 条，更新 {updated} 条", "ok")
            self._check_ips([it for it in self.ip_items
                             if it["ok"] is None or not it.get("web_ts")])

        def load_file():
            path = filedialog.askopenfilename(
                parent=dlg, title="选择 IP 列表文件",
                filetypes=[("文本/CSV 文件", "*.txt *.csv *.list"), ("所有文件", "*.*")])
            if not path:
                return
            body = ""
            for enc in ("utf-8-sig", "gbk", "utf-8"):
                try:
                    with open(path, "r", encoding=enc) as f:
                        body = f.read()
                    break
                except Exception:
                    continue
            if not body:
                msg.configure(text="文件读取失败", fg=ERR)
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", body)
            msg.configure(text=f"已载入 {os.path.basename(path)}，确认后点“导入”", fg=MUTED)

        ttk.Button(btns, text="导入", style="Primary.TButton", command=do_import).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, self.px(8)))
        ttk.Button(btns, text="从文件读取…", command=load_file).pack(side="right", padx=(0, self.px(8)))
        dlg.grab_set()

    def _on_ip_double(self, event):
        iid = self.tv_ip.identify_row(event.y)
        if not iid:
            return
        for it in self.ip_items:
            if f"ip{it['id']}" == iid:
                self._edit_ip_item(it)
                return

    def _ip_menu(self, event):
        iid = self.tv_ip.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tv_ip.selection():
            self.tv_ip.selection_set(iid)
        target = None
        for it in self.ip_items:
            if f"ip{it['id']}" == iid:
                target = it
                break
        menu = tk.Menu(self.root, tearoff=0, font=self.fp(11))
        menu.add_command(label="立即检测", command=lambda: self._check_ips([target] if target else [], force_web=True))
        menu.add_command(label="编辑设备名 / 备注",
                         command=lambda: self._edit_ip_item(target) if target else None)
        menu.add_command(label="复制 IP", command=lambda: self._copy(target["ip"]) if target else None)
        menu.add_separator()
        menu.add_command(label="删除", command=self._del_ip_items)
        menu.tk_popup(event.x_root, event.y_root)

    def _edit_ip_item(self, item):
        if not item:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("编辑设备信息")
        dlg.configure(bg=CARD)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = self.px(420), self.px(240)
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        tk.Label(dlg, text="编辑设备信息", bg=CARD, fg=TEXT,
                 font=self.fp(13, True)).pack(anchor="w", padx=self.px(18), pady=(self.px(14), self.px(8)))
        body = tk.Frame(dlg, bg=CARD)
        body.pack(fill="x", padx=self.px(18))

        tk.Label(body, text="IP 地址", bg=CARD, fg=MUTED, font=self.fp(11)).grid(
            row=0, column=0, sticky="w", pady=self.px(4))
        tk.Label(body, text=item["ip"], bg=CARD, fg=TEXT, font=self.fp(12, True)).grid(
            row=0, column=1, sticky="w", padx=self.px(10), pady=self.px(4))

        tk.Label(body, text="设备名", bg=CARD, fg=MUTED, font=self.fp(11)).grid(
            row=1, column=0, sticky="w", pady=self.px(4))
        var_name = tk.StringVar(value=item["name"])
        ttk.Entry(body, textvariable=var_name, width=28).grid(
            row=1, column=1, sticky="w", padx=self.px(10), pady=self.px(4))

        tk.Label(body, text="备注", bg=CARD, fg=MUTED, font=self.fp(11)).grid(
            row=2, column=0, sticky="w", pady=self.px(4))
        var_note = tk.StringVar(value=item["note"])
        ttk.Entry(body, textvariable=var_note, width=28).grid(
            row=2, column=1, sticky="w", padx=self.px(10), pady=self.px(4))

        btns = tk.Frame(dlg, bg=CARD)
        btns.pack(fill="x", padx=self.px(18), pady=self.px(16))

        def save():
            item["name"] = var_name.get().strip() or item["ip"]
            item["note"] = var_note.get().strip()
            self._render_ip_table()
            self._save_config()
            dlg.destroy()

        ttk.Button(btns, text="保存", style="Primary.TButton", command=save).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, self.px(8)))
        dlg.grab_set()

    # --- Tab2 端口 ---
    def _build_tab_ports(self):
        tab = self.tab_port
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=(0, self.px(10)))

        tk.Label(bar, text="搜索", bg=BG, fg=MUTED, font=self.fp(11)).pack(side="left")
        self.var_search = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.var_search, width=34)
        ent.pack(side="left", padx=(self.px(6), self.px(14)))
        self.var_search.trace_add("write", lambda *a: self._invalidate_ports())

        self.var_listen = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(bar, text="仅显示监听端口", variable=self.var_listen, bg=BG, fg=TEXT,
                             font=self.fp(12), activebackground=BG, selectcolor=CARD,
                             highlightthickness=0, bd=0, command=self._invalidate_ports)
        chk.pack(side="left")

        self.var_tcp = tk.BooleanVar(value=True)
        self.var_udp = tk.BooleanVar(value=True)
        for var, label in ((self.var_tcp, "TCP"), (self.var_udp, "UDP")):
            tk.Checkbutton(bar, text=label, variable=var, bg=BG, fg=TEXT, font=self.fp(12),
                           activebackground=BG, selectcolor=CARD, highlightthickness=0, bd=0,
                           command=self._invalidate_ports).pack(side="left", padx=(self.px(10), 0))

        self.lbl_port_count = tk.Label(bar, text="", bg=BG, fg=MUTED, font=self.fp(11))
        self.lbl_port_count.pack(side="right")

        card = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="both", expand=True)

        cols = [("proto", "协议", 66, "w"), ("local", "本地地址", 245, "w"), ("port", "端口", 72, "center"),
                ("remote", "远端地址", 225, "w"), ("status", "状态", 118, "w"),
                ("pid", "PID", 72, "center"), ("proc", "进程", 200, "w")]
        self.tv_pt = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings")
        for key, title, width, anchor in cols:
            self.tv_pt.heading(key, text=title, anchor=anchor)
            self.tv_pt.column(key, width=self.px(width), anchor=anchor,
                              stretch=(key in ("local", "remote", "proc")))
        self.tv_pt.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        sb = ttk.Scrollbar(card, orient="vertical", command=self.tv_pt.yview,
                           style="Slim.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
        self.tv_pt.configure(yscrollcommand=sb.set)
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)

        for i, bg in ((0, CARD), (1, ROW_ALT)):
            self.tv_pt.tag_configure(f"l{i}", background=bg, foreground=OK)
            self.tv_pt.tag_configure(f"e{i}", background=bg, foreground=TEXT)
            self.tv_pt.tag_configure(f"o{i}", background=bg, foreground=MUTED)
        self.tv_pt.bind("<Button-3>", self._port_menu)

    # --- Tab3 探活 ---
    def _build_tab_probe(self):
        tab = self.tab_probe
        bar = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        bar.pack(fill="x", pady=(0, self.px(10)))
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=self.px(14), pady=self.px(10))

        tk.Label(inner, text="名称", bg=CARD, fg=MUTED, font=self.fp(11)).pack(side="left")
        self.var_p_name = tk.StringVar()
        ttk.Entry(inner, textvariable=self.var_p_name, width=14).pack(side="left", padx=(self.px(6), self.px(12)))
        tk.Label(inner, text="主机", bg=CARD, fg=MUTED, font=self.fp(11)).pack(side="left")
        self.var_p_host = tk.StringVar(value="127.0.0.1")
        ttk.Entry(inner, textvariable=self.var_p_host, width=20).pack(side="left", padx=(self.px(6), self.px(12)))
        tk.Label(inner, text="端口", bg=CARD, fg=MUTED, font=self.fp(11)).pack(side="left")
        self.var_p_port = tk.StringVar()
        e_port = ttk.Entry(inner, textvariable=self.var_p_port, width=8)
        e_port.pack(side="left", padx=(self.px(6), self.px(12)))
        e_port.bind("<Return>", lambda ev: self._add_probe())

        ttk.Button(inner, text="添加", style="Primary.TButton", command=self._add_probe).pack(side="left")
        ttk.Button(inner, text="全部检测", command=self._probe_all).pack(side="left", padx=(self.px(8), 0))
        ttk.Button(inner, text="删除选中", command=self._del_probe).pack(side="left", padx=(self.px(8), 0))

        card = tk.Frame(tab, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="both", expand=True)
        cols = [("name", "名称", 180, "w"), ("target", "目标地址", 300, "w"),
                ("result", "连通性", 110, "w"), ("lat", "延迟", 110, "w"),
                ("ts", "最近检测", 180, "w")]
        self.tv_pb = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings")
        for key, title, width, anchor in cols:
            self.tv_pb.heading(key, text=title, anchor=anchor)
            self.tv_pb.column(key, width=self.px(width), anchor=anchor, stretch=(key == "target"))
        self.tv_pb.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        sb = ttk.Scrollbar(card, orient="vertical", command=self.tv_pb.yview,
                           style="Slim.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns", pady=1, padx=(0, 1))
        self.tv_pb.configure(yscrollcommand=sb.set)
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)
        for i, bg in ((0, CARD), (1, ROW_ALT)):
            self.tv_pb.tag_configure(f"ok{i}", background=bg, foreground=OK)
            self.tv_pb.tag_configure(f"bad{i}", background=bg, foreground=ERR)
            self.tv_pb.tag_configure(f"wait{i}", background=bg, foreground=MUTED)

        tk.Label(tab, text="每 5 秒自动检测一次已添加的目标；探活使用 TCP 连接，超时 2 秒。",
                 bg=BG, fg=MUTED, font=self.fp(11), anchor="w").pack(fill="x", pady=(self.px(8), 0))

    # ---------- 数据流 ----------
    def push_snapshot(self, snap):
        try:
            while self.q.qsize() > 1:
                self.q.get_nowait()
        except Exception:
            pass
        self.q.put(snap)

    def push_error(self, msg):
        try:
            while self.q.qsize() > 1:
                self.q.get_nowait()
        except Exception:
            pass
        self.q.put({"error": msg})

    def _tick(self):
        try:
            while True:
                snap = self.q.get_nowait()
                if "error" in snap:
                    self._report_error(snap["error"])
                else:
                    self._apply(snap)
        except queue.Empty:
            pass
        self._drain_probe()
        self._drain_ip()
        self.root.after(130, self._tick)

    def _report_error(self, msg):
        if not self._error_shown:
            self._error_shown = True
            self.lbl_stat_right.configure(text=f"采集异常：{msg}")

    def _apply(self, snap):
        self.snap = snap
        self.adapters = snap["adapters"]
        self.ports = snap["ports"]
        self.rates = snap["rates"]
        self.last_ts = snap["ts"]
        self._error_shown = False
        self._render_adapters()
        self._render_ports()
        self._update_status()
        self._update_switch_ui()

    def _update_status(self):
        active_ips = sum(1 for a in self.adapters if a["up"] for i in a["ips"] if not i["ip"].startswith("169.254"))
        listening = sum(1 for p in self.ports if p["status"] == "LISTEN")
        conns = sum(1 for p in self.ports if p["status"] == "ESTABLISHED")
        up_count = sum(1 for a in self.adapters if a["up"])
        ts = time.strftime("%H:%M:%S", time.localtime(self.last_ts)) if self.last_ts else "—"
        self.lbl_stat_left.configure(
            text=f"适配器 {len(self.adapters)}（已连接 {up_count}）   ·   活跃 IP {active_ips}   ·   "
                 f"监听端口 {listening}   ·   已建立连接 {conns}")
        extra = "已暂停" if self.paused else f"更新于 {ts}"
        if time.time() >= getattr(self, "_toast_until", 0):
            self.lbl_stat_right.configure(text=f"{extra}   ·   刷新间隔 {int(self.interval)} 秒", fg=MUTED)

    # ---------- 内外网快速切换 ----------
    def _switch_adapters(self):
        """按配置的名称/IP 定位内网、外网网卡；找不到时按特征回退。"""
        lan = wan = None
        lan_ip = str(self.switch_cfg.get("lan_ip", "")).strip()
        lan_name = str(self.switch_cfg.get("lan_name", "")).strip()
        wan_name = str(self.switch_cfg.get("wan_name", "")).strip()

        def v4(ad):
            return [i["ip"] for i in ad["ips"] if i["family"] == "IPv4"]

        for ad in self.adapters:
            name = ad["name"]
            if lan_name and name == lan_name:
                lan = ad
            elif lan_ip and lan is None and lan_ip in v4(ad):
                lan = ad
            elif lan_ip and lan is None and lan_ip.rsplit(".", 1)[0] and \
                    any(ip.startswith(lan_ip.rsplit(".", 1)[0] + ".") for ip in v4(ad)):
                lan = ad
            if wan_name and name == wan_name:
                wan = ad
        if lan is None:
            for ad in self.adapters:
                if any(ip.startswith("10.") for ip in v4(ad)) and ad["up"]:
                    lan = ad
                    break
        if wan is None:
            for ad in self.adapters:
                if ad is not lan and ad["up"] and ad["gateways"] and \
                        (ad["if_type"] == 71 or any(k in ad["name"] for k in ("WLAN", "Wi-Fi", "无线"))):
                    wan = ad
                    break
        return lan, wan

    @staticmethod
    def _mode_of(lan, wan):
        if not lan or not wan:
            return "unknown"
        if lan["metric_v4"] < wan["metric_v4"]:
            return "lan"
        if wan["metric_v4"] < lan["metric_v4"]:
            return "wan"
        return "auto"

    def _update_switch_ui(self, mode=None):
        if self._switching or not hasattr(self, "btn_switch"):
            return
        lan, wan = self._switch_adapters()
        self.switch_mode = mode or self._mode_of(lan, wan)

        labels = {
            "lan": ("● 当前：内网优先", PRIMARY),
            "wan": ("● 当前：外网优先", PRIMARY),
            "auto": ("○ 当前：自动跃点", MUTED),
            "unknown": ("○ 未检测到内外网双网卡", WARN),
        }
        text, color = labels.get(self.switch_mode, labels["unknown"])
        self.lbl_mode.configure(text=text, fg=color)
        target = "外网优先" if self.switch_mode == "lan" else "内网优先"
        self.btn_switch.configure(
            text="切换到" + target if self.switch_mode != "unknown" else "无法切换",
            state="normal" if self.switch_mode != "unknown" else "disabled")

        def describe(ad, role):
            ips = [i["ip"] for i in ad["ips"] if i["family"] == "IPv4"]
            return f"{role} {ad['name']} · {ips[0] if ips else '—'} · 跃点 {ad['metric_v4']}"

        parts = []
        if lan:
            parts.append(describe(lan, "内网"))
        if wan:
            parts.append(describe(wan, "外网"))
        self.lbl_switch_info.configure(text="      ".join(parts) if parts else "未识别到配置中的网卡")

    def _toggle_mode(self):
        if self._switching:
            return
        if not self.admin:
            if messagebox.askyesno(APP_NAME, "切换网卡优先级需要管理员权限，是否以管理员身份重新启动？"):
                self._elevate()
            return
        lan, wan = self._switch_adapters()
        if not lan or not wan:
            messagebox.showwarning(APP_NAME, "未同时找到内网与外网网卡，无法切换。\n"
                                            "可在程序目录 config.json 的 switch 段修改网卡名称/IP。")
            return
        target = "wan" if self.switch_mode == "lan" else "lan"
        try:
            low = int(self.switch_cfg.get("low", 10))
            high = int(self.switch_cfg.get("high", 100))
        except Exception:
            low, high = 10, 100
        jobs = [(lan["name"], low if target == "lan" else high),
                (wan["name"], low if target == "wan" else high)]
        label = "内网" if target == "lan" else "外网"

        self._switching = True
        self.btn_switch.configure(text="正在切换…", state="disabled")
        self._toast(f"正在切换到{label}优先…", "info")

        def worker():
            results = [(name, val) + netinfo.set_metric(name, 4, val) for name, val in jobs]
            self.root.after(0, lambda: self._switch_done(target, results))

        threading.Thread(target=worker, daemon=True).start()

    def _switch_done(self, target, results):
        self._switching = False
        bad = [r for r in results if not r[2]]
        label = "内网" if target == "lan" else "外网"
        if bad:
            detail = "\n".join(f"{r[0]} → 跃点 {r[1]}：{r[3]}" for r in bad)
            self._toast(f"切换失败：{detail}", "err")
            messagebox.showerror(APP_NAME, f"切换到{label}优先失败：\n\n{detail}")
        else:
            self._toast(f"已切换到{label}优先", "ok")
        self._ad_sig = None
        self._metric_dirty = False
        self._reload_metric_fields(force=True)
        self._update_switch_ui(mode=target if not bad else None)
        self._force_refresh()

    # ---------- 渲染：网卡 ----------
    @staticmethod
    def _ad_key(ad):
        name = ad["name"]
        is_loop = "Loopback" in name or name.lower().startswith("loopback")
        return (is_loop, 0 if ad["up"] else 1, ad["metric_v4"], name)

    def _render_adapters(self):
        sig = tuple((a["guid"], a["up"], a["metric_v4"], a["metric_v6"],
                     tuple(i["ip"] for i in a["ips"])) for a in self.adapters)
        if sig != self._ad_sig:
            self._ad_sig = sig

            tv = self.tv_ad
            self._busy = True
            sel = self.select_guid
            yv = tv.yview()
            tv.delete(*tv.get_children())

            meta = {}
            idx = 0
            for ad in sorted(self.adapters, key=self._ad_key):
                ips = ad["ips"] or [None]
                rate = self.rates.get(ad["name"], (0.0, 0.0))
                loop = "Loopback" in ad["name"]
                for i, info in enumerate(ips):
                    first = i == 0
                    if ad["up"]:
                        state = "● 已连接"
                    else:
                        state = "○ 未连接"
                    fam = info["family"] if info else ""
                    ip_txt = info["ip"] if info else "—"
                    prefix = f"/{info['prefix']}" if info else ""
                    gw = "—"
                    if ad["gateways"]:
                        same_fam = [g for g in ad["gateways"] if (":" in g) == (fam == "IPv6")]
                        if same_fam:
                            gw = same_fam[0]
                    metric = 0
                    if info:
                        metric = ad["metric_v4"] if fam == "IPv4" else ad["metric_v6"]
                    iid = f"{ad['guid']}#{idx}"
                    meta[iid] = (ad["name"], first, loop)
                    idx += 1
                    kind = "loop" if loop else ("on" if ad["up"] else "off")
                    tv.insert("", "end", iid=iid, tags=(f"{kind}{idx % 2}",), values=(
                        ad["name"] if first else "",
                        state if first else "",
                        fam,
                        ip_txt,
                        prefix,
                        gw if first else "",
                        metric if metric else "—",
                        fmt_rate(rate[0]) if first and not loop else "",
                        fmt_rate(rate[1]) if first and not loop else "",
                    ))
            self._row_meta = meta
            self._busy = False
            if sel:
                for child in tv.get_children():
                    if child.split("#")[0] == sel:
                        tv.selection_set(child)
                        tv.see(child)
                        break
            try:
                tv.yview_moveto(yv[0])
            except Exception:
                pass
        self._update_rates()

    def _update_rates(self):
        """只刷新下行/上行两列，避免整表重建覆盖用户操作。"""
        meta = getattr(self, "_row_meta", {})
        if not meta:
            return
        tv = self.tv_ad
        for iid, (name, first, loop) in meta.items():
            if not first or loop or not tv.exists(iid):
                continue
            rx, tx = self.rates.get(name, (0.0, 0.0))
            tv.set(iid, "down", fmt_rate(rx))
            tv.set(iid, "up", fmt_rate(tx))

    def _on_ad_select(self, _evt=None):
        if self._busy:
            return
        sel = self.tv_ad.selection()
        if not sel:
            self.select_guid = None
            self._set_metric_enabled(False)
            self.lbl_sel.configure(text="未选择适配器（在上方列表中单击一行）", fg=MUTED)
            return
        guid = sel[0].split("#")[0]
        if guid == self.select_guid:
            return  # 同一适配器的例行刷新，保留用户正在输入的内容
        self.select_guid = guid
        self._metric_dirty = False
        self._reload_metric_fields(force=True)

    def _current_adapter(self):
        if not self.select_guid:
            return None
        for a in self.adapters:
            if a["guid"] == self.select_guid:
                return a
        return None

    def _mark_metric_dirty(self, _evt=None):
        self._metric_dirty = True

    def _reload_metric_fields(self, force=False):
        ad = self._current_adapter()
        if not ad:
            self._set_metric_enabled(False)
            return
        self._set_metric_enabled(True)
        if self._metric_dirty and not force:
            return  # 用户正在输入，跳过覆盖
        self.var_m4.set(str(ad["metric_v4"]) if ad["metric_v4"] else "")
        self.var_m6.set(str(ad["metric_v6"]) if ad["metric_v6"] else "")
        kind = "无线" if ad["if_type"] == 71 else ("以太网" if ad["if_type"] == 6 else "虚拟/其他")
        self.lbl_sel.configure(
            text=f"{ad['name']}   ·   {kind}   ·   {fmt_link(ad['rx_speed'])}   ·   MTU {ad['mtu']}",
            fg=TEXT)

    def _reset_metric_input(self):
        self._metric_dirty = False
        self._reload_metric_fields()

    def _set_metric_enabled(self, on):
        state = "normal" if on else "disabled"
        for w in (self.ent_m4, self.ent_m6, self.btn_apply, self.btn_auto, self.btn_reset):
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _apply_metric(self, auto=False):
        ad = self._current_adapter()
        if not ad:
            messagebox.showinfo(APP_NAME, "请先在列表中选择一块网卡。")
            return
        if not self.admin:
            if messagebox.askyesno(APP_NAME, "修改跃点数需要管理员权限，是否以管理员身份重新启动？"):
                self._elevate()
            return

        jobs = []
        if auto:
            jobs = [(4, None), (6, None)]
        else:
            raw4, raw6 = self.var_m4.get().strip(), self.var_m6.get().strip()
            for fam, raw, cur in ((4, raw4, ad["metric_v4"]), (6, raw6, ad["metric_v6"])):
                if raw == "":
                    continue
                try:
                    val = int(raw)
                except ValueError:
                    messagebox.showwarning(APP_NAME, f"IPv{fam} 跃点数必须是 1–9999 的整数。")
                    return
                if not 1 <= val <= 9999:
                    messagebox.showwarning(APP_NAME, f"IPv{fam} 跃点数必须在 1–9999 之间。")
                    return
                if val != cur:
                    jobs.append((fam, val))
            if not jobs:
                self._toast("输入值与当前跃点数相同，无需修改。", "info")
                return

        name = ad["name"]
        self._toast("正在应用…", "info")
        self.btn_apply.configure(state="disabled")

        def worker():
            results = []
            for fam, val in jobs:
                ok, msg = netinfo.set_metric(name, fam, val)
                results.append((fam, val, ok, msg))
            self.root.after(0, lambda: self._metric_done(name, results))

        threading.Thread(target=worker, daemon=True).start()

    def _metric_done(self, name, results):
        self.btn_apply.configure(state="normal")
        bad = [r for r in results if not r[2]]
        if bad:
            detail = "\n".join(f"IPv{r[0]} → {r[3]}" for r in bad)
            self._toast(f"修改失败：{detail}", "err")
            messagebox.showerror(APP_NAME, f"设置 {name} 的跃点数失败：\n\n{detail}\n\n提示：请确认程序以管理员身份运行。")
        else:
            parts = []
            for f, v, _ok, _msg in results:
                parts.append(f"IPv{f}=" + ("自动" if v is None else str(v)))
            self._toast(f"{name} 已更新：" + ", ".join(parts), "ok")
        self._ad_sig = None
        self._metric_dirty = False
        self._reload_metric_fields(force=True)
        self._force_refresh()

    def _toast(self, msg, level="info"):
        color = {"ok": OK, "err": ERR, "info": MUTED}.get(level, MUTED)
        self._toast_until = time.time() + 4
        self.lbl_stat_right.configure(text=msg, fg=color)
        self.root.after(4000, self._clear_toast)

    def _clear_toast(self):
        self._toast_until = 0
        self._update_status()
        self.lbl_stat_right.configure(fg=MUTED)

    # ---------- 渲染：端口 ----------
    def _invalidate_ports(self):
        self._port_sig = None
        self._render_ports(force=True)

    def _filtered_ports(self):
        q = self.var_search.get().strip().lower()
        only_listen = self.var_listen.get()
        use_tcp, use_udp = self.var_tcp.get(), self.var_udp.get()
        out = []
        for r in self.ports:
            is_tcp = r["proto"].startswith("TCP")
            if is_tcp and not use_tcp:
                continue
            if not is_tcp and not use_udp:
                continue
            if only_listen and r["status"] != "LISTEN":
                continue
            if q:
                hay = f'{r["local"]} {r["port"]} {r["pid"]} {r["proc"]} {r["remote"]} {r["proto"]}'.lower()
                if q not in hay:
                    continue
            out.append(r)
        out.sort(key=lambda r: (r["port"], r["proto"], r["local"]))
        return out

    def _render_ports(self, force=False):
        rows = self._filtered_ports()
        sig = tuple((r["proto"], r["local"], r["remote"], r["status"], r["pid"]) for r in rows)
        self.lbl_port_count.configure(text=f"共 {len(rows)} 条（全部 {len(self.ports)}）")
        if sig == self._port_sig and not force:
            return
        self._port_sig = sig

        tv = self.tv_pt
        yv = tv.yview()
        tv.delete(*tv.get_children())
        for i, r in enumerate(rows):
            if r["status"] == "LISTEN":
                kind = "l"
            elif r["status"] == "ESTABLISHED":
                kind = "e"
            else:
                kind = "o"
            tv.insert("", "end", tags=(f"{kind}{i % 2}",), values=(
                r["proto"], r["local"], r["port"], r["remote"], r["status"], r["pid"] or "—", r["proc"]))
        try:
            tv.yview_moveto(yv[0])
        except Exception:
            pass

    def _port_menu(self, event):
        iid = self.tv_pt.identify_row(event.y)
        if not iid:
            return
        self.tv_pt.selection_set(iid)
        vals = self.tv_pt.item(iid, "values")
        menu = tk.Menu(self.root, tearoff=0, font=self.fp(11))
        menu.add_command(label="复制本地地址", command=lambda: self._copy(vals[1]))
        menu.add_command(label="复制端口号", command=lambda: self._copy(vals[2]))
        menu.add_command(label="复制进程名", command=lambda: self._copy(vals[6]))
        menu.add_separator()
        menu.add_command(label="复制整行", command=lambda: self._copy("  ".join(str(v) for v in vals)))
        menu.tk_popup(event.x_root, event.y_root)

    def _ad_menu(self, event):
        iid = self.tv_ad.identify_row(event.y)
        if not iid:
            return
        self.tv_ad.selection_set(iid)
        self._on_ad_select()
        vals = self.tv_ad.item(iid, "values")
        menu = tk.Menu(self.root, tearoff=0, font=self.fp(11))
        menu.add_command(label="复制 IP", command=lambda: self._copy(vals[3]))
        menu.add_command(label="复制网卡名称", command=lambda: self._copy(vals[0]))
        menu.add_separator()
        menu.add_command(label="复制整行", command=lambda: self._copy("  ".join(str(v) for v in vals)))
        menu.tk_popup(event.x_root, event.y_root)

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(str(text))
        self._toast("已复制到剪贴板", "ok")

    # ---------- 探活 ----------
    def _add_probe(self):
        host = self.var_p_host.get().strip() or "127.0.0.1"
        raw_port = self.var_p_port.get().strip()
        try:
            port = int(raw_port)
            assert 1 <= port <= 65535
        except Exception:
            messagebox.showwarning(APP_NAME, "端口号必须是 1–65535 的整数。")
            return
        name = self.var_p_name.get().strip() or f"{host}:{port}"
        for p in self.probes:
            if p["host"] == host and p["port"] == port:
                self._toast("该目标已在列表中", "info")
                return
        self._probe_seq += 1
        item = {"id": self._probe_seq, "name": name, "host": host, "port": port,
                "ok": None, "ms": 0.0, "err": "", "ts": ""}
        self.probes.append(item)
        self.var_p_name.set("")
        self.var_p_port.set("")
        self._refresh_probe_table()
        self._save_config()
        self._probe_one(item)

    def _del_probe(self):
        sel = self.tv_pb.selection()
        if not sel:
            return
        self.probes = [p for p in self.probes if f"p{p['id']}" not in sel]
        self._refresh_probe_table()
        self._save_config()

    def _refresh_probe_table(self):
        tv = self.tv_pb
        tv.delete(*tv.get_children())
        for i, p in enumerate(self.probes):
            if p["ok"] is True:
                kind, res = "ok", f"● 通  {p['ms']:.0f} ms"
                lat = f"{p['ms']:.1f} ms"
            elif p["ok"] is False:
                kind, res = "bad", "● 不通"
                lat = "—"
            else:
                kind, res = "wait", "○ 检测中"
                lat = "—"
            tv.insert("", "end", iid=f"p{p['id']}", tags=(f"{kind}{i % 2}",), values=(
                p["name"], f"{p['host']}:{p['port']}", res, lat, p["ts"] or "—"))

    def _probe_all(self):
        for p in self.probes:
            self._probe_one(p)

    def _probe_one(self, item):
        if item["id"] in self._probing:
            return
        self._probing.add(item["id"])

        def work():
            t0 = time.perf_counter()
            ok, err = False, ""
            try:
                with socket.create_connection((item["host"], item["port"]), timeout=2.0):
                    ok = True
            except Exception as e:
                err = str(e)
            ms = (time.perf_counter() - t0) * 1000
            self.probe_q.put((item["id"], ok, ms, err))

        threading.Thread(target=work, daemon=True).start()

    def _drain_probe(self):
        try:
            while True:
                pid, ok, ms, err = self.probe_q.get_nowait()
                self._probing.discard(pid)
                for p in self.probes:
                    if p["id"] == pid:
                        p["ok"], p["ms"], p["err"] = ok, ms, err
                        p["ts"] = time.strftime("%H:%M:%S")
                        iid = f"p{pid}"
                        if self.tv_pb.exists(iid):
                            idx = self.tv_pb.index(iid)
                            kind = "ok" if ok else "bad"
                            self.tv_pb.item(iid, tags=(f"{kind}{idx % 2}",), values=(
                                p["name"], f"{p['host']}:{p['port']}",
                                f"● 通  {ms:.0f} ms" if ok else "● 不通",
                                f"{ms:.1f} ms" if ok else "—", p["ts"]))
                        break
        except queue.Empty:
            pass

    def _auto_probe(self):
        if not self.paused and self.probes:
            self._probe_all()
        self.root.after(5000, self._auto_probe)

    # ---------- 交互 ----------
    def _on_interval(self, _evt=None):
        txt = self.var_interval.get()
        try:
            self.interval = float(txt.split()[0])
        except Exception:
            self.interval = 2.0
        self._update_status()
        self._save_config()

    def _toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.configure(text="继续" if self.paused else "暂停")
        self._update_status()

    def _force_refresh(self):
        self._ad_sig = None
        self._port_sig = None
        self.collector.stop_evt.set()
        self.collector = Collector(self)
        self.collector.start()

    def _elevate(self):
        try:
            if getattr(sys, "frozen", False):
                exe, params = sys.executable, ""
            else:
                exe = sys.executable
                params = f'"{os.path.abspath(__file__)}"'
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params,
                                                      os.path.dirname(exe), 1)
            if ret > 32:
                self.root.after(300, self.root.destroy)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"提权失败：{e}")

    # ---------- 配置 ----------
    def _load_config(self):
        self._cfg = {}
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                self._cfg = json.load(f)
        except Exception:
            pass
        self.interval = float(self._cfg.get("interval", 2))
        self.switch_cfg = {"lan_ip": "10.254.217.87", "lan_name": "以太网 8",
                           "wan_name": "WLAN", "low": 10, "high": 100}
        user_switch = self._cfg.get("switch", {})
        if isinstance(user_switch, dict):
            self.switch_cfg.update(user_switch)
        probes = self._cfg.get("probes", [])
        for i, p in enumerate(probes):
            self._probe_seq = max(self._probe_seq, i + 1)
            self.probes.append({"id": i + 1, "name": p.get("name", ""), "host": p.get("host", ""),
                                "port": int(p.get("port", 0)), "ok": None, "ms": 0.0,
                                "err": "", "ts": ""})

        self.auto_check = bool(self._cfg.get("ip_auto_check", True))
        for i, it in enumerate(self._cfg.get("ip_list", [])):
            self._ip_seq = max(self._ip_seq, i + 1)
            self.ip_items.append({"id": i + 1, "name": it.get("name", ""),
                                  "ip": it.get("ip", ""), "note": it.get("note", ""),
                                  "ok": None, "ms": 0.0, "err": "", "ts": "",
                                  "web": None, "web_label": "", "web_detail": "", "web_ts": 0.0})

    def _save_config(self):
        cfg = {"interval": self.interval,
               "switch": self.switch_cfg,
               "ip_auto_check": bool(self.var_auto_check.get()) if hasattr(self, "var_auto_check") else self.auto_check,
               "ip_list": [{"name": it["name"], "ip": it["ip"], "note": it["note"]}
                           for it in self.ip_items],
               "probes": [{"name": p["name"], "host": p["host"], "port": p["port"]} for p in self.probes]}
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self):
        self._save_config()
        try:
            self.collector.stop_evt.set()
        except Exception:
            pass
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
