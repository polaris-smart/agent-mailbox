"""Metadata from the registered, running Gateway, never disk-config inference.

This is a private local diagnostic boundary, not injection authorization. It
reads no sessions, prompts, credentials or database handles and schedules no
work. A snapshot must be obtained afresh before selecting/binding a session.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path

SUPPORTED = {
    "hermes_cli.plugins": "b0f09d0004890ff0b2c07d17ee7c5688709162da4fd43d404cabbe13f1d9f59d",
    "gateway.run_plugin_injection": "fbac49a8dead1c3f1e665d89345ba2366ead90525ca16ae3d6f02ba441315bed",
    "gateway.session": "921e579363945f15972355a7b454df6bf8c385d50d3a88854a46f31abeffea01",
    "gateway.session_persistence": "a0a08e18afd925a320fa5faf439252c2d6a554caa481161c46f8e274d893492a",
    "hermes_constants": "5d43e6102c69748a2e64fc91623a49e1b0d76623416ce4ec37f23f216c3ce91f",
    "gateway.run": "2f6119b55c3a2ef61ab67397f13797cce9cf8b9f1e4e342a308e9c4cb4092098",
}

PRIVATE_SUPPORTED = {
    **SUPPORTED,
    "hermes_cli.plugins": "b67ff8526d79e06b5673845245f587598d2205dba5a5ed5f1db2e234ac1ce4d4",
    "gateway.run_plugin_injection": "aa7ab74a099462c8baaa14e9337606b653ef836614c1432bc840e4cdc1bcc866",
    "gateway.run_turn": "2166596c5f2f9ae07c37c81dacd51d24167310443ac2f8e71c1a8656e2a7e6a0",
}

# Complete session-authority generation, matching the separately pinned
# admission path in pinned.py. A mixed set must still fail closed.
AUTHORITY_SUPPORTED = {
    "hermes_cli.plugins": "09d2ecf9298118b156cd092190235507169fada6f0ea9ec32b6b61a5747e0549",
    "gateway.run_plugin_injection": "e4097818da5177cf779a30c06426ff4789bf986cdedd298c4439278d7272a7f1",
    "gateway.session": "632d8e541b07262b8af3006905d1719fdce1cd011b5573043a14380a9ceb0933",
    "gateway.session_persistence": "db4dad2fcdc378a0b7729e6f02d6dd31201dcf37008c92b07cfc546b15723183",
    "hermes_constants": "fa787b51131a857182df790d19f7285f39d91f2abfd9ecf8142909a1de7e130e",
    "gateway.run": "ad8e017ab49075f33f5659dfefe547ecb73467dd139e80e3fec39182393045f4",
}


def _sdk():
    # A diagnostic request must never import the runner. Accept only one
    # complete pinned generation; a mixture of legacy/private host files fails.
    if any(sys.modules.get(name) is None for name in SUPPORTED):
        raise ValueError("hermes_runtime_not_loaded")
    for expected in (SUPPORTED, PRIVATE_SUPPORTED, AUTHORITY_SUPPORTED):
        modules = {}
        for name, digest in expected.items():
            module = sys.modules.get(name)
            if module is None:
                break
            source = Path(module.__file__)
            if source.suffix != ".py" or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                break
            modules[name] = module
        else:
            return modules
    raise ValueError("unsupported_hermes_metadata_version")


def _directory(value):
    if not isinstance(value, (str, Path)):
        raise ValueError("live_metadata_path_unconfirmed")  # noqa: TRY004 - untrusted runtime state
    p = Path(value)
    if not p.is_absolute() or p.resolve(strict=True) != p:
        raise ValueError("live_metadata_path_unconfirmed")
    s = p.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError("live_metadata_path_unconfirmed")
    return p


def _bound(method, owner, implementation):
    return getattr(method, "__self__", None) is owner and getattr(method, "__func__", None) is implementation


class LiveRouting:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.registration = None
        self.instance_id = None

    def snapshot(self):
        with self.lock:
            modules = _sdk()
            native = modules["gateway.run_plugin_injection"].GatewayPluginInjectionMixin
            persistence = modules["gateway.session_persistence"].SessionPersistenceMixin
            manager = self.ctx._manager
            if (not isinstance(self.ctx, modules["hermes_cli.plugins"].PluginContext)
                    or not isinstance(manager, modules["hermes_cli.plugins"].PluginManager)):
                raise ValueError("live_plugin_context_unconfirmed")  # noqa: TRY004 - untrusted runtime state
            registration = manager._gateway_message_injector
            if (not isinstance(registration, tuple) or len(registration) != 2
                    or manager._cli_ref is not None or manager.has_tui_message_injector
                    or self.ctx._load_abandoned):
                raise ValueError("live_gateway_unavailable")
            owner, scheduler = registration
            if (not _bound(scheduler, owner, native._schedule_plugin_message_injection)
                    or not _bound(owner._served_profile_for_home, owner, native._served_profile_for_home)):
                raise ValueError("live_gateway_binding_unconfirmed")
            loop = owner._gateway_loop
            if (owner._running is not True or owner._draining or loop is None
                    or not loop.is_running() or loop.is_closed()):
                raise ValueError("live_gateway_unavailable")
            store = owner.session_store
            if (not isinstance(store, modules["gateway.session"].SessionStore)
                    or store.config is not owner.config
                    or not _bound(store._routing_scope, store, persistence._routing_scope)):
                raise ValueError("live_routing_store_unconfirmed")
            home = _directory(manager.home_path)
            routing = _directory(store._routing_home)
            scope = _directory(store._routing_scope())
            multiplex = owner.config.multiplex_profiles
            profile = owner._served_profile_for_home(home)
            gateway_profile = modules["hermes_constants"].profile_name_for_home(routing)
            if (type(multiplex) is not bool or any(not isinstance(p, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", p)
                    for p in (profile, gateway_profile))
                    or (not multiplex and (home != routing or profile != gateway_profile))):
                raise ValueError("live_profile_not_served")
            # Refuse an observed replacement/drain during collection. This is a
            # point-in-time diagnostic, not a lease on future host execution.
            if (manager._gateway_message_injector is not registration
                    or owner.session_store is not store or owner._gateway_loop is not loop
                    or owner._running is not True or owner._draining
                    or self.ctx._load_abandoned or manager._cli_ref is not None
                    or manager.has_tui_message_injector or Path(manager.home_path) != home
                    or owner.config is not store.config or owner.config.multiplex_profiles is not multiplex
                    or Path(store._routing_home) != routing or store._routing_scope() != str(scope)):
                raise ValueError("live_gateway_changed")
            if self.registration is not registration:
                self.registration = registration
                self.instance_id = secrets.token_hex(16)
            return {"protocol": "agent-mailbox.hermes-routing.v1", "host_kind": "hermes",
                    "instance_id": self.instance_id, "pid": os.getpid(), "observed_at": time.time(),
                    "profile_id": profile, "profile_home": str(home),
                    "routing_home": str(routing), "scope": str(scope),
                    "gateway_profile": gateway_profile, "multiplex": multiplex,
                    "session_verified": False, "delivery_verified": False}
