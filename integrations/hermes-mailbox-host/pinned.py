"""Version-bounded Hermes Gateway admission protection used by Receiver.

The public inject_message API has no expected session id. This compatibility
adapter calls the unchanged official dispatch method with a per-request facade.
Only its adapter admission is narrowed: a strict event must name the authorized
session id before any content reaches the real adapter. No host monkeypatch,
new session, model call or fallback to public unpinned injection is allowed.
This protects admission, not the entire host turn or compression continuation.
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

SUPPORTED = {
    "hermes_cli.plugins": "b0f09d0004890ff0b2c07d17ee7c5688709162da4fd43d404cabbe13f1d9f59d",
    "gateway.run_plugin_injection": "fbac49a8dead1c3f1e665d89345ba2366ead90525ca16ae3d6f02ba441315bed",
    "gateway.run_turn": "14b326c34e15fac2951efc471bb5c34f63ccda6f6c4a6c2156cbed4375cd726b",
    "gateway.run_busy": "e4baf0d89c3d035660be743543fc0504348619d17d7df2ddb28dfe066b666ddb",
}

# Hermes Gateway's session-authority generation. The automation admission path
# bypasses the old adapter facade, so it needs its own pinned admission guard.
AUTHORITY_SUPPORTED = {
    "hermes_cli.plugins": "09d2ecf9298118b156cd092190235507169fada6f0ea9ec32b6b61a5747e0549",
    "gateway.run_plugin_injection": "e4097818da5177cf779a30c06426ff4789bf986cdedd298c4439278d7272a7f1",
    "gateway.run_turn": "c546b5cbc19bfb28606dee9d25e46f6bf0fc6bde03f2695d6b1f5ade755d4566",
    "gateway.run_turn_prepare": "7debb42ae2b7607d8e3e1d75e8590b1f2b49f663461036bf2b32113562c7acf6",
    "gateway.run_busy": "dc39233ef2fde3f3bd2475b354ab4ef8137ed6340566333713712acc439b3cb2",
    "gateway.session_automation": "ef599876368b05e9974bc644dee12b6a9fa73636e8afe0b13125b6b287aad865",
    "gateway.session_authority": "75dcc046a40643031fdfae91ead25c60deee586003e381ddf2249ce690d65bc8",
    "gateway.session_authorities": "e29986bffd8c58c69fd9eec244a6e02eba992101e111ca70da5415fa1e176627",
    "gateway.session_envelope": "948d26349e3fb6ea865af33e18dafd2e4177e5fd887afb271de89363f4a0d68a",
    "gateway.session_ingress": "d498954d3867337e3beda70adfbef0f581671e72e73e0526ae0eefe37101127f",
}

# Private mailbox work requires the complete, independently tested host patch.
# Keep it separate from the existing Gateway pin: mixed host generations fail.
PRIVATE_SUPPORTED = {
    "hermes_cli.plugins": "b67ff8526d79e06b5673845245f587598d2205dba5a5ed5f1db2e234ac1ce4d4",
    "hermes_cli.plugins_injection": "4155f6b4f82368839e67b4f7491f99c628dace04d0ec08d628c3572231c22ae1",
    "gateway.run_plugin_injection": "74f46767be1120262cb729c83bef4f62157ccb8c937e053bc467040bd39fb02c",
    "gateway.run_turn": "2166596c5f2f9ae07c37c81dacd51d24167310443ac2f8e71c1a8656e2a7e6a0",
    "gateway.run_busy": "e02ebfe061f65e760a8a95e647d1cae4445572eaed6e758aebf6955cd4c9c171",
    "gateway.run_inbound": "6ebee29aea7fc91275bff5653fd604125887b9581739e976b834a68929706903",
    "gateway.reply_policy": "7241c2263e52b56d044f97346e045f9dfdcbfeae53c8579c447216b19e1802a7",
    "gateway.platforms.event": "d3fc11d5b691f194a99ed965679b23033f0b78a35cee60fa9854e4bf574e6d01",
    "gateway.platforms.base": "34541fbd80101b06cb68d93df6e22858b21f85914d74d0811d50e5035d3d6eb4",
    "gateway.turn_context": "e58da05fa18cb82b500473b1462edd5f6482b92a3db2a115d82c393482d6c64c",
    "plugins.platforms.feishu.adapter": "72d9e86c47d7484d6cc410ee1e16ef4a6fb05d2a276395fb42efa2d44ed45920",
    "plugins.platforms.feishu.feishu_comment": "20fa061a63fca6b91b066751e1c4b90976eb8fc0c344706a9109bff117480ee5",
}


def compatible_sdk(*, private_mode=False):
    """Unknown SDKs stop here; installing an update is not a compatibility proof."""
    for expected_set in ((PRIVATE_SUPPORTED,) if private_mode else (SUPPORTED, AUTHORITY_SUPPORTED)):
        modules = {}
        for name, expected in expected_set.items():
            module = importlib.import_module(name)
            source = Path(module.__file__)
            if source.suffix != ".py" or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                break
            modules[name] = module
        else:
            return modules
    raise ValueError("unsupported_hermes_dispatch_version")


def _bound_to(method, owner, implementation):
    return getattr(method, "__self__", None) is owner and getattr(method, "__func__", None) is implementation


class PinnedGateway:
    """One delivery, one registration, one immutable authorization pin.

    still_authorized must recheck the caller's binding and receiver lifecycle;
    it must not read a database closed during unload. A True schedule result
    means only queued, never a transport receipt or task acceptance.
    """

    def __init__(self, ctx, *, session_key, session_id, platform, still_authorized,
                 private_mode=False):
        self.private_mode = private_mode is True
        modules = compatible_sdk(private_mode=self.private_mode)
        self.authority_mode = "gateway.session_automation" in modules
        self.authorities = modules.get("gateway.session_authorities")
        self.automation = modules.get("gateway.session_automation")
        self.native = modules["gateway.run_plugin_injection"].GatewayPluginInjectionMixin
        turn = modules["gateway.run_turn"].GatewayTurnMixin
        context = modules["hermes_cli.plugins"].PluginContext
        if not all(isinstance(v, str) and v and len(v) <= 512 and not any(ord(c) < 32 for c in v)
                   for v in (session_key, session_id, platform)) or not callable(still_authorized):
            raise ValueError("invalid_session_pin")
        self.ctx = ctx
        self.registration = ctx._manager._gateway_message_injector
        if not isinstance(self.registration, tuple) or len(self.registration) != 2:
            raise ValueError("gateway_unavailable")
        self.owner, scheduler = self.registration
        for method, owner, implementation in (
            (scheduler, self.owner, self.native._schedule_plugin_message_injection),
            (self.owner._dispatch_plugin_message_injection, self.owner, self.native._dispatch_plugin_message_injection),
            (self.owner._hmwa_resolve_session, self.owner, turn._hmwa_resolve_session),
            (ctx._gateway_injection_allowed, ctx, context._gateway_injection_allowed),
            (ctx.spawn_task, ctx, context.spawn_task),
        ):
            if not _bound_to(method, owner, implementation):
                raise ValueError("unsupported_hermes_dispatch_binding")
        if self.authority_mode and not _bound_to(
                self.owner._admit_plugin_injection, self.owner, self.native._admit_plugin_injection):
            raise ValueError("unsupported_hermes_dispatch_binding")
        if self.authority_mode and not _bound_to(
                self.owner._plugin_messaging_session_owned, self.owner,
                self.native._plugin_messaging_session_owned):
            raise ValueError("unsupported_hermes_dispatch_binding")
        self.resolver = turn._hmwa_resolve_session
        self.key, self.sid, self.platform = session_key, session_id, platform
        self.still_authorized = still_authorized
        self.loop = self.owner._gateway_loop
        self.entered = False

    def _authority_route_ready(self):
        if not self.authority_mode:
            return True
        authority = self.authorities.authority_for_home(
            self.owner, self.ctx._manager.home_path
        )
        if authority is None:
            # An unserved profile must not look like a standalone Gateway.
            return (getattr(self.owner, "session_authorities", None) is None
                    and getattr(self.owner, "session_authority", None) is None)
        live = authority.sessions.get(self.sid)
        if live is not None:
            return live.route == self.key and not getattr(live, "superseded", False)
        # A Gateway restart restores local sessions, but messaging sessions are
        # mounted on first inbound. Its official automation path can admit a
        # cold messaging route when it has committed human provenance; requiring
        # an in-memory LiveSession here would strand mail until the Human writes.
        # Check the exact persisted route and provenance before Receiver journals
        # anything. Hermes rechecks both at durable FIFO admission.
        entry = self.owner.session_store.lookup_by_session_key(self.key)
        if (entry is None or entry.session_id != self.sid or entry.session_key != self.key
                or entry.origin is None
                or getattr(entry.origin.platform, "value", entry.origin.platform) != self.platform):
            return False
        return self.automation.committed_route_envelope(authority, entry) is not None

    def current(self):
        try:
            manager = self.ctx._manager
            # The Gateway may rotate a session while keeping its routing key.
            # A live injector is not proof that this exact session pin is still
            # current. Refuse before Receiver persists a dispatching journal row.
            session_store = self.owner.session_store
            return (
                (not self.private_mode or self.platform == "feishu")
                and manager._gateway_message_injector is self.registration
                and manager._cli_ref is None
                and not manager.has_tui_message_injector
                and (not self.authority_mode or self.owner._plugin_messaging_session_owned(
                    self.key, plugin_home=manager.home_path, plugin_id=self.ctx.plugin_id))
                and not self.ctx._load_abandoned
                and self.ctx._gateway_injection_allowed()
                and self.owner._plugin_injection_accepting()
                and self.owner._gateway_loop is self.loop
                and self._authority_route_ready()
                and session_store.peek_session_id(self.key) == self.sid
                and _bound_to(self.owner._hmwa_resolve_session, self.owner, self.resolver)
                and self.loop.is_running() and not self.loop.is_closed()
                and self.still_authorized() is True
            )
        except Exception:  # noqa: BLE001 — unknown host state closes admission
            return False

    async def dispatch(self, content):
        if (self.private_mode or self.entered or not self.current()
                or not isinstance(content, str) or not content):
            return False
        self.entered = True
        try:
            facade = _Dispatch(self)
            kwargs = {"session_key": self.key, "content": content,
                      "plugin_id": self.ctx.plugin_id}
            if self.authority_mode:
                # Current Hermes also fences messaging sessions by the plugin's
                # immutable profile home. Preserve that official dispatch input.
                kwargs["plugin_home"] = self.ctx._manager.home_path
            await self.native._dispatch_plugin_message_injection(
                facade, **kwargs,
            )
            return facade.admitted
        except Exception:  # noqa: BLE001 — external host dispatch boundary
            # Scheduling/admission errors never cause an unpinned retry.
            return False

    def schedule(self, content):
        if self.private_mode:
            return False  # Private mode uses only its separately authorized adapter.
        if not self.current():
            return False

        def launch():
            if not self.current():
                return
            coro = self.dispatch(content)
            try:
                self.ctx.spawn_task(coro, name="agent-mailbox:pinned-notification")
            except Exception:  # noqa: BLE001 — official plugin lifecycle boundary
                coro.close()

        try:
            self.loop.call_soon_threadsafe(launch)
            return True
        except RuntimeError:
            return False


class _Dispatch:
    def __init__(self, request):
        self.request = request
        self.admitted = False

    def __getattr__(self, name):
        return getattr(self.request.owner, name)

    def _plugin_injection_accepting(self):
        return self.request.current()

    async def _admit_plugin_injection(self, adapter, source, *, session_key,
                                     session_id, content, plugin_id):
        """Guard the durable FIFO path, which never calls the adapter facade."""
        request = self.request
        if not isinstance(adapter, _Admission) or adapter.facade is not self:
            return False
        native_adapter = adapter.adapter
        platform = getattr(source.platform, "value", source.platform)
        if not (request.authority_mode and request.current()
                and session_key == request.key and session_id == request.sid
                and platform == request.platform
                and plugin_id == request.ctx.plugin_id
                and request.owner._plugin_messaging_session_owned(
                    session_key, plugin_home=request.ctx._manager.home_path,
                    plugin_id=plugin_id)
                and request.owner._session_key_for_source(source) == request.key
                and request.owner._plugin_injection_authorized(
                    source, plugin_id=request.ctx.plugin_id, target=f"session={request.key}")):
            return False
        admitted = await request.owner._admit_plugin_injection(
            native_adapter, source, session_key=session_key, session_id=session_id,
            content=content, plugin_id=plugin_id)
        if admitted is None:
            return None  # no authority: host takes the strict adapter path below
        self.admitted = admitted is True
        return self.admitted

    def _delivery_adapter_for(self, source):
        adapter = self.request.owner._delivery_adapter_for(source)
        return _Admission(self, adapter) if adapter is not None else None


class _Admission:
    def __init__(self, facade, adapter):
        self.facade, self.adapter = facade, adapter

    async def handle_message(self, event):
        request = self.facade.request
        metadata = event.metadata or {}
        platform = getattr(event.source.platform, "value", event.source.platform)
        if not (
            request.current()
            and metadata.get("gateway_session_key") == request.key
            and metadata.get("gateway_session_id") == request.sid
            and metadata.get("gateway_session_strict") is True
            and platform == request.platform
            and request.owner._session_key_for_source(event.source) == request.key
            and event.internal is True and event.allow_gateway_control is False
            and request.owner._plugin_injection_authorized(
                event.source, plugin_id=request.ctx.plugin_id, target=f"session={request.key}",
            )
        ):
            return
        self.facade.admitted = True
        await self.adapter.handle_message(event)
