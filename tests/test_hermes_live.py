"""Official SDK metadata APIs with a synthetic registered Gateway; no model/host boot."""

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host/live.py"
spec = importlib.util.spec_from_file_location("mailbox_live_test", SOURCE)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if importlib.util.find_spec("hermes_cli") is None or "gateway.run" not in sys.modules:
            self.skipTest(
                "Requires isolated Hermes SDK runner with preloaded Gateway; skip is not validation"
            )
        from gateway.config import GatewayConfig
        from gateway.run_plugin_injection import GatewayPluginInjectionMixin
        from gateway.session import SessionStore
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from hermes_constants import pin_process_hermes_home

        self.tmp = tempfile.TemporaryDirectory(prefix="mailbox-live-metadata-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        pin_process_hermes_home(self.root)
        self.addCleanup(pin_process_hermes_home, None)
        self.scope = self.root / "custom-session-location"
        self.scope.mkdir(mode=0o700)
        config = GatewayConfig()
        config.multiplex_profiles = False

        class Store(SessionStore):
            @property
            def _routing_db(self):
                raise AssertionError("metadata must not open/read a database")

        # Exercise real type/method contracts without invoking the DB-opening constructor.
        store = object.__new__(Store)
        store.config = config
        store.sessions_dir = self.scope
        store._routing_home = self.root

        class Gateway(GatewayPluginInjectionMixin):
            _running = True
            _draining = False
            _primary_profile_name = "default"

        self.owner = Gateway()
        self.owner.config = config
        self.owner.session_store = store
        self.owner._gateway_loop = asyncio.get_running_loop()
        self.manager = PluginManager(scope_key=str(self.root))
        self.manager.set_gateway_message_injector(
            self.owner, self.owner._schedule_plugin_message_injection
        )
        self.ctx = PluginContext(PluginManifest(name="agent-mailbox-host"), self.manager)
        self.reader = live.LiveRouting(self.ctx)

    async def test_live_store_scope_wins_over_disk_and_no_grant_needed(self):
        (self.root / "config.yaml").write_text(
            "gateway:\n  sessions_dir: /wrong\nsecret: PRIVATE\n"
        )
        before = (self.root / "config.yaml").read_bytes()
        result = self.reader.snapshot()
        self.assertEqual(result["scope"], str(self.scope))
        self.assertEqual(result["routing_home"], str(self.root))
        self.assertEqual(result["profile_id"], "default")
        self.assertEqual(result["gateway_profile"], "default")
        self.assertFalse(result["session_verified"])
        self.assertFalse(result["delivery_verified"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual((self.root / "config.yaml").read_bytes(), before)
        self.assertFalse((self.root / "state.db").exists())
        self.assertEqual(result["instance_id"], self.reader.snapshot()["instance_id"])

    async def test_external_grant_repair_is_visible_without_recreating_context(self):
        """Use the real SDK cache and external grant writer, never a model or Gateway."""
        import json
        import subprocess

        component = SOURCE.parent
        spec = importlib.util.spec_from_file_location(
            "live_grant_receiver", component / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        receiver = object.__new__(module.Receiver)
        receiver.ctx = self.ctx
        receiver.closed = False
        for name in ("mailbox", "state", "ipc"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "plugins/agent-mailbox-host").mkdir(parents=True, mode=0o700)
        config = {
            "plugins": {
                "entries": {
                    "agent-mailbox-host": {
                        "allow_gateway_injection": False,
                        "settings": {
                            "home": str(self.root / "mailbox"),
                            "state_dir": str(self.root / "state"),
                            "socket_path": str(self.root / "ipc/hermes.sock"),
                            "sessions": {
                                "synthetic-route": {
                                    "session_id": "synthetic-session",
                                    "platform": "feishu",
                                }
                            },
                        },
                    }
                }
            }
        }
        path = self.root / "config.yaml"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        original = self.ctx
        self.assertFalse(receiver.available())

        def invoke(operation, options):
            request = {
                "version": 1,
                "operation": operation,
                "profile": str(self.root),
                "options": options,
            }
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(component / "bridge.py")],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                cwd=self.root,
                env={
                    "HOME": str(self.root),
                    "HERMES_HOME": str(self.root),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "en_US.UTF-8",
                },
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, "isolated official grant writer failed")
            response = json.loads(result.stdout)
            self.assertIs(response["ok"], True)
            return response["result"]

        scope = {"mailbox_home": str(self.root / "mailbox"), "session_key": "synthetic-route"}
        preview = invoke("grant_preview", scope)
        applied = invoke(
            "grant_apply",
            {**scope, "confirmed": True, "expected_config_sha256": preview["config_sha256"]},
        )
        self.assertTrue(applied["enabled"])
        self.assertIs(receiver.ctx, original)
        self.assertTrue(receiver.available())
        # A subsequent external revoke is also seen by that same live object.
        config["plugins"]["entries"]["agent-mailbox-host"]["allow_gateway_injection"] = False
        path.write_text(json.dumps(config))
        self.assertFalse(receiver.available())
        self.assertIs(receiver.ctx, original)
        self.assertFalse((self.root / "ipc/hermes.sock").exists())

    async def test_no_registration_or_cli_or_tui_refuses(self):
        original = self.manager._gateway_message_injector
        self.manager._gateway_message_injector = None
        with self.assertRaises(ValueError):
            self.reader.snapshot()
        self.manager._gateway_message_injector = original
        self.manager._cli_ref = object()
        with self.assertRaises(ValueError):
            self.reader.snapshot()
        self.manager._cli_ref = None
        self.manager._tui_message_injector = (object(), lambda: None)
        with self.assertRaises(ValueError):
            self.reader.snapshot()

    async def test_stopped_draining_abandoned_refuse(self):
        for obj, key in [(self.owner, "_draining"), (self.ctx, "_load_abandoned")]:
            setattr(obj, key, True)
            with self.assertRaises(ValueError):
                self.reader.snapshot()
            setattr(obj, key, False)
        self.owner._running = False
        with self.assertRaises(ValueError):
            self.reader.snapshot()

    async def test_substituted_scheduler_or_scope_reader_refuses(self):
        self.manager._gateway_message_injector = (self.owner, lambda **kw: None)
        with self.assertRaises(ValueError):
            self.reader.snapshot()
        self.manager.set_gateway_message_injector(
            self.owner, self.owner._schedule_plugin_message_injection
        )
        self.owner.session_store._routing_scope = lambda: str(self.scope)
        with self.assertRaises(ValueError):
            self.reader.snapshot()

    async def test_foreign_profile_refuses_without_reading_its_config(self):
        other = self.root / "profiles" / "other"
        other.mkdir(parents=True, mode=0o700)
        self.manager.home_path = other
        with self.assertRaises(ValueError):
            self.reader.snapshot()

    async def test_multiplex_secondary_uses_actual_shared_routing_store(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        other = self.root / "profiles" / "research"
        other.mkdir(parents=True, mode=0o700)
        (other / "config.yaml").write_text("{}\n")
        (other / "config.yaml").chmod(0o600)
        self.owner.config.multiplex_profiles = True
        manager = PluginManager(scope_key=str(other))
        manager.set_gateway_message_injector(
            self.owner, self.owner._schedule_plugin_message_injection
        )
        ctx = PluginContext(PluginManifest(name="agent-mailbox-host"), manager)
        result = live.LiveRouting(ctx).snapshot()
        self.assertEqual(result["profile_id"], "research")
        self.assertEqual(result["profile_home"], str(other))
        self.assertEqual(result["routing_home"], str(self.root))
        self.assertEqual(result["scope"], str(self.scope))
        self.assertTrue(result["multiplex"])

    async def test_unknown_sdk_is_not_trusted(self):
        with (
            patch.dict(live.SUPPORTED, {"gateway.session": "0" * 64}),
            patch.dict(live.PRIVATE_SUPPORTED, {"gateway.session": "0" * 64}),
            self.assertRaisesRegex(ValueError, "unsupported_hermes_metadata_version"),
        ):
            self.reader.snapshot()

    async def test_missing_runtime_never_imports_or_bootstraps_it(self):
        module = sys.modules.pop("gateway.run")
        try:
            with self.assertRaisesRegex(ValueError, "hermes_runtime_not_loaded"):
                self.reader.snapshot()
            self.assertNotIn("gateway.run", sys.modules)
        finally:
            sys.modules["gateway.run"] = module

    async def test_wide_or_symlink_scope_refuses(self):
        self.scope.chmod(0o755)
        with self.assertRaises(ValueError):
            self.reader.snapshot()
        self.scope.chmod(0o700)
        link = self.root / "alias"
        link.symlink_to(self.scope, target_is_directory=True)
        # Official scope already resolves aliases; the returned scope must be canonical.
        self.owner.session_store.sessions_dir = link
        self.assertEqual(self.reader.snapshot()["scope"], str(self.scope))

    async def test_registration_replacement_changes_generation(self):
        first = self.reader.snapshot()
        self.manager.set_gateway_message_injector(
            self.owner, self.owner._schedule_plugin_message_injection
        )
        second = self.reader.snapshot()
        self.assertNotEqual(first["instance_id"], second["instance_id"])

    async def test_mismatched_store_configuration_refuses(self):
        from gateway.config import GatewayConfig

        self.owner.session_store.config = GatewayConfig()
        with self.assertRaises(ValueError):
            self.reader.snapshot()


if __name__ == "__main__":
    unittest.main()
