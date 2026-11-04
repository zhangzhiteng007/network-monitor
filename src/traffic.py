# -*- coding: utf-8 -*-
"""网卡流量历史存储。

- 采样点：后台线程每次采集记录一张网卡相对上次的字节增量与瞬时速率。
- 细粒度：最近 15 分钟保留原始采样（2 秒左右一个点），用于短时段曲线。
- 归档：更早的采样滚动合并为「分钟桶」，默认保留 24 小时，避免长时间运行内存膨胀。
- 查询：按时段 + 网卡（或全部合计）返回曲线点、总量、峰值、平均速率。
- 持久化：分钟桶与累计计数器写入 JSON，重开程序后仍可查看历史时段。
"""
import json
import os
import threading
import time

RAW_WINDOW = 15 * 60          # 原始采样保留时长（秒）
ARCHIVE_WINDOW = 24 * 3600    # 分钟桶保留时长（秒）
MAX_GAP = 120                 # 超过该间隔的采样视为休眠/暂停，不计入统计
ALL_NIC = "__all__"


def _bucket_key(ts):
    return int(ts // 60) * 60


class TrafficStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._prev = {}       # nic -> [rx_total, tx_total, ts]
        self._raw = {}        # nic -> [[ts, drx, dtx, dt, rx_rate, tx_rate], ...]
        self._buckets = {}    # nic -> [[minute, rx_sum, tx_sum, rx_peak, tx_peak, span, n], ...]
        self._dirty = False
        self._gen = 0
        self._last_save = 0.0

    # ---------- 采集 ----------
    def add(self, ts, io_totals):
        """io_totals: {nic: (bytes_recv, bytes_sent)}，累计计数器。"""
        with self._lock:
            for nic, pair in (io_totals or {}).items():
                try:
                    rx, tx = float(pair[0]), float(pair[1])
                except Exception:
                    continue
                prev = self._prev.get(nic)
                self._prev[nic] = [rx, tx, ts]
                if not prev:
                    continue
                prx, ptx, pts = prev
                dt = ts - pts
                if dt <= 0 or dt > MAX_GAP:
                    continue
                drx, dtx = rx - prx, tx - ptx
                if drx < 0 or dtx < 0:      # 网卡重插 / 计数器重置
                    drx, dtx = max(rx, 0.0), max(tx, 0.0)
                rr, tr = drx / dt, dtx / dt
                self._raw.setdefault(nic, []).append([ts, drx, dtx, dt, rr, tr])
                self._rollup(nic, ts)
            self._dirty = True
            self._gen += 1

    def _rollup(self, nic, now):
        raw = self._raw.get(nic)
        if not raw:
            return
        cutoff = now - RAW_WINDOW
        i = 0
        while i < len(raw) and raw[i][0] < cutoff:
            s = raw[i]
            b = self._get_bucket(nic, _bucket_key(s[0]))
            b[1] += s[1]
            b[2] += s[2]
            b[3] = max(b[3], s[4])
            b[4] = max(b[4], s[5])
            b[5] += s[3]
            b[6] += 1
            i += 1
        if i:
            del raw[:i]
        bl = self._buckets.get(nic)
        if bl:
            keep = now - ARCHIVE_WINDOW
            self._buckets[nic] = [b for b in bl if b[0] >= keep]

    def _get_bucket(self, nic, minute):
        bl = self._buckets.setdefault(nic, [])
        if bl and bl[-1][0] == minute:
            return bl[-1]
        if bl and bl[-1][0] > minute:        # 乱序（一般不会）
            for b in bl:
                if b[0] == minute:
                    return b
        b = [minute, 0.0, 0.0, 0.0, 0.0, 0.0, 0]
        bl.append(b)
        return b

    # ---------- 查询 ----------
    def nic_names(self):
        with self._lock:
            names = set(self._raw) | set(self._buckets) | set(self._prev)
            return sorted(n for n in names if n)

    def data_span(self):
        """返回 (最早时间, 最新时间)，无数据返回 (None, None)。"""
        with self._lock:
            ts = []
            for raw in self._raw.values():
                if raw:
                    ts.append(raw[0][0])
                    ts.append(raw[-1][0])
            for bl in self._buckets.values():
                if bl:
                    ts.append(bl[0][0])
                    ts.append(bl[-1][0] + 60)
            return (min(ts), max(ts)) if ts else (None, None)

    def sample_count(self, nic=None):
        with self._lock:
            names = self._targets(nic)
            return sum(len(self._raw.get(n, ())) + len(self._buckets.get(n, ())) for n in names)

    def _targets(self, nic):
        if nic and nic != ALL_NIC:
            return [nic] if (nic in self._raw or nic in self._buckets or nic in self._prev) else []
        return self.nic_names()

    def query(self, nic, start, end, max_points=360):
        """返回统计字典：points / total_rx / total_tx / peak_rx / peak_tx / avg_* / span / count。"""
        with self._lock:
            if end <= start:
                end = start + 1
            merged = {}          # ts(秒) -> [rx_rate, tx_rate]
            total_rx = total_tx = 0.0
            first_ts, last_ts = None, None

            for n in self._targets(nic):
                for s in self._raw.get(n, ()):
                    ts = s[0]
                    if start <= ts <= end:
                        key = int(ts)
                        row = merged.setdefault(key, [0.0, 0.0])
                        row[0] += s[4]
                        row[1] += s[5]
                        total_rx += s[1]
                        total_tx += s[2]
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                for b in self._buckets.get(n, ()):
                    minute = b[0]
                    if not (start <= minute <= end):
                        continue
                    span = max(b[5], 1.0)
                    key = minute + 30
                    row = merged.setdefault(key, [0.0, 0.0])
                    row[0] += b[1] / span
                    row[1] += b[2] / span
                    total_rx += b[1]
                    total_tx += b[2]
                    first_ts = minute if first_ts is None else min(first_ts, minute)
                    last_ts = minute + 60 if last_ts is None else max(last_ts, minute + 60)

            points = [[k, v[0], v[1]] for k, v in sorted(merged.items())]
            count = len(points)
            peak_rx = max((p[1] for p in points), default=0.0)
            peak_tx = max((p[2] for p in points), default=0.0)
            if len(points) > max_points:
                points = _downsample(points, max_points)
            span = max((last_ts - first_ts) if first_ts is not None else 0.0, 1.0)
            return {
                "points": points,
                "total_rx": total_rx,
                "total_tx": total_tx,
                "peak_rx": peak_rx,
                "peak_tx": peak_tx,
                "avg_rx": total_rx / span,
                "avg_tx": total_tx / span,
                "span": span,
                "count": count,
                "first_ts": first_ts,
                "last_ts": last_ts,
            }

    def clear(self):
        with self._lock:
            self._raw.clear()
            self._buckets.clear()
            self._dirty = True
            self._gen += 1

    # ---------- 持久化 ----------
    def load(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if int(data.get("version", 0)) != 1:
                return
            with self._lock:
                self._buckets = {k: [list(b) for b in v] for k, v in data.get("buckets", {}).items()}
                self._raw = {k: [list(s) for s in v] for k, v in data.get("raw", {}).items()}
                self._prev = {k: list(v) for k, v in data.get("prev", {}).items()}
                now = time.time()
                for bl in self._buckets.values():
                    bl[:] = [b for b in bl if b[0] >= now - ARCHIVE_WINDOW]
                for raw in self._raw.values():
                    raw[:] = [s for s in raw if s[0] >= now - RAW_WINDOW]
        except Exception:
            pass

    def save(self, path, force=False, min_interval=120):
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.time()
            if not force and now - self._last_save < min_interval:
                return
            payload = {"version": 1, "buckets": self._buckets,
                       "raw": self._raw, "prev": self._prev}
            gen = self._gen
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            with self._lock:
                self._last_save = now
                if self._gen == gen:      # 写盘期间没有新采样才清除脏标记
                    self._dirty = False
        except Exception:
            pass


def _downsample(points, max_points):
    """按时间等分合并，取每个区间内的平均速率，控制绘图点数。"""
    step = len(points) / float(max_points)
    out = []
    for i in range(max_points):
        a, b = int(i * step), int((i + 1) * step)
        chunk = points[a:b] or [points[-1]]
        n = len(chunk)
        out.append([sum(p[0] for p in chunk) / n,
                    sum(p[1] for p in chunk) / n,
                    sum(p[2] for p in chunk) / n])
    return out
