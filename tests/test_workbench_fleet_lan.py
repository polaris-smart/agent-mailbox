"""LAN discovery suggests addresses but cannot mint or validate Fleet trust."""

import ipaddress
from typing import ClassVar

import agent_mailbox.workbench_fleet_lan as lan
from agent_mailbox.workbench_fleet import FleetCoordinator
from agent_mailbox.workbench_store import WorkbenchStore


class FakeZeroconf:
    instances: ClassVar[list] = []

    def __init__(self, interfaces=None):
        self.interfaces = interfaces
        self.registered = []
        self.withdrawn = []
        self.closed = False
        self.instances.append(self)

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.withdrawn.append(info)

    def close(self):
        self.closed = True

    def get_service_info(self, type_, name, timeout):
        assert type_ == lan.SERVICE_TYPE and timeout == 250
        return {
            "one": lan.ServiceInfo(
                type_,
                "one." + type_,
                addresses=[ipaddress.IPv4Address("192.168.1.12").packed],
                port=49001,
                properties={"protocol": "fleet-v1"},
            ),
            "outside": lan.ServiceInfo(
                type_,
                "outside." + type_,
                addresses=[ipaddress.IPv4Address("8.8.8.8").packed],
                port=49002,
                properties={"protocol": "fleet-v1"},
            ),
            "other": lan.ServiceInfo(
                type_,
                "other." + type_,
                addresses=[ipaddress.IPv4Address("192.168.1.13").packed],
                port=49003,
                properties={"protocol": "unknown"},
            ),
        }[name]


class FakeBrowser:
    def __init__(self, zc, type_, listener):
        assert type_ == lan.SERVICE_TYPE
        self.cancelled = False
        for name in ("one", "outside", "other"):
            listener.add_service(zc, type_, name)

    def cancel(self):
        self.cancelled = True


def test_lan_advertisement_has_no_project_or_pairing_secret_and_withdraws(monkeypatch):
    FakeZeroconf.instances.clear()
    monkeypatch.setattr(lan, "Zeroconf", FakeZeroconf)
    advertiser = lan.FleetLanAdvertiser("https://192.168.1.12:49001")
    zc = FakeZeroconf.instances[-1]
    assert zc.registered == [advertiser.info]
    assert zc.interfaces == ["192.168.1.12"]
    assert advertiser.info.parsed_addresses() == ["192.168.1.12"]
    assert advertiser.info.properties == {b"protocol": b"fleet-v1"}
    advertiser.close()
    assert zc.withdrawn == [advertiser.info] and zc.closed


def test_discovery_returns_only_unverified_private_address_hints(monkeypatch):
    FakeZeroconf.instances.clear()
    monkeypatch.setattr(lan, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(lan, "ServiceBrowser", FakeBrowser)
    assert lan.discover_lan(timeout=0.01) == [
        {"base_url": "https://192.168.1.12:49001", "verified": False}
    ]
    assert FakeZeroconf.instances[-1].closed


def test_loopback_cannot_be_advertised():
    try:
        lan.FleetLanAdvertiser("https://127.0.0.1:49001")
    except ValueError:
        pass
    else:
        raise AssertionError("loopback listener must not advertise")


def test_listener_advertises_only_explicit_lan_and_withdraws_on_stop(tmp_path, monkeypatch):
    seen = []

    class Advertiser:
        def __init__(self, url):
            seen.append(("register", url))

        def close(self):
            seen.append(("withdraw",))

    monkeypatch.setattr(lan, "FleetLanAdvertiser", Advertiser)
    coordinator = FleetCoordinator(WorkbenchStore(tmp_path))
    coordinator.start(host="127.0.0.1")
    assert seen == []
    coordinator.stop()
    coordinator.start(host="127.0.0.1", advertised_host="192.168.1.12")
    assert seen == [("register", coordinator.base_url)]
    coordinator.stop()
    assert seen[-1] == ("withdraw",)


def test_multicast_failure_does_not_disable_manual_tls_pairing(tmp_path, monkeypatch):
    def unavailable(_url):
        raise OSError("multicast unavailable")

    monkeypatch.setattr(lan, "FleetLanAdvertiser", unavailable)
    coordinator = FleetCoordinator(WorkbenchStore(tmp_path))
    started = coordinator.start(host="127.0.0.1", advertised_host="192.168.1.12")
    try:
        assert started["base_url"] == coordinator.base_url
        assert coordinator.server is not None
        assert coordinator.lan_error == "OSError"
    finally:
        coordinator.stop()
