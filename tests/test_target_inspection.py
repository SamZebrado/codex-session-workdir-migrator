"""Import safety contracts using disposable synthetic homes and injected failures."""
import builtins
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from codex_workdir_migrate import CodexSessionMigrator

SID = "synthetic-workflow-01"


class TargetInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="migrator-target-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.m = CodexSessionMigrator(str(self.home))
        self.bundle = self.root / "synthetic.zip"
        self.make_bundle()

    def make_bundle(self, sqlite=False, cwd="/synthetic-source"):
        with zipfile.ZipFile(self.bundle, "w") as z:
            z.writestr("MANIFEST.json", json.dumps({"session_id": SID}))
            z.writestr(f"sessions/raw_jsonl/rollout-{SID}.jsonl", json.dumps({
                "type": "session_meta", "payload": {"id": SID, "cwd": cwd}}) + "\n")
            z.writestr("index/session_index_records.jsonl", json.dumps({"id": SID, "title": cwd}) + "\n")
            if sqlite:
                z.writestr("sqlite/state_db_matching_rows.json", json.dumps({
                    "matching_rows": [{"id": SID, "cwd": cwd}]}))

    def db(self, existing=False, schema="id TEXT PRIMARY KEY, cwd TEXT"):
        with sqlite3.connect(self.home / "state_5.sqlite") as db:
            db.execute(f"CREATE TABLE threads ({schema})")
            if existing:
                db.execute("INSERT INTO threads VALUES (?, ?)", (SID, "/existing"))

    def jsonl(self, sid=SID, name=None, archived=False):
        p = self.home / ("archived_sessions" if archived else "sessions") / (name or f"rollout-{sid}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
        return p

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob("*") if p.is_file()}

    def run_import(self, **kwargs):
        return self.m.import_bundle(str(self.bundle), [], str(self.root / "backups"),
                                    dry_run=False, **kwargs)

    def assert_blocked(self, before, result, error=None):
        self.assertFalse(result["success"], result)
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(result["backup"])
        self.assertEqual([], result["imported_files"])
        self.assertFalse((self.root / "backups").exists())
        if error:
            self.assertIn(error, " ".join(result["errors"]))

    def assert_inspection_blocks(self, error):
        before = self.snapshot()
        plan = self.m.import_plan(str(self.bundle), [])
        self.assertFalse(plan["success"], plan)
        self.assertEqual("unknown", plan["target_status"]["status"])
        self.assertIsNone(plan["session_exists_on_target"])
        for options in [{}, {"mode": "overwrite"}, {"on_conflict": "import-as-new"}]:
            self.assert_blocked(before, self.run_import(**options), error)

    def test_M1b_first_read_failure_never_reaches_later_writes(self):
        self.db(existing=True)
        self.make_bundle(sqlite=True)
        before = self.snapshot()
        connect = self.m._connect_state_db
        calls = []
        def fail_first(readonly=False):
            calls.append(readonly)
            if len(calls) == 1:
                raise sqlite3.OperationalError("synthetic transient read failure")
            return connect(readonly=readonly)
        with patch.object(self.m, "_connect_state_db", side_effect=fail_first):
            self.assert_blocked(before, self.run_import(), "transient read failure")
        self.assertEqual([True], calls)
        with sqlite3.connect(self.home / "state_5.sqlite") as db:
            self.assertEqual("/existing", db.execute("SELECT cwd FROM threads").fetchone()[0])

    def test_M1_corrupt_db_is_unknown_even_for_jsonl_bundle(self):
        (self.home / "state_5.sqlite").write_bytes(b"synthetic non-SQLite bytes")
        self.assert_inspection_blocks("target SQLite")

    def test_schema_mismatch_closes_read_handles(self):
        self.db(schema="wrong_id TEXT")
        connections = []
        connect = self.m._connect_state_db
        def track(readonly=False):
            conn = connect(readonly=readonly)
            connections.append(conn)
            return conn
        with patch.object(self.m, "_connect_state_db", side_effect=track):
            self.assert_inspection_blocks("no such column: id")
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_locked_db_blocks_before_backup(self):
        self.db()
        lock = sqlite3.connect(self.home / "state_5.sqlite")
        self.addCleanup(lock.close)
        lock.execute("BEGIN EXCLUSIVE")
        def fast(readonly=False):
            return sqlite3.connect(f"file:{self.home / 'state_5.sqlite'}?mode=ro", uri=True, timeout=0.01)
        with patch.object(self.m, "_connect_state_db", side_effect=fast):
            self.assert_inspection_blocks("locked")
        lock.rollback()

    def test_unreadable_db(self):
        self.db()
        with patch.object(self.m, "_connect_state_db", side_effect=PermissionError("synthetic unreadable DB")):
            self.assert_inspection_blocks("unreadable DB")

    def test_stat_read_failure_is_not_absence(self):
        real_stat = os.stat
        def denied(path, *args, **kwargs):
            if str(path) == self.m.state_db_path:
                raise PermissionError("synthetic stat denied")
            return real_stat(path, *args, **kwargs)
        with patch("codex_workdir_migrate.os.stat", side_effect=denied):
            self.assert_inspection_blocks("stat denied")

    def test_dangling_db_is_not_absence(self):
        (self.home / "state_5.sqlite").symlink_to(self.root / "missing-db")
        self.assert_inspection_blocks("dangling symlink")

    def test_M2_every_partial_authority_and_complete_target_conflict(self):
        for state in ["index", "db", "jsonl", "complete"]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                self.home = Path(td)
                self.m = CodexSessionMigrator(td)
                if state in ["db", "complete"]:
                    self.db(existing=True)
                if state in ["jsonl", "complete"]:
                    self.jsonl()
                if state in ["index", "complete"]:
                    (self.home / "session_index.jsonl").write_text(json.dumps({"id": SID, "title": "existing"}) + "\n")
                plan = self.m.import_plan(str(self.bundle), [])
                self.assertTrue(plan["success"], plan)
                self.assertTrue(plan["session_exists_on_target"])
                self.assertEqual("complete" if state == "complete" else "incomplete", plan["target_status"]["status"])
                before = self.snapshot()
                self.assert_blocked(before, self.run_import(), "already exists")

    def test_index_only_explicit_overwrite_and_clone(self):
        idx = self.home / "session_index.jsonl"
        idx.write_text(json.dumps({"id": SID, "title": "existing"}) + "\n")
        result = self.run_import(on_conflict="import-as-new")
        self.assertTrue(result["success"], result)
        rows = [json.loads(line) for line in idx.read_text().splitlines()]
        self.assertEqual("existing", rows[0]["title"])
        self.assertEqual(result["target_session_id"], rows[1]["id"])
        result = self.run_import(on_conflict="overwrite")
        self.assertTrue(result["success"], result)
        rows = [json.loads(line) for line in idx.read_text().splitlines()]
        self.assertEqual("/synthetic-source", next(row for row in rows if row["id"] == SID)["title"])

    def test_M3_malformed_middle_tail_shape_and_duplicate_index_preserved(self):
        valid = json.dumps({"id": "unrelated", "title": "preserve"}) + "\n"
        cases = [(valid + '{"id":"partial"', "line 2"),
                 (json.dumps({"id": SID}) + '\n' + '{"id":"partial"', "line 2"),
                 (valid + 'broken\n' + json.dumps({"id": SID}) + "\n", "line 2"),
                 ('[]\n', "expected object"), ('{"title":"missing id"}\n', "expected object"),
                 ('{"id":123}\n', "expected object"),
                 (valid + valid, "Ambiguous target index")]
        for contents, error in cases:
            with self.subTest(contents=contents):
                (self.home / "session_index.jsonl").write_text(contents)
                self.assert_inspection_blocks(error)

    def test_unreadable_index_blocks(self):
        idx = self.home / "session_index.jsonl"
        idx.write_text(json.dumps({"id": SID}) + "\n")
        real_open = builtins.open
        def denied(path, *args, **kwargs):
            if str(path) == str(idx):
                raise PermissionError("synthetic index unreadable")
            return real_open(path, *args, **kwargs)
        with patch("builtins.open", side_effect=denied):
            self.assert_inspection_blocks("index unreadable")

    def test_valid_index_blank_lines_and_unterminated_valid_row_preserved(self):
        idx = self.home / "session_index.jsonl"
        original = '\r\n  {"id":"unrelated","title":"keep spacing"}\r\n\n{"id":"last"}'
        idx.write_bytes(original.encode())
        result = self.run_import()
        self.assertTrue(result["success"], result)
        self.assertTrue(idx.read_bytes().startswith(original.encode() + b"\n"))
        self.assertEqual(SID, json.loads(idx.read_text().splitlines()[-1])["id"])

    def test_M5_overlapping_id_never_selects_or_deletes_unrelated(self):
        other = self.jsonl(SID + "-other")
        original = other.read_bytes()
        self.assertEqual([], self.m.find_session_file(SID))
        result = self.run_import(mode="overwrite")
        self.assertTrue(result["success"], result)
        self.assertEqual(original, other.read_bytes())

    def test_short_id_and_irregular_archived_filenames_use_metadata(self):
        for sid in ["l", "jsonl", SID]:
            with self.subTest(sid=sid):
                match = self.jsonl(sid, name=f"irregular-{sid}-name.jsonl", archived=True)
                other = self.jsonl(sid + "-other")
                self.assertEqual([str(match)], self.m.find_session_file(sid))
                self.assertNotIn(str(other), self.m.find_session_file(sid))

    def test_unrelated_payload_id_is_not_session_identity(self):
        other = self.jsonl(SID + "-other")
        with other.open("a") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"id": SID}}) + "\n")
        self.assertEqual([], self.m.find_session_file(SID))

    def test_malformed_missing_and_ambiguous_jsonl_block(self):
        p = self.home / "sessions" / f"rollout-{SID}.jsonl"
        meta = json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n"
        for content in ['broken\n', '{}\n', '[]\n', meta + '{"type":"partial"',
                        meta + json.dumps({"type": "session_meta", "payload": {"id": "other"}}) + "\n"]:
            with self.subTest(content=content):
                p.write_text(content)
                self.assert_inspection_blocks("Target inspection failed")

    def test_unreadable_jsonl_blocks(self):
        p = self.jsonl()
        real_open = builtins.open
        def denied(path, *args, **kwargs):
            if str(path) == str(p):
                raise PermissionError("synthetic JSONL unreadable")
            return real_open(path, *args, **kwargs)
        with patch("builtins.open", side_effect=denied):
            self.assert_inspection_blocks("JSONL unreadable")

    def test_directory_enumeration_failure_blocks(self):
        def failed_walk(path, onerror=None):
            onerror(PermissionError("synthetic directory unreadable"))
            return iter([])
        with patch("codex_workdir_migrate.os.walk", side_effect=failed_walk):
            self.assert_inspection_blocks("directory unreadable")

    def test_matching_filename_with_wrong_metadata_never_overwritten(self):
        p = self.home / "sessions" / datetime.now().strftime("%Y/%m/%d") / f"rollout-{SID}.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"type": "session_meta", "payload": {"id": "unrelated"}}) + "\n")
        self.assert_blocked(self.snapshot(), self.run_import(mode="overwrite"), "another session")

    def test_M4_automatic_repeats_clone_and_explicit_retry_conflicts(self):
        first = self.run_import(on_conflict="import-as-new", no_backup=True)
        second = self.run_import(on_conflict="import-as-new", no_backup=True)
        self.assertTrue(first["success"], first)
        self.assertTrue(second["success"], second)
        self.assertNotEqual(first["target_session_id"], second["target_session_id"])
        before = self.snapshot()
        self.assert_blocked(before, self.run_import(new_session_id=first["target_session_id"]), "already exists")

    def test_unchanged_and_changed_source_repeat_never_overwrite_by_default(self):
        self.db()
        self.make_bundle(sqlite=True)
        first = self.run_import(no_backup=True)
        self.assertTrue(first["success"], first)
        before = self.snapshot()
        self.assert_blocked(before, self.run_import(), "already exists")
        self.make_bundle(sqlite=True, cwd="/changed-remote-equivalent-source")
        self.assert_blocked(before, self.run_import(), "already exists")
        overwritten = self.run_import(mode="overwrite")
        self.assertTrue(overwritten["success"], overwritten)
        with sqlite3.connect(self.home / "state_5.sqlite") as db:
            self.assertEqual("/changed-remote-equivalent-source", db.execute("SELECT cwd FROM threads").fetchone()[0])

    def test_interrupted_import_partial_jsonl_blocks_retry(self):
        self.db()
        self.make_bundle(sqlite=True)
        connect = self.m._connect_state_db
        def interrupt(readonly=False):
            if not readonly:
                raise sqlite3.OperationalError("synthetic interruption after JSONL write")
            return connect(readonly=readonly)
        with patch.object(self.m, "_connect_state_db", side_effect=interrupt):
            first = self.run_import(no_backup=True)
        self.assertFalse(first["success"])
        self.assertTrue(first["imported_files"])
        self.assertEqual(1, len(list((self.home / "sessions").rglob("*.jsonl"))))
        before = self.snapshot()
        self.assert_blocked(before, self.run_import(), "already exists")
        self.assertEqual("incomplete", self.m.import_plan(str(self.bundle), [])["target_status"]["status"])

    def test_later_schema_read_failure_closes_handles_and_blocks_plan_and_import(self):
        self.db()
        self.make_bundle(sqlite=True)
        class BrokenCursor(sqlite3.Cursor):
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("synthetic later schema read failure")
        class BrokenConnection(sqlite3.Connection):
            def cursor(self):
                return super().cursor(factory=BrokenCursor)
        before = self.snapshot()
        for action in [lambda: self.m.import_plan(str(self.bundle), []), self.run_import]:
            connections = []
            def connect(readonly=False):
                factory = BrokenConnection if connections else sqlite3.Connection
                conn = sqlite3.connect(self.home / "state_5.sqlite", factory=factory)
                connections.append(conn)
                return conn
            with self.subTest(action=action), patch.object(self.m, "_connect_state_db", side_effect=connect):
                result = action()
            self.assertFalse(result["success"], result)
            self.assertIn("later schema read failure", " ".join(result["errors"]))
            self.assertEqual(before, self.snapshot())
            self.assertFalse((self.root / "backups").exists())
            if "target_status" in result:
                self.assertEqual("unknown", result["target_status"]["status"])
            else:
                self.assertEqual([], result["imported_files"])
            for conn in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")

    def test_inspect_reports_malformed_index_instead_of_hiding_tail(self):
        (self.home / "session_index.jsonl").write_text(json.dumps({"id": SID}) + '\n{"id":"partial"')
        inspected = self.m.inspect_session(SID)
        self.assertIn("line 2", inspected["session_index"]["error"])
