"""LAN mesh network interface helpers: choose the bind address, check that an address is private.

Reads only. ``default_lan_ip`` uses the UDP connect trick: ``connect`` on a UDP socket picks a
route and a source address, but sends no packet.

Private (for the mesh) = RFC 1918 (10/8, 172.16/12, 192.168/16), link-local (169.254/16,
fe80::/10), ULA (fc00::/7) and loopback (127/8, ::1). This is narrower than
``ipaddress.is_private``, which also accepts reserved and documentation ranges.
"""
from __future__ import annotations

import ipaddress
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


def choose_bind(bind: str | None, *, allow_public: bool = False) -> str:
    """Return the one address to bind. Raises ValueError.

    ``bind=None`` uses ``default_lan_ip()``. Always refused: ``0.0.0.0``, ``::`` (all interfaces)
    and multicast. A non-private address needs ``allow_public=True``.
    """
    if bind is None:
        ip = default_lan_ip()
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
