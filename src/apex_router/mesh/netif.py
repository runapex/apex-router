"""LAN mesh network interface helpers: choose the bind address, check that an address is private.

Reads only. ``default_lan_ip`` uses the UDP connect trick: ``connect`` on a UDP socket picks a
route and a source address, but sends no packet.

Private (for the mesh) = RFC 1918 (10/8, 172.16/12, 192.168/16), link-local (169.254/16,
fe80::/10), ULA (fc00::/7) and loopback (127/8, ::1). This is narrower than
``ipaddress.is_private``, which also accepts reserved and documentation ranges.
"""
from __future__ import annotations

import ipaddress
import re
import socket

_PRIVATE_V4 = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",   # RFC 1918
    "169.254.0.0/16",                                    # link-local
    "127.0.0.0/8",                                       # loopback
))
_PRIVATE_V6 = tuple(ipaddress.ip_network(n) for n in (
    "fe80::/10",                                         # link-local
    "fc00::/7",                                          # ULA
    "::1/128",                                           # loopback
))
_PROBE_V4 = "192.0.2.1"          # TEST-NET-1 (RFC 5737): never routed, no packet is sent
_PROBE_PORT = 9                  # discard


def _parse(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse ``ip`` (an IPv6 zone ``%eth0`` is allowed). Raises ValueError."""
    if not isinstance(ip, str) or not 0 < len(ip) <= 64:
        raise ValueError(f"not an IP address: {ip!r}")
    addr = ipaddress.ip_address(ip.strip())
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_private(ip: str) -> bool:
    """True for an RFC 1918, link-local, ULA or loopback address. False for anything else."""
    try:
        addr = _parse(ip)
    except ValueError:
        return False
    nets = _PRIVATE_V4 if addr.version == 4 else _PRIVATE_V6
    return any(addr in n for n in nets)


def default_lan_ip() -> str | None:
    """Source address of the default IPv4 route, or None. No packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((_PROBE_V4, _PROBE_PORT))
            ip = s.getsockname()[0]
    except OSError:
        return None
    try:
        addr = _parse(ip)
    except ValueError:
        return None
    if addr.is_unspecified:
        return None
    return str(addr)


# Tunnel and virtual interfaces (macOS names). A VPN takes the default route through one of these;
# its address is not reachable from the LAN, so the mesh must not bind it by default.
# Exact name patterns, not loose prefixes: "br-lan" (a LAN bridge) and "local0" must stay eligible.
_TUNNEL_RE = re.compile(
    r"\A(?:lo\d*"                                        # loopback
    r"|(?:utun|ipsec|ppp|tun|tap|gif|stf|awdl|llw|anpi|vmenet)\d+"   # macOS tunnels, AWDL
    r"|(?:tunl|sit|gre|gretap|erspan|ip6tnl|ip6gre|ip_vti|ip6_vti)\d+"  # Linux IP tunnels
    r"|bridge\d+"                                         # macOS VM / Internet Sharing bridge
    r"|wg\d+|tailscale\d+|zt[0-9a-z]{8,}|nebula\d*"      # WireGuard, Tailscale, ZeroTier, Nebula
    r"|docker\d+|br-[0-9a-f]{12}|veth[0-9a-z]+|virbr\d+" # Docker, libvirt
    r"|cni\d+|flannel\.\d+|vxlan\.?\w*|kube-\w+|lxcbr\d+|lxdbr\d+|podman\d+)\Z")


def _is_tunnel(name: str) -> bool:
    return bool(_TUNNEL_RE.match(name))
# Physical LAN names first: macOS en*, Linux eth*, en* (enp0s3, eno1), wl* (wlan0, wlp2s0).
_LAN_PREFIXES = ("en", "eth", "wl")


# Linux hosts without net-tools (openSUSE, minimal images) have only iproute2 ``ip``.
_IP_PATHS = ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")


def _ip_bin() -> str | None:
    import os
    return next((p for p in _IP_PATHS if os.access(p, os.X_OK)), None)


def _ip_addrs(addr_text: str, link_text: str) -> list[tuple[str, str, bool]]:
    """[(interface, IPv4 address, status active)] from ``ip -o -4 addr`` and ``ip -o link``.

    Active = the link flags hold UP and LOWER_UP (carrier), the ``ip`` form of UP + RUNNING. An
    interface missing from ``link_text`` counts as active, as ``ifconfig`` output without flags.
    """
    active = {}
    for line in link_text.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or "<" not in parts[2]:
            continue
        name = parts[1].strip().split("@", 1)[0]          # "veth1a2b@if2" -> "veth1a2b"
        flags = parts[2].split("<", 1)[1].split(">", 1)[0].split(",")
        active[name] = "UP" in flags and "LOWER_UP" in flags
    out = []
    for line in addr_text.splitlines():
        parts = line.split()
        # "2: wlp1s0    inet 10.0.0.221/24 brd ... scope global wlp1s0\ ..."
        if len(parts) >= 4 and parts[2] == "inet":
            name = parts[1].split("@", 1)[0]
            out.append((name, parts[3].split("/", 1)[0], active.get(name, True)))
    return out


def _iface_addrs(text: str | None = None) -> list[tuple[str, str, bool]]:
    """[(interface, IPv4 address, status active)] from ``ifconfig``, else from ``ip``. [] on any error."""
    import subprocess
    if text is None:
        try:
            text = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=3,
                                  check=False).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        if not text.strip():
            ip = _ip_bin()
            if ip is None:
                return []
            try:
                addr = subprocess.run([ip, "-o", "-4", "addr", "show"], capture_output=True,
                                      text=True, timeout=3, check=False).stdout
                link = subprocess.run([ip, "-o", "link", "show"], capture_output=True,
                                      text=True, timeout=3, check=False).stdout
            except (OSError, subprocess.SubprocessError):
                return []
            return _ip_addrs(addr, link)
    out, name, addrs, active = [], None, [], True

    def flush():
        if name:
            out.extend((name, a, active) for a in addrs)

    for line in text.splitlines():
        if line and not line[0].isspace():
            flush()
            name, addrs = line.split(":", 1)[0], []
            # "flags=8863<UP,BROADCAST,...,RUNNING,...>": up and running (macOS and Linux)
            flags = line.split("<", 1)[1].split(">", 1)[0].split(",") if "<" in line else []
            active = not flags or ("UP" in flags and "RUNNING" in flags)
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet":
            addrs.append(parts[1])
        elif len(parts) >= 2 and parts[0] == "status:":
            active = active and parts[1] == "active"
    flush()
    return out


def lan_ip(default: str | None = None, ifaces: list[tuple[str, str, bool]] | None = None) -> str | None:
    """The address to bind when no ``--bind`` is given.

    The source address of the default route, unless it sits on a tunnel interface (a VPN). Then the
    first private IPv4 of an active non-tunnel interface (``en*`` first). Else the default route
    address as before. ``default`` and ``ifaces`` are for tests.
    """
    ip = default if default is not None else default_lan_ip()
    rows = _iface_addrs() if ifaces is None else ifaces
    owner = next((n for n, a, _ in rows if a == ip), None)
    if ip is not None and (owner is None or not _is_tunnel(owner)):
        return ip
    lan = [(n, a) for n, a, up in rows
           if up and not _is_tunnel(n) and is_private(a)
           and not _parse(a).is_loopback and not a.startswith("169.254.")]
    lan.sort(key=lambda r: (not r[0].startswith(_LAN_PREFIXES), r[0]))
    return lan[0][1] if lan else ip


def choose_bind(bind: str | None, *, allow_public: bool = False) -> str:
    """Return the one address to bind. Raises ValueError.

    ``bind=None`` uses ``lan_ip()`` (the default route address, but not a VPN tunnel address).
    Always refused: ``0.0.0.0``, ``::`` (all interfaces) and multicast. A non-private address
    needs ``allow_public=True``.
    """
    if bind is None:
        ip = lan_ip()
        if ip is None:
            raise ValueError("no LAN address found; pass --bind IP")
        src = "default LAN address"
    else:
        ip = bind
        src = "--bind"
    try:
        addr = _parse(ip)
    except ValueError:
        raise ValueError(f"{src} {ip!r} is not an IP address") from None
    if addr.is_unspecified:
        raise ValueError(f"{src} {ip!r} binds all interfaces; give one LAN address")
    if addr.is_multicast:
        raise ValueError(f"{src} {ip!r} is a multicast address")
    out = str(addr)                  # normalized; keeps an IPv6 zone
    if not is_private(out) and not allow_public:
        raise ValueError(f"{src} {ip!r} is not a private LAN address; "
                         "pass --allow-public to bind it")
    return out


# -- interface pinning (VPN split routes) -----------------------------------------------------
# A VPN can install a route for the home LAN subnet (for example 10.0.0.0/24 via utun). Then a
# socket bound to the LAN address still sends its packets into the VPN: bind() sets only the source
# address, the route table picks the interface. macOS IP_BOUND_IF / IPV6_BOUND_IF pin a socket to
# one interface, so mesh traffic stays on the LAN. Values from <netinet/in.h> / <netinet6/in6.h>;
# Python does not export them. On other systems pin_socket is a no-op.
_IP_BOUND_IF = 25
_IPV6_BOUND_IF = 125


def iface_index_for_ip(ip: str) -> int | None:
    """Index of the interface that owns ``ip`` (macOS ``ifconfig``), or None. Never raises."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return None
    try:
        want = _parse(ip)
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=3,
                             check=False).stdout
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    name = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            name = line.split(":", 1)[0]
            continue
        parts = line.split()
        if name and len(parts) >= 2 and parts[0] in ("inet", "inet6"):
            addr = parts[1].split("%", 1)[0]
            try:
                if ipaddress.ip_address(addr) == want:
                    return socket.if_nametoindex(name)
            except (ValueError, OSError):
                continue
    return None


def pin_socket(sock: socket.socket, ip: str | None) -> bool:
    """Pin ``sock`` to the interface that owns ``ip`` (macOS). True if pinned. Never raises.

    No-op for loopback, for an unknown address, and on systems without IP_BOUND_IF.
    """
    if not ip:
        return False
    try:
        if _parse(ip).is_loopback:
            return False
    except ValueError:
        return False
    idx = iface_index_for_ip(ip)
    if idx is None:
        return False
    try:
        if sock.family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, _IPV6_BOUND_IF, idx)
        else:
            sock.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, idx)
        return True
    except OSError:
        return False
