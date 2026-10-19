"""Real Hermes dispatch/strict resolution/lifecycle, synthetic routing and adapter.

Run with the installed Hermes interpreter under a synthetic HOME. Never starts
a Gateway, model, real chat adapter, or production profile.
"""

import asyncio
import contextlib
import datetime
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host/pinned.py"
spec = importlib.util.spec_from_file_location("pinned_candidate", SOURCE)
pinned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pinned)


class PinnedDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if importlib.util.find_spec("hermes_cli") is None:
            self.skipTest("Requires installed Hermes interpreter; a skip is not host validation")
        from gateway.run_plugin_injection import GatewayPluginInjectionMixin
        from gateway.run_turn import GatewayTurnMixin
        from gateway.session import SessionEntry, SessionSource
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-pinned-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.grant(True)
        self.key = "agent:main:feishu:dm:synthetic"
        self.source = SessionSource(platform="feishu", chat_id="synthetic", chat_type="dm")
        self.entry_type = SessionEntry
        self.before_lookup = None
        self.before_admission = None
        self.before_resolution = None
        self.after_resolution = None
        self.received, self.resolved, self.auth_calls = [], [], []
        self.lookup_count = 0
        self.bound = True
        case = self

        class Store:
            async def lookup_by_session_key(self, key):
                case.lookup_count += 1
                if case.before_lookup:
                    action, case.before_lookup = case.before_lookup, None
                    await action()
                return case.entry if key == case.key else None

        class Adapter:
            async def handle_message(self, event):
                case.received.append(event)
                if case.before_resolution:
                    await case.before_resolution()
                result = await case.owner._hmwa_resolve_session(event, event.source)
                if result:
                    case.resolved.append(result[1].session_id)
                    if case.after_resolution:
                        case.after_resolution(event, result[1])

        class Gateway(GatewayPluginInjectionMixin, GatewayTurnMixin):
            _running = True
            _draining = False

            def _served_profile_for_home(self, home):
                return "main" if Path(home).resolve() == case.root else None

            def _restored_source(self, entry):
                return entry.origin

            def _is_user_authorized_for_source(self, source, *, allow_adapter_delegation):
                case.auth_calls.append(allow_adapter_delegation)
                return case.gateway_allowed

            def _delivery_adapter_for(self, source):
                return case.adapter

            @contextlib.asynccontextmanager
            async def _plugin_injection_scope(self, source):
                if case.before_admission:
                    await case.before_admission()
                yield

            def _session_key_for_source(self, source):
                return case.key

            def _cache_session_source(self, key, source):
                pass

            def _is_telegram_topic_lane(self, source):
                return False

        self.gateway_allowed = True
        self.adapter = Adapter()
        self.owner = Gateway()
        self.owner.async_session_store = Store()
        self.owner.session_store = SimpleNamespace(
            peek_session_id=lambda key: self.entry.session_id if key == self.key else None
        )
        self.owner._gateway_loop = asyncio.get_running_loop()
        self.manager = PluginManager(scope_key=str(self.root))
        self.manager.set_gateway_message_injector(
            self.owner, self.owner._schedule_plugin_message_injection
        )
        self.ctx = PluginContext(PluginManifest(name="agent-mailbox-host"), self.manager)
        self.reset("original")

    def grant(self, allowed):
        (self.root / "config.yaml").write_text(
            json.dumps(
                {
                    "plugins": {
                        "entries": {"agent-mailbox-host": {"allow_gateway_injection": allowed}}
                    }
                }
            )
        )
        (self.root / "config.yaml").chmod(0o600)

    def reset(self, sid):
        now = datetime.datetime.now(datetime.UTC)
        self.entry = self.entry_type(
            session_key=self.key, session_id=sid, origin=self.source, created_at=now, updated_at=now
        )

    def request(self, **changes):
        return pinned.PinnedGateway(
            self.ctx,
            **(
                {
                    "session_key": self.key,
                    "session_id": "original",
                    "platform": "feishu",
                    "still_authorized": lambda: self.bound,
                }
                | changes
            ),
        )

    async def test_private_pin_requires_complete_new_host_and_cannot_send_to_original_chat(self):
        try:
            modules = pinned.compatible_sdk(private_mode=True)
        except ValueError:
            self.skipTest("Requires the isolated private-route host generation")
        self.assertEqual(set(modules), set(pinned.PRIVATE_SUPPORTED))
        with self.assertRaisesRegex(ValueError, "unsupported_hermes_dispatch_version"):
            pinned.compatible_sdk()
        request = self.request(private_mode=True)
        self.assertTrue(request.current())
        self.assertFalse(request.schedule("synthetic private notice"))
        self.assertFalse(await request.dispatch("synthetic private notice"))
        self.assertEqual(self.received, [])

    async def test_original_admitted_and_strict_resolved(self):
        self.assertTrue(await self.request().dispatch("synthetic notification"))
        self.assertEqual(self.resolved, ["original"])
        event = self.received[0]
        self.assertEqual(event.metadata["gateway_session_id"], "original")
        self.assertIs(event.metadata["gateway_session_strict"], True)
        self.assertFalse(event.allow_gateway_control)
        self.assertGreaterEqual(len(self.auth_calls), 2)
        self.assertTrue(all(value is False for value in self.auth_calls))

    async def test_current_host_durable_admission_keeps_exact_pin(self):
        """A current Gateway authority must not bypass the pinned facade."""
        from unittest.mock import patch

        import gateway.session_authorities as authorities

        class Authority:
            def __init__(self):
                self.events = []

            async def admit_automation(self, adapter, event, identity):
                self.events.append((adapter, event, identity))
                return True

        authority = Authority()
        with patch.object(authorities, "active_authority", return_value=authority):
            request = self.request()
            self.assertTrue(await request.dispatch("synthetic notification"))
            self.assertEqual(len(authority.events), 1)
            self.assertIs(authority.events[0][0], self.adapter)
            self.assertEqual(authority.events[0][1].metadata["gateway_session_id"], "original")
            self.assertEqual(self.received, [])  # queued is not a turn receipt
            self.reset("new-conversation")
            self.assertFalse(await self.request().dispatch("synthetic stale notice"))
            self.assertEqual(len(authority.events), 1)

    async def test_current_authority_requires_recovered_native_session(self):
        """A legacy transcript pin alone cannot admit automation after Gateway restart."""
        from unittest.mock import patch

        import gateway.session_authorities as authorities

        authority = SimpleNamespace(sessions={})
        with patch.object(authorities, "authority_for_home", return_value=authority):
            self.assertFalse(self.request().current())
            authority.sessions["original"] = SimpleNamespace(route=self.key)
            self.assertTrue(self.request().current())

    async def test_current_host_refuses_another_plugin_profile(self):
        from unittest.mock import patch

        import gateway.session_authorities as authorities

        class Authority:
            def __init__(self):
                self.calls = 0

            async def admit_automation(self, adapter, event, identity):
                self.calls += 1
                return True

        authority = Authority()
        other = self.root / "other"
        other.mkdir(mode=0o700)
        (other / "config.yaml").write_text(
            json.dumps(
                {"plugins": {"entries": {"agent-mailbox-host": {"allow_gateway_injection": True}}}}
            )
        )
        (other / "config.yaml").chmod(0o600)
        self.manager.home_path = other
        request = self.request()
        self.assertFalse(request.current())  # refuse before Receiver journals a dispatch
        with patch.object(authorities, "active_authority", return_value=authority):
            self.assertFalse(await request.dispatch("synthetic cross-profile notice"))
        self.assertEqual(authority.calls, 0)
        self.assertEqual(self.received, [])

    async def test_current_host_durable_admission_rechecks_revoked_route(self):
        from unittest.mock import patch

        import gateway.session_authorities as authorities

        class Authority:
            def __init__(self):
                self.events = []

            async def admit_automation(self, adapter, event, identity):
                self.events.append(event)
                return True

        authority = Authority()

        async def revoke():
            self.gateway_allowed = False

        self.before_admission = revoke
        with patch.object(authorities, "active_authority", return_value=authority):
            self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(authority.events, [])

    async def test_current_host_authority_refusal_does_not_fall_back_to_adapter(self):
        from unittest.mock import patch

        import gateway.session_authorities as authorities
        from hermes_state_runtime import RuntimeStoreError

        class Authority:
            async def admit_automation(self, adapter, event, identity):
                raise RuntimeStoreError("not_found")

        with patch.object(authorities, "active_authority", return_value=Authority()):
            self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_old_pin_after_reset_never_reaches_adapter(self):
        self.reset("new")
        request = self.request()
        self.assertFalse(request.current())
        self.assertFalse(await request.dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_reset_during_lookup_never_reaches_adapter(self):
        async def reset():
            await asyncio.sleep(0)
            self.reset("new")

        self.before_lookup = reset
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_mutable_entry_reset_during_scope_refused(self):
        async def mutate():
            self.entry.session_id = "new"

        self.before_admission = mutate
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_reset_after_capture_before_turn_dropped_by_real_resolver(self):
        async def reset():
            self.reset("new")

        self.before_resolution = reset
        self.assertTrue(await self.request().dispatch("synthetic"))
        self.assertEqual(len(self.received), 1)  # queued is not turn admission
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.received[0].metadata["gateway_session_id"], "original")

    async def test_revoked_binding_at_last_admission(self):
        async def revoke():
            self.bound = False

        self.before_admission = revoke
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_revoked_profile_grant_at_last_admission(self):
        async def revoke():
            self.grant(False)

        self.before_admission = revoke
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_gateway_route_revocation_rechecked(self):
        async def revoke():
            self.gateway_allowed = False

        self.before_admission = revoke
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_registration_rotation_rejects_old_request(self):
        request = self.request()
        self.manager.set_gateway_message_injector(
            self.owner, self.owner._schedule_plugin_message_injection
        )
        self.assertFalse(await request.dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_platform_mismatch(self):
        self.assertFalse(await self.request(platform="telegram").dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_draining_and_missing_entry(self):
        self.owner._draining = True
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.owner._draining = False
        self.entry = None
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_cli_or_tui_never_used_as_fallback(self):
        self.manager._cli_ref = object()
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.manager._cli_ref = None
        self.manager._tui_message_injector = (object(), lambda **kw: True)
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_single_request_does_not_reinject(self):
        request = self.request()
        self.assertTrue(await request.dispatch("synthetic"))
        self.assertFalse(await request.dispatch("synthetic"))
        self.assertEqual(len(self.received), 1)

    async def test_schedule_uses_official_supervision(self):
        self.assertTrue(self.request().schedule("synthetic"))
        self.assertEqual(self.received, [])
        for _ in range(8):
            await asyncio.sleep(0.01)
            if self.resolved:
                break
        self.assertEqual(self.resolved, ["original"])

    async def test_unload_before_callback_does_not_schedule(self):
        self.assertTrue(self.request().schedule("synthetic"))
        self.bound = False
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])

    async def test_unknown_method_rejected_without_dispatch(self):
        self.owner._hmwa_resolve_session = lambda *_: None
        with self.assertRaisesRegex(ValueError, "unsupported_hermes_dispatch_binding"):
            self.request()

    async def test_unknown_sdk_hash_rejected(self):
        from unittest.mock import patch

        with (
            patch.dict(pinned.SUPPORTED, {"gateway.run_turn": "0" * 64}),
            patch.dict(pinned.AUTHORITY_SUPPORTED, {"gateway.run_turn": "0" * 64}),
            self.assertRaisesRegex(ValueError, "unsupported_hermes_dispatch_version"),
        ):
            self.request()

    async def test_route_changed_during_scope_refused(self):
        async def reroute():
            self.owner._session_key_for_source = lambda source: "another-key"

        self.before_admission = reroute
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_resolver_changed_during_scope_refused(self):
        async def replace():
            self.owner._hmwa_resolve_session = lambda *_: None

        self.before_admission = replace
        self.assertFalse(await self.request().dispatch("synthetic"))
        self.assertEqual(self.received, [])

    async def test_real_busy_fifo_preserves_pin_then_reset_is_dropped(self):
        from types import SimpleNamespace

        from gateway.run_busy import GatewayBusySessionMixin

        owner = self.owner
        state = SimpleNamespace(conversation=SimpleNamespace(queued_events=[]))

        class Busy(GatewayBusySessionMixin):
            _BUSY_QUEUE_MAX_PENDING = 32  # official GatewayRunner default

            def _delivery_adapter_for(self, source):
                return adapter

            def _peek_session_state(self, key):
                return state

            def _session_state(self, key):
                return state

        queue = Busy()
        adapter = SimpleNamespace(_pending_messages={})
        self.assertTrue(await self.request().dispatch("synthetic first"))
        first = self.received[-1]
        self.assertTrue(await self.request().dispatch("synthetic second"))
        second = self.received[-1]
        queue._queue_or_replace_pending_event(self.key, first)
        queue._queue_or_replace_pending_event(self.key, second)
        self.assertEqual(len(state.conversation.queued_events), 1)
        event = queue._promote_queued_event(self.key, adapter, None)
        self.assertIs(event, second)
        self.reset("new")
        self.assertIsNone(await owner._hmwa_resolve_session(event, event.source))
        self.assertEqual(event.metadata["gateway_session_id"], "original")

    async def test_official_lifecycle_handle_cancels_inflight_lookup(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def hold():
            started.set()
            await release.wait()

        self.before_lookup = hold
        self.assertTrue(self.request().schedule("synthetic"))
        await asyncio.wait_for(started.wait(), 1)
        handles = list(self.manager._registration_order)
        self.assertEqual(len(handles), 1)
        handles[0].dispose()
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])

    async def test_cross_thread_scheduling_reaches_original(self):
        self.assertTrue(await asyncio.to_thread(self.request().schedule, "synthetic"))
        for _ in range(10):
            if self.resolved:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.resolved, ["original"])

    async def test_cross_thread_queued_before_unload_is_refused(self):
        request = self.request()
        results = []
        worker = threading.Thread(target=lambda: results.append(request.schedule("synthetic")))
        worker.start()
        # Hold the owner loop until a real foreign thread has queued its callback.
        worker.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])
        self.bound = False
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])

    async def test_two_queued_callbacks_cannot_duplicate_one_request(self):
        request = self.request()
        self.assertTrue(request.schedule("synthetic"))
        self.assertTrue(request.schedule("synthetic"))
        for _ in range(10):
            await asyncio.sleep(0.01)
            if self.resolved:
                break
        self.assertEqual(self.resolved, ["original"])
        self.assertEqual(len(self.received), 1)

    def receiver(self):
        spec = importlib.util.spec_from_file_location(
            "receiver_integration", SOURCE.with_name("__init__.py")
        )
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        home = self.root / "mailbox"
        (home / "wake").mkdir(parents=True, mode=0o700)
        binding = {
            "id": "binding_test",
            "employee_id": "employee_test",
            "project_id": "project_test",
            "host_kind": "hermes",
            "host_session_id": self.key,
        }
        binding_file = home / "wake/host-bindings.json"
        binding_file.write_text(json.dumps({"version": 1, "bindings": [binding]}))
        receiver = plugin.Receiver(
            self.ctx,
            home,
            self.root / "receiver-state",
            {self.key: {"session_id": "original", "platform": "feishu"}},
        )
        self.addCleanup(receiver.close)
        envelope = {
            "receipt_protocol": plugin.PROTOCOL,
            "employee_id": "employee_test",
            "delivery_id": "wake-integration",
            "binding": binding,
            "fresh": ["message_test"],
        }
        return receiver, envelope, binding_file

    async def test_receiver_uses_real_pinned_dispatch_and_requires_turn_observation(self):
        receiver, envelope, _ = self.receiver()
        public_calls = []
        self.ctx.inject_message = lambda *a, **k: public_calls.append((a, k))
        first = await asyncio.to_thread(receiver.receive, envelope, 0.05)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(self.resolved, ["original"])
        self.assertEqual(public_calls, [])
        event = self.received[0]
        # Simulated pre_llm hook only after official strict resolve. No model is run.
        receiver.observe_turn(session_id="original", platform="feishu", user_message=event.text)
        again = receiver.receive(envelope, 0)
        self.assertEqual(again["status"], "received")
        self.assertEqual(len(self.received), 1)

    async def test_one_hermes_session_keeps_two_project_deliveries_separate(self):
        receiver, first, binding_file = self.receiver()
        second_binding = {
            **first["binding"],
            "id": "binding_second",
            "project_id": "project_second",
        }
        binding_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "bindings": [
                        first["binding"],
                        second_binding,
                    ],
                }
            )
        )
        second = {
            **first,
            "binding": second_binding,
            "delivery_id": "wake-second",
            "fresh": ["message_second"],
        }

        for envelope in (first, second):
            result = await asyncio.to_thread(receiver.receive, envelope, 0.05)
            self.assertEqual(result["status"], "unknown")
            event = self.received[-1]
            self.assertIn(envelope["binding"]["project_id"], event.text)
            self.assertIn(envelope["fresh"][0], event.text)
            other = second if envelope is first else first
            self.assertNotIn(other["fresh"][0], event.text)
            receiver.observe_turn(session_id="original", platform="feishu", user_message=event.text)
            self.assertEqual(receiver.delivery_status(envelope)["status"], "received")

        self.assertEqual(self.resolved, ["original", "original"])
        self.assertEqual(len(self.received), 2)

        binding_file.write_text(json.dumps({"version": 1, "bindings": [second_binding]}))
        revoked = {**first, "delivery_id": "wake-revoked", "fresh": ["message_revoked"]}
        self.assertEqual(receiver.receive(revoked, 0)["status"], "not_received")
        self.assertEqual(len(self.received), 2)
        remaining = {**second, "delivery_id": "wake-remaining", "fresh": ["message_remaining"]}
        result = await asyncio.to_thread(receiver.receive, remaining, 0.05)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("message_remaining", self.received[-1].text)
        receiver.observe_turn(
            session_id="original", platform="feishu", user_message=self.received[-1].text
        )
        self.assertEqual(receiver.delivery_status(remaining)["status"], "received")
        self.assertEqual(len(self.received), 3)

        receiver.close()
        reopened = type(receiver)(
            self.ctx, receiver.home, self.root / "receiver-state", receiver.sessions
        )
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.delivery_status(remaining)["status"], "received")
        self.assertEqual(len(self.received), 3)  # no replay of a durable receipt
        after_restart = {
            **second,
            "delivery_id": "wake-after-restart",
            "fresh": ["message_after_restart"],
        }
        result = await asyncio.to_thread(reopened.receive, after_restart, 0.05)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("message_after_restart", self.received[-1].text)
        reopened.observe_turn(
            session_id="original", platform="feishu", user_message=self.received[-1].text
        )
        self.assertEqual(reopened.delivery_status(after_restart)["status"], "received")
        self.assertEqual(len(self.received), 4)

    async def test_unknown_in_one_project_does_not_reinject_or_block_another(self):
        receiver, first, binding_file = self.receiver()
        second_binding = {
            **first["binding"],
            "id": "binding_second",
            "project_id": "project_second",
        }
        binding_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "bindings": [
                        first["binding"],
                        second_binding,
                    ],
                }
            )
        )
        self.assertEqual(
            (await asyncio.to_thread(receiver.receive, first, 0.05))["status"], "unknown"
        )
        self.assertEqual(len(self.received), 1)

        receiver.close()
        reopened = type(receiver)(
            self.ctx, receiver.home, self.root / "receiver-state", receiver.sessions
        )
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.receive(first, 0)["status"], "unknown")
        self.assertEqual(len(self.received), 1)  # ambiguous original is never replayed

        second = {
            **first,
            "binding": second_binding,
            "delivery_id": "wake-second",
            "fresh": ["message_second"],
        }
        self.assertEqual(
            (await asyncio.to_thread(reopened.receive, second, 0.05))["status"], "unknown"
        )
        self.assertEqual(len(self.received), 2)
        self.assertIn("message_second", self.received[-1].text)
        reopened.observe_turn(
            session_id="original", platform="feishu", user_message=self.received[-1].text
        )
        self.assertEqual(reopened.delivery_status(second)["status"], "received")
        self.assertEqual(reopened.delivery_status(first)["status"], "unknown")

    async def test_receiver_reset_before_dispatch_never_reaches_new_conversation(self):
        receiver, envelope, _ = self.receiver()
        self.assertTrue(receiver.probe(envelope["binding"])["route_available"])
        self.reset("new-conversation")
        self.assertFalse(receiver.probe(envelope["binding"])["route_available"])
        result = await asyncio.to_thread(receiver.receive, envelope, 0.05)
        # The exact pin is already stale before scheduling, so no host effect
        # is possible and no dispatching journal row should be created.
        self.assertEqual(result["status"], "not_received")

    async def test_receiver_does_not_claim_route_before_native_session_recovery(self):
        from unittest.mock import patch

        import gateway.session_authorities as authorities

        receiver, envelope, _ = self.receiver()
        authority = SimpleNamespace(sessions={})
        with patch.object(authorities, "authority_for_home", return_value=authority):
            self.assertFalse(receiver.probe(envelope["binding"])["route_available"])
            result = await asyncio.to_thread(receiver.receive, envelope, 0.05)
            self.assertEqual(result["status"], "not_received")
            authority.sessions["original"] = SimpleNamespace(route=self.key)
            self.assertTrue(receiver.probe(envelope["binding"])["route_available"])
        self.assertEqual(receiver.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.received, [])
        self.assertEqual(self.resolved, [])

    async def test_receiver_with_injection_grant_paused_creates_no_dispatch_journal(self):
        receiver, envelope, _ = self.receiver()
        self.grant(False)
        result = await asyncio.to_thread(receiver.receive, envelope, 0.05)
        self.assertEqual(result["status"], "not_received")
        self.assertEqual(receiver.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.received, [])

    async def test_reauthorization_candidate_is_read_only_and_binding_scoped(self):
        receiver, envelope, binding_file = self.receiver()
        binding = envelope["binding"]
        with self.assertRaisesRegex(ValueError, "candidate_unavailable"):
            receiver.reauthorization_candidate(binding)
        self.reset("new-conversation")
        first = receiver.reauthorization_candidate(binding)
        self.assertEqual(first["protocol"], "agent-mailbox.host-reauthorization.v1")
        self.assertEqual(first["binding_id"], binding["id"])
        self.assertEqual(first["platform"], "feishu")
        self.assertFalse(first["route_available"])
        self.assertFalse(first["session_verified"])
        self.assertFalse(first["delivery_verified"])
        self.assertNotIn("new-conversation", str(first))
        resolved = receiver.reauthorization_candidate(
            binding, expected_fingerprint=first["candidate_fingerprint"]
        )
        self.assertEqual(resolved["candidate_session_id"], "new-conversation")
        with self.assertRaisesRegex(ValueError, "candidate_changed"):
            receiver.reauthorization_candidate(binding, expected_fingerprint="0" * 64)
        self.assertEqual(receiver.sessions[self.key]["session_id"], "original")
        self.assertEqual(receiver.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.received, [])
        self.reset("another-conversation")
        self.assertNotEqual(
            receiver.reauthorization_candidate(binding)["candidate_fingerprint"],
            first["candidate_fingerprint"],
        )
        with self.assertRaisesRegex(ValueError, "candidate_changed"):
            receiver.reauthorization_candidate(
                binding, expected_fingerprint=first["candidate_fingerprint"]
            )
        binding_file.write_text(json.dumps({"version": 1, "bindings": []}))
        with self.assertRaisesRegex(ValueError, "route_unavailable"):
            receiver.reauthorization_candidate(binding)

    async def test_receiver_binding_revoked_during_lookup_stops_admission(self):
        receiver, envelope, binding_file = self.receiver()

        async def revoke():
            binding_file.write_text(json.dumps({"version": 1, "bindings": []}))

        self.before_lookup = revoke
        result = await asyncio.to_thread(receiver.receive, envelope, 0.05)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.received, [])

    async def test_receiver_closed_after_enqueue_prevents_late_dispatch(self):
        receiver, envelope, _ = self.receiver()
        results = []
        worker = threading.Thread(target=lambda: results.append(receiver.receive(envelope, 0)))
        worker.start()
        worker.join(2)  # owner event loop deliberately held until callback is enqueued
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "unknown")
        receiver.close()
        await asyncio.sleep(0.02)
        self.assertEqual(self.received, [])

    async def test_receiver_wrong_observed_id_never_creates_receipt(self):
        receiver, envelope, _ = self.receiver()
        await asyncio.to_thread(receiver.receive, envelope, 0.05)
        receiver.observe_turn(
            session_id="other", platform="feishu", user_message=self.received[0].text
        )
        self.assertEqual(receiver.receive(envelope, 0)["status"], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
