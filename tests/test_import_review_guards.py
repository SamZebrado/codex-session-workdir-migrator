"""Regressions for independently observed destination and enumeration guards."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from codex_workdir_migrate import CodexSessionMigrator

SID = "synthetic-review-source"


class ImportReviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="migrator-review-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.m = CodexSessionMigrator(str(self.home))
        self.bundle = self.root / "synthetic.zip"
        self.backups = self.root / "backups"
        self.metadata = json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n"

    def make_bundle(self, filename):
        with zipfile.ZipFile(self.bundle, "w") as z:
            z.writestr("MANIFEST.json", json.dumps({"session_id": SID}))
            z.writestr("sessions/raw_jsonl/" + filename, self.metadata)

    def snapshot(self):
        # Includes linked source bytes and the link itself without relying on
        # rglob following directory symlinks.
        files = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        links = {str(p.relative_to(self.root)): str(p.readlink())
                 for p in self.root.rglob("*") if p.is_symlink()}
        return files, links

    def assert_import_blocked(self, before, **options):
        for dry_run in [True, False]:
            with self.subTest(dry_run=dry_run), patch("codex_workdir_migrate.shutil.copy2") as copy, patch("codex_workdir_migrate.os.unlink") as unlink, patch("codex_workdir_migrate.os.replace") as replace:
                result = self.m.import_bundle(str(self.bundle), [], str(self.backups), dry_run=dry_run, **options)
                self.assertFalse(result["success"], result)
                self.assertTrue(result["errors"], result)
                self.assertEqual([], result["imported_files"])
                self.assertIsNone(result["backup"])
                copy.assert_not_called()
                unlink.assert_not_called()
                replace.assert_not_called()
            self.assertEqual(before, self.snapshot())
            self.assertFalse(self.backups.exists())

    def test_irregular_source_collision_blocks_auto_and_explicit_clone(self):
        self.make_bundle("irregular.jsonl")
        source = self.home / "sessions" / datetime.now().strftime("%Y/%m/%d") / "irregular.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(self.metadata)
        before = self.snapshot()
        for no_backup in [False, True]:
            for options in [{"on_conflict": "import-as-new"},
                            {"on_conflict": "import-as-new", "new_session_id": "explicit-clone"},
                            {"on_conflict": "import-as-new", "mode": "overwrite"},
                            {"on_conflict": "overwrite", "new_session_id": "explicit-clone"}]:
                with self.subTest(no_backup=no_backup, options=options):
                    self.assert_import_blocked(before, no_backup=no_backup, **options)
        self.assertEqual(self.metadata.encode(), source.read_bytes())

    def test_irregular_same_target_explicit_overwrite_remains_supported(self):
        self.make_bundle("irregular.jsonl")
        source = self.home / "sessions" / datetime.now().strftime("%Y/%m/%d") / "irregular.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(self.metadata)
        for options in [{"mode": "overwrite"}, {"on_conflict": "overwrite"}]:
            with self.subTest(options=options):
                result = self.m.import_bundle(str(self.bundle), [], None, dry_run=False, no_backup=True, **options)
                self.assertTrue(result["success"], result)
                self.assertEqual(SID, json.loads(source.read_text())["payload"]["id"])

    def test_nested_active_and_archived_directory_links_block_as_unknown(self):
        self.make_bundle(SID + ".jsonl")
        for tree in ["sessions", "archived_sessions"]:
            for dangling in [False, True]:
                with self.subTest(tree=tree, dangling=dangling):
                    parent = self.home / tree / "nested"
                    parent.mkdir(parents=True, exist_ok=True)
                    held = self.home / f"held-{tree}"
                    if not dangling:
                        held.mkdir(exist_ok=True)
                        (held / "irregular.jsonl").write_text(self.metadata)
                    target = self.home / "absent-directory" if dangling else held
                    link = parent / "linked-directory"
                    link.symlink_to(target, target_is_directory=True)
                    before = self.snapshot()
                    plan = self.m.import_plan(str(self.bundle), [])
                    self.assertFalse(plan["success"], plan)
                    self.assertEqual("unknown", plan["target_status"]["status"])
                    self.assertIsNone(plan["session_exists_on_target"])
                    self.assertIn("directory link", " ".join(plan["errors"]))
                    for options in [{}, {"mode": "overwrite"}, {"on_conflict": "import-as-new"}]:
                        self.assert_import_blocked(before, **options)
                    link.unlink()

    def test_directory_link_with_jsonl_suffix_is_still_blocked(self):
        self.make_bundle(SID + ".jsonl")
        held = self.home / "held"
        held.mkdir()
        (held / "existing.jsonl").write_text(self.metadata)
        (self.home / "sessions" / "nested.jsonl").symlink_to(held, target_is_directory=True)
        self.assert_import_blocked(self.snapshot())

    def occupied_source_bundle(self):
        self.make_bundle("different-path-" + SID + ".jsonl")
        archive = self.home / "archived_sessions"
        archive.mkdir()
        (archive / "irregular.jsonl").write_text(self.metadata)
        (self.home / "session_index.jsonl").write_text(json.dumps({"id": SID, "title": "existing"}) + "\n")
        db_path = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?)", (SID, "/existing"))
            db.commit()
        with zipfile.ZipFile(self.bundle, "a") as z:
            z.writestr("sqlite/state_db_matching_rows.json", json.dumps({"matching_rows": [{"id": SID, "cwd": "/imported"}]}))
            z.writestr("index/session_index_records.jsonl", json.dumps({"id": SID, "title": "imported"}) + "\n")
        return db_path

    def test_occupied_equal_id_clone_blocks_before_any_mutation(self):
        db_path = self.occupied_source_bundle()
        before = self.snapshot()
        for no_backup in [False, True]:
            for mode in ["skip", "overwrite"]:
                with self.subTest(no_backup=no_backup, mode=mode):
                    self.assert_import_blocked(before, new_session_id=SID,
                                               on_conflict="import-as-new", mode=mode,
                                               no_backup=no_backup)
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual("/existing", db.execute("SELECT cwd FROM threads WHERE id = ?", (SID,)).fetchone()[0])

    def test_distinct_clone_and_equal_id_authorized_overwrite_remain_supported(self):
        db_path = self.occupied_source_bundle()
        source = self.home / "archived_sessions" / "irregular.jsonl"
        original = source.read_bytes()
        clone = self.m.import_bundle(str(self.bundle), [], None, dry_run=False,
                                     no_backup=True, new_session_id="distinct-clone",
                                     on_conflict="import-as-new")
        self.assertTrue(clone["success"], clone)
        self.assertEqual(original, source.read_bytes())
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual("/existing", db.execute("SELECT cwd FROM threads WHERE id = ?", (SID,)).fetchone()[0])
            self.assertEqual("/imported", db.execute("SELECT cwd FROM threads WHERE id = ?", ("distinct-clone",)).fetchone()[0])
        overwritten = self.m.import_bundle(str(self.bundle), [], None, dry_run=False,
                                          no_backup=True, new_session_id=SID,
                                          on_conflict="overwrite")
        self.assertTrue(overwritten["success"], overwritten)
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual("/imported", db.execute("SELECT cwd FROM threads WHERE id = ?", (SID,)).fetchone()[0])
        index = [json.loads(line) for line in (self.home / "session_index.jsonl").read_text().splitlines()]
        self.assertEqual("imported", next(row for row in index if row["id"] == SID)["title"])
