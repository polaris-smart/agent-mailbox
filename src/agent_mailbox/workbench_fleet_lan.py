"""Untrusted LAN address hints for an explicitly enabled Fleet listener.

Discovery never carries an invitation or changes pairing state. The client must
still verify the coordinator certificate fingerprint from a trusted invitation.
"""

from __future__ import annotations

import ipaddress
import secrets
import threading
from urllib.parse import urlsplit

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

from .workbench_store import WorkbenchError

SERVICE_TYPE = "_agent-mailbox._tcp.local."
MAX_RESULTS = 32


def _lan_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(
        ip in network
        for network in (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
    )


class FleetLanAdvertiser:
    def __init__(self, base_url: str):
        endpoint = urlsplit(base_url)
        if endpoint.scheme != "https" or not _lan_ipv4(endpoint.hostname or ""):
            raise ValueError("Only an explicit LAN IPv4 HTTPS listener may be advertised.")
        self.zeroconf = Zeroconf(interfaces=[endpoint.hostname])
        self.info = ServiceInfo(
            SERVICE_TYPE,
            f"Mailbox-{secrets.token_hex(4)}.{SERVICE_TYPE}",
            addresses=[ipaddress.IPv4Address(endpoint.hostname).packed],
            port=endpoint.port,
            properties={"protocol": "fleet-v1"},
        )
        try:
            self.zeroconf.register_service(self.info)
        except Exception:
            self.zeroconf.close()
            raise

    def close(self):
        try:
            self.zeroconf.unregister_service(self.info)
        finally:
            self.zeroconf.close()


class _Listener:
    def __init__(self):
        self.names: set[str] = set()
        self.lock = threading.Lock()

    def add_service(self, _zc, _type, name):
        with self.lock:
            if len(self.names) < MAX_RESULTS:
                self.names.add(name)

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)

    def remove_service(self, _zc, _type, name):
        with self.lock:
            self.names.discard(name)


def discover_lan(timeout=1.5):
    """Return fresh address suggestions; none of these are trusted identities."""
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 5:
        raise ValueError("Discovery timeout must be between 0 and 5 seconds.")
    try:
        zc = Zeroconf()
    except (OSError, RuntimeError) as exc:
        raise WorkbenchError("network_error", "局域网发现不可用，请手动填写邀请中的地址。") from exc
    browser = None
    try:
        listener = _Listener()
        browser = ServiceBrowser(zc, SERVICE_TYPE, listener)
        threading.Event().wait(timeout)
        with listener.lock:
            names = sorted(listener.names)
        results = []
        seen = set()
        for name in names:
            try:
                info = zc.get_service_info(SERVICE_TYPE, name, timeout=250)
            except (OSError, RuntimeError, ValueError):
                continue
            if not info or info.properties.get(b"protocol") != b"fleet-v1":
                continue
            if not isinstance(info.port, int) or not 1 <= info.port <= 65535:
                continue
            try:
                addresses = info.parsed_addresses()
            except (OSError, RuntimeError, ValueError):
                continue
            for address in addresses:
                if not _lan_ipv4(address):
                    continue
                base_url = f"https://{address}:{info.port}"
                if base_url not in seen:
                    seen.add(base_url)
                    results.append({"base_url": base_url, "verified": False})
                if len(results) >= MAX_RESULTS:
                    return results
        return results
    except (OSError, RuntimeError) as exc:
        raise WorkbenchError("network_error", "局域网发现不可用，请手动填写邀请中的地址。") from exc
    finally:
        if browser is not None:
            browser.cancel()
        zc.close()
