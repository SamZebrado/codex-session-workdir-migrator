import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from codex_workdir_migrate import CodexSessionMigrator


class ImportPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "synthetic_home"
        self.home.mkdir()
        self.bundle = self.root / "bundle.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def bundle_for(self, session_id="source-id", member="sessions/raw_jsonl/source-id.jsonl"):
        with zipfile.ZipFile(self.bundle, "w") as zf:
            zf.writestr("MANIFEST.json", json.dumps({"session_id": session_id}))
            zf.writestr(member, json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n")

    def imported(self, **kwargs):
        return CodexSessionMigrator(str(self.home)).import_bundle(
            str(self.bundle), [], str(self.root / "backups"), **kwargs
        )

    def test_unsafe_explicit_ids_rejected_without_side_effects(self):
        self.bundle_for()
        invalid = [str(self.root / "escaped"), "../escaped", "a/b", "a\\b", "C:\\escaped",
                   "", ".", "..", "has space", "bad\nname", "bad\x00name", 123]
        for session_id in invalid:
            for dry_run in [True, False]:
                with self.subTest(session_id=session_id, dry_run=dry_run):
                    result = self.imported(new_session_id=session_id, dry_run=dry_run)
                    self.assertFalse(result["success"])
                    self.assertIn("Invalid new session ID", result["errors"][0])
                    self.assertEqual(list(self.home.iterdir()), [])
                    self.assertFalse((self.root / "backups").exists())
                    self.assertFalse((self.root / "escaped.jsonl").exists())

    def test_valid_uuid_and_non_uuid_ids_remain_contained(self):
        self.bundle_for()
        for session_id in ["explicit-new-session-456", "safe.session_456", "01234567-89ab-cdef-0123-456789abcdef"]:
            with self.subTest(session_id=session_id):
                result = self.imported(new_session_id=session_id, dry_run=False)
                self.assertTrue(result["success"], result["errors"])
                target = Path(result["imported_files"][0]["target"])
                self.assertTrue(target.resolve().is_relative_to(self.home.resolve()))
                self.assertEqual(target.name, session_id + ".jsonl")
                record = json.loads(target.read_text())
                self.assertEqual(record["payload"]["id"], session_id)

    def test_member_traversal_is_reduced_to_contained_basename(self):
        self.bundle_for(member="sessions/raw_jsonl/../../source-id.jsonl")
        result = self.imported(new_session_id="safe-id", dry_run=False)
        self.assertTrue(result["success"], result["errors"])
        target = Path(result["imported_files"][0]["target"])
        self.assertTrue(target.resolve().is_relative_to(self.home.resolve()))
        self.assertEqual(target.name, "safe-id.jsonl")

    def test_unsafe_member_basename_rejected_before_writes(self):
        self.bundle_for(member="sessions/raw_jsonl/..\\source-id.jsonl")
        result = self.imported(dry_run=False)
        self.assertFalse(result["success"])
        self.assertIn("Unsafe bundle JSONL destination.", result["errors"])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_symlinked_sessions_destination_rejected(self):
        self.bundle_for()
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / "sessions").symlink_to(outside, target_is_directory=True)
        result = self.imported(dry_run=False)
        self.assertFalse(result["success"])
        self.assertIn("Unsafe bundle JSONL destination.", result["errors"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_manifest_id_rejected_by_plan_and_import(self):
        for session_id in [None, "", "../source", "a\\b"]:
            with self.subTest(session_id=session_id):
                self.bundle_for(session_id=session_id)
                migrator = CodexSessionMigrator(str(self.home))
                plan = migrator.import_plan(str(self.bundle), [])
                result = self.imported(dry_run=False)
                for response in [plan, result]:
                    self.assertFalse(response["success"])
                    self.assertIn("Invalid bundle session ID", response["errors"][0])
                self.assertEqual(list(self.home.iterdir()), [])

    def test_temporary_destination_symlink_rejected_without_overwrite(self):
        self.bundle_for()
        target_dir = self.home / "sessions" / datetime.now().strftime("%Y/%m/%d")
        target_dir.mkdir(parents=True)
        sentinel = self.root / "outside-sentinel"
        sentinel.write_text("unchanged")
        (target_dir / "safe-id.jsonl.tmp").symlink_to(sentinel)
        result = self.imported(new_session_id="safe-id", dry_run=False)
        self.assertFalse(result["success"])
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse((target_dir / "safe-id.jsonl").exists())

    def test_all_member_destinations_checked_before_any_write(self):
        self.bundle_for()
        with zipfile.ZipFile(self.bundle, "a") as zf:
            zf.writestr("sessions/raw_jsonl/unsafe\\source-id.jsonl", "{}\n")
        result = self.imported(dry_run=False)
        self.assertFalse(result["success"])
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse((self.root / "backups").exists())

    def test_source_ids_overlapping_extension_preserve_jsonl_suffix(self):
        for source_id in ["jsonl", "l"]:
            with self.subTest(source_id=source_id):
                self.bundle_for(session_id=source_id, member=f"sessions/raw_jsonl/rollout-{source_id}.jsonl")
                target_id = "target-" + source_id
                result = self.imported(new_session_id=target_id, dry_run=False)
                self.assertTrue(result["success"], result["errors"])
                target = Path(result["imported_files"][0]["target"])
                self.assertEqual(target.name, f"rollout-{target_id}.jsonl")
                self.assertTrue(target.resolve().is_relative_to(self.home.resolve()))
                self.assertEqual(json.loads(target.read_text())["payload"]["id"], target_id)

    def test_only_terminal_id_in_filename_stem_is_rewritten(self):
        for source_id, stem in [
            ("l", "rollout-l-label-l"),
            ("jsonl", "jsonl-prefix-jsonl"),
            ("roll", "rollout-roll-roll"),
            ("2026", "rollout-2026-10-02-2026"),
        ]:
            with self.subTest(source_id=source_id):
                self.bundle_for(session_id=source_id, member=f"sessions/raw_jsonl/{stem}.jsonl")
                target_id = "new-" + source_id
                result = self.imported(new_session_id=target_id, dry_run=False)
                self.assertTrue(result["success"], result["errors"])
                target = Path(result["imported_files"][0]["target"])
                self.assertEqual(target.name, stem[:-len(source_id)] + target_id + ".jsonl")

    def test_source_id_inside_unrelated_filename_prefix_is_not_rewritten(self):
        self.bundle_for(session_id="l", member="sessions/raw_jsonl/rollout-unrelated.jsonl")
        result = self.imported(new_session_id="target-id", dry_run=False)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(Path(result["imported_files"][0]["target"]).name, "rollout-unrelated.jsonl")
