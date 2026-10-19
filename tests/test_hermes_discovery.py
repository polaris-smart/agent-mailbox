"""Run with the installed Hermes interpreter: real profile APIs and real schema.

python -I tests/test_hermes_discovery.py
All profiles, databases, WAL, and marker files are synthetic and removed.
"""

import contextlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host/discovery.py"
spec = importlib.util.spec_from_file_location("candidate_metadata", SOURCE)
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


class OfficialMetadataTest(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("hermes_state") is None:
            self.skipTest("Run with installed Hermes interpreter for official SDK/schema")
        self.temporary = tempfile.TemporaryDirectory(prefix="hermes-metadata-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        previous = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)
        self.addCleanup(
            lambda: (
                os.environ.pop("HERMES_HOME", None)
                if previous is None
                else os.environ.__setitem__("HERMES_HOME", previous)
            )
        )
        (self.root / "config.yaml").write_text("gateway:\n  multiplex_profiles: false\n")
        (self.root / "config.yaml").chmod(0o600)
        (self.root / "profiles").mkdir(mode=0o700)
        self.scope = self.root / "sessions"
        self.scope.mkdir(mode=0o700)
        # Official migrations build the isolated test schema, never our own DDL.
        from hermes_state import SessionDB

        self.db = SessionDB(db_path=self.root / "state.db")
        self.addCleanup(self.db.close)
        self.context = {
            "routing_home": self.root,
            "scope": self.scope,
            "gateway_profile": "default",
            "multiplex": False,
        }
        self.key = "agent:main:feishu:dm:synthetic"

    def add(self, *, key=None, sid="session-one", scope=None, platform="feishu", **extra):
        key = key or self.key
        entry = {
            "session_key": key,
            "session_id": sid,
            "platform": platform,
            "display_name": "Synthetic",
            "model_override": {"model": "PRIVATE-SENTINEL"},
            "prompt_pin": {"private": "PRIVATE-PROMPT"},
            **extra,
        }
        self.db.save_gateway_routing_entry(key, json.dumps(entry), scope=str(scope or self.scope))
        # Official DB may use a permissive process umask for coordination files;
        # our deployment boundary requires all synthetic files private.
        for path in self.root.glob("state.db*"):
            path.chmod(0o600)

    def rows(self, profile="default", **context):
        return metadata.candidates(self.root, profile, **(self.context | context))

    def test_actual_schema_and_private_projection(self):
        self.add()
        rows = self.rows()
        self.assertEqual(rows[0]["session_id"], "session-one")
        self.assertTrue(rows[0]["candidate_only"])
        self.assertFalse(rows[0]["session_verified"])
        self.assertNotIn("PRIVATE", json.dumps(rows))

    def test_committed_wal_reset_invalidates_old_pin(self):
        self.add()
        selected = self.rows()[0]
        with contextlib.closing(sqlite3.connect(self.root / "state.db")) as writer:
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.add(sid="reset-new")
        self.assertGreater((self.root / "state.db-wal").stat().st_size, 0)
        self.assertEqual(self.rows()[0]["session_id"], "reset-new")
        with self.assertRaisesRegex(ValueError, "candidate_changed"):
            metadata.recheck_candidate(self.root, "default", selected, **self.context)

    def named(self, name):
        path = self.root / "profiles" / name
        path.mkdir(mode=0o700)
        (path / "config.yaml").write_text("synthetic: true\n")
        (path / "config.yaml").chmod(0o600)
        return path

    def test_official_tombstone_and_ghost_shell_excluded(self):
        from hermes_constants import mark_named_profile_deleted

        path = self.named("deleted")
        mark_named_profile_deleted(path)
        (self.root / "profiles" / "empty-shell").mkdir(mode=0o700)
        self.named("live")
        self.assertEqual(
            {p["profile_id"] for p in metadata.profiles(self.root)}, {"default", "live"}
        )

    def test_symlink_profile_excluded(self):
        self.named("live")
        (self.root / "profiles" / "redirect").symlink_to(self.root / "profiles/live")
        self.assertNotIn("redirect", {p["profile_id"] for p in metadata.profiles(self.root)})

    def test_multiplex_reads_gateway_root_for_named_profile(self):
        self.named("worker")
        self.add(key="agent:worker:feishu:dm:synthetic", transport_profile="worker")
        self.add()
        rows = self.rows("worker", multiplex=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["session_key"].startswith("agent:worker:"))

    def test_multiplex_unknown_transport_not_adopted(self):
        self.named("worker")
        self.add(key="agent:worker:feishu:dm:synthetic")
        self.assertEqual(self.rows("worker", multiplex=True), [])

    def test_named_main_uses_official_namespace(self):
        self.named("main")
        self.add(key="agent:main~:feishu:dm:synthetic", transport_profile="main")
        self.assertEqual(len(self.rows("main", multiplex=True)), 1)

    def test_no_guessed_multiplex_context(self):
        self.add()
        with self.assertRaisesRegex(ValueError, "routing_context_unproven"):
            self.rows(multiplex=None)

    def test_wrong_routing_home_rejected(self):
        other = self.named("worker")
        with self.assertRaisesRegex(ValueError, "routing_home_mismatch"):
            self.rows(routing_home=other)

    def test_other_scope_not_selected(self):
        other = self.root / "other-scope"
        other.mkdir(mode=0o700)
        self.add(scope=other)
        self.assertEqual(self.rows(), [])

    def test_non_gateway_and_malformed_entries_excluded(self):
        self.add(key="agent:main:local:dm:synthetic", platform="local")
        self.add(key="agent:main:cli:dm:synthetic", platform="cli")
        self.add(sid="../bad")
        self.assertEqual(self.rows(), [])

    def test_hardlink_database_rejected(self):
        self.add()
        os.link(self.root / "state.db", self.root / "alias")
        with self.assertRaisesRegex(ValueError, "database_not_private"):
            self.rows()

    def test_sidecar_symlink_rejected(self):
        self.add()
        wal = self.root / "state.db-wal"
        wal.rename(self.root / "wal-original")
        wal.symlink_to(self.root / "wal-original")
        with self.assertRaisesRegex(ValueError, "database_not_private"):
            self.rows()
        wal.unlink()
        (self.root / "wal-original").rename(wal)

    def test_oversized_entry_not_returned(self):
        self.add(prompt_pin={"synthetic": "x" * 70000})
        self.assertEqual(self.rows(), [])

    def test_query_does_not_change_database_or_wal(self):
        self.add()
        before = {p: p.read_bytes() for p in (self.root / "state.db", self.root / "state.db-wal")}
        self.rows()
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main(verbosity=2)
