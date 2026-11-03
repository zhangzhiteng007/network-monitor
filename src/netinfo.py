"""核心网络信息采集：通过 IP Helper API + psutil 获取适配器/IP/端口信息。"""
import ctypes
import ctypes.wintypes as wt
import ipaddress
import os
import socket
import struct
import subprocess

# ---------------- Win32 结构定义 ----------------

AF_UNSPEC = 0
AF_INET = 2
AF_INET6 = 23
GAA_FLAG_SKIP_ANYCAST = 0x0002
GAA_FLAG_SKIP_MULTICAST = 0x0004
GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
GAA_FLAG_INCLUDE_PREFIX = 0x0010
ERROR_BUFFER_OVERFLOW = 111

IF_OPER_STATUS_UP = 1


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class IP_ADAPTER_GATEWAY_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_GATEWAY_ADDRESS._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", ctypes.c_ulong),
    ("ZoneIndices", ctypes.c_ulong * 16),
    ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong),
    ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", ctypes.c_void_p),
    ("FirstGatewayAddress", ctypes.POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Ipv4Metric", ctypes.c_ulong),
    ("Ipv6Metric", ctypes.c_ulong),
    ("Luid", ctypes.c_ulonglong),
]


def _sockaddr_to_ip(ptr, length):
    if not ptr or not length:
        return None
    raw = ctypes.string_at(ptr, length)
    family = struct.unpack_from("<H", raw, 0)[0]
    if family == AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[4:8])
    if family == AF_INET6:
        addr = raw[8:24]
        try:
            ip = socket.inet_ntop(socket.AF_INET6, addr)
        except OSError:
            return None
        scope = struct.unpack_from("<I", raw, 24)[0] if length >= 28 else 0
        if scope and (ip.startswith("fe80") or ip == "::"):
            ip = f"{ip}%{scope}"
        return ip
    return None


def _prefix_to_mask(prefix_len, v6=False):
    try:
        bits = 128 if v6 else 32
        net = ipaddress.ip_network(f"{'::' if v6 else '0.0.0.0'}/{prefix_len}", strict=False)
        return str(net.netmask)
    except Exception:
        return ""


def get_adapters():
    """返回适配器列表（含 IP、掩码、网关、跃点数）。"""
    size = wt.ULONG(15000)
    flags = (GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
             | GAA_FLAG_INCLUDE_GATEWAYS | GAA_FLAG_INCLUDE_PREFIX)
    buf = None
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        ret = ctypes.windll.iphlpapi.GetAdaptersAddresses(
            wt.ULONG(AF_UNSPEC), wt.ULONG(flags), None, buf, ctypes.byref(size))
        if ret == 0:
            break
        if ret != ERROR_BUFFER_OVERFLOW:
            raise OSError(f"GetAdaptersAddresses failed: {ret}")
    else:
        raise OSError("GetAdaptersAddresses buffer retry failed")

    adapters = []
    p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while p:
        a = p.contents
        name = a.FriendlyName or ""
        desc = a.Description or ""
        ips = []
        u = a.FirstUnicastAddress
        while u:
            ua = u.contents
            ip = _sockaddr_to_ip(ua.Address.lpSockaddr, ua.Address.iSockaddrLength)
            if ip:
                v6 = ":" in ip
                ips.append({
                    "ip": ip,
                    "family": "IPv6" if v6 else "IPv4",
                    "prefix": ua.OnLinkPrefixLength,
                    "mask": _prefix_to_mask(ua.OnLinkPrefixLength, v6),
                })
            u = ua.Next
        gws = []
        g = a.FirstGatewayAddress
        while g:
            ga = g.contents
            ip = _sockaddr_to_ip(ga.Address.lpSockaddr, ga.Address.iSockaddrLength)
            if ip:
                gws.append(ip)
            g = ga.Next
        mac = "-".join(f"{b:02X}" for b in a.PhysicalAddress[:a.PhysicalAddressLength]) \
            if a.PhysicalAddressLength else ""
        adapters.append({
            "name": name,
            "desc": desc,
            "guid": (a.AdapterName or b"").decode("ascii", "ignore"),
            "mac": mac,
            "up": a.OperStatus == IF_OPER_STATUS_UP,
            "if_type": a.IfType,
            "mtu": a.Mtu,
            "rx_speed": a.ReceiveLinkSpeed,
            "tx_speed": a.TransmitLinkSpeed,
            "metric_v4": a.Ipv4Metric,
            "metric_v6": a.Ipv6Metric,
            "ips": ips,
            "gateways": gws,
        })
        p = a.Next
    return adapters


def _decode(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("mbcs", "ignore")


def set_metric(name, family, metric):
    """设置接口跃点数。metric 为 int，None 表示恢复自动跃点。"""
    fam = "ipv4" if family == 4 else "ipv6"
    val = "automatic" if metric is None else str(int(metric))
    args = ["netsh", "interface", fam, "set", "interface", name, f"metric={val}"]
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(args, capture_output=True, creationflags=flags, timeout=15)
    except Exception as e:
        return False, str(e)
    msg = _decode((r.stdout or b"") + (r.stderr or b"")).strip()
    return r.returncode == 0, msg or ("成功" if r.returncode == 0 else f"错误码 {r.returncode}")


def get_ports(kind="inet"):
    """返回监听/连接端口列表。"""
    import psutil
    name_cache = {}
    rows = []
    try:
        conns = psutil.net_connections(kind=kind)
    except Exception:
        return rows
    for c in conns:
        if not c.laddr:
            continue
        pid = c.pid or 0
        if pid and pid not in name_cache:
            try:
                name_cache[pid] = psutil.Process(pid).name()
            except Exception:
                name_cache[pid] = "—"
        fam = "IPv6" if (c.laddr.ip and ":" in c.laddr.ip) else "IPv4"
        status = c.status or ""
        if status in ("NONE", "none"):
            status = "—"
        rows.append({
            "proto": ("TCP6" if fam == "IPv6" else "TCP") if c.type == socket.SOCK_STREAM else
                     ("UDP6" if fam == "IPv6" else "UDP"),
            "local": f"{c.laddr.ip}:{c.laddr.port}",
            "port": c.laddr.port,
            "remote": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "—",
            "status": status,
            "pid": pid,
            "proc": name_cache.get(pid, "—") if pid else "—",
        })
    return rows
