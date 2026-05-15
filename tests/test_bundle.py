import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from codex_workdir_migrate import CodexSessionMigrator, MigrationPolicy


SESSION_ID = "bundle-test-123"
OLD_CWD = "/old/workdir"
NEW_CWD = "/new/workdir"


def _write_jsonl(path: Path, with_cwd=True):
    records = [
        {"type": "session_meta", "payload": {"id": SESSION_ID, "cwd": OLD_CWD if with_cwd else NEW_CWD}},
        {"type": "turn_context", "payload": {"cwd": OLD_CWD if with_cwd else NEW_CWD}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"<environment_context><cwd>{OLD_CWD if with_cwd else NEW_CWD}</cwd></environment_context>",
                    },
                ],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _setup_codex_home(tmp_path: Path):
    codex_home = tmp_path / "codex_home"
    session_dir = codex_home / "sessions" / "2026" / "04" / "28"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-2026-04-28-{SESSION_ID}.jsonl"
    _write_jsonl(rollout)

    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": SESSION_ID, "title": "Test Session"}) + "\n",
        encoding="utf-8"
    )

    db_path = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, sandbox_policy TEXT, "
        "title TEXT, source TEXT, created_at INTEGER, updated_at INTEGER, "
        "model_provider TEXT, approval_mode TEXT, tokens_used INTEGER, archived INTEGER)"
    )
    cur.execute(
        "INSERT INTO threads (id, cwd, sandbox_policy, title, source, created_at, updated_at, "
        "model_provider, approval_mode, tokens_used, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            SESSION_ID,
            OLD_CWD,
            json.dumps({"allow": [OLD_CWD, f"{OLD_CWD}/sub"]}),
            "Test Session",
            "cli",
            1714240000,
            1714243600,
            "openai",
            "on-request",
            1000,
            0,
        ),
    )
    conn.commit()
    conn.close()
    return codex_home, rollout, db_path


def _setup_target_codex_home(tmp_path: Path):
    target_home = tmp_path / "target_codex_home"
    target_sessions = target_home / "sessions" / "2026" / "04" / "28"
    target_sessions.mkdir(parents=True)
    target_db = target_home / "state_5.sqlite"

    conn = sqlite3.connect(target_db)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, sandbox_policy TEXT, "
        "title TEXT, source TEXT, created_at INTEGER, updated_at INTEGER, "
        "model_provider TEXT, approval_mode TEXT, tokens_used INTEGER, archived INTEGER)"
    )
    conn.commit()
    conn.close()

    return target_home


class BundleExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_bundle_creates_zip(self):
        output_path = str(self.tmp_path / "bundle.zip")
        result = self.migrator.export_bundle(SESSION_ID, output_path)

        self.assertTrue(result["success"])
        self.assertTrue(os.path.exists(output_path))
        self.assertIsNotNone(result["manifest"])

    def test_bundle_contains_manifest(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            self.assertIn("MANIFEST.json", zf.namelist())
            manifest = json.loads(zf.read("MANIFEST.json").decode("utf-8"))
            self.assertEqual(manifest["session_id"], SESSION_ID)
            self.assertEqual(manifest["tool_version"], "0.2.1")

    def test_bundle_contains_raw_jsonl(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            namelist = zf.namelist()
            self.assertTrue(
                any("sessions/raw_jsonl/" in name for name in namelist),
                f"Expected sessions/raw_jsonl/ in {namelist}"
            )

    def test_bundle_contains_session_index_record(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("index/session_index_records.jsonl", namelist)

    def test_bundle_contains_sqlite_rows(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("sqlite/state_db_matching_rows.json", namelist)
            sqlite_data = json.loads(
                zf.read("sqlite/state_db_matching_rows.json").decode("utf-8")
            )
            self.assertIn("matching_rows", sqlite_data)
            self.assertEqual(len(sqlite_data["matching_rows"]), 1)
            self.assertEqual(sqlite_data["matching_rows"][0]["id"], SESSION_ID)

    def test_bundle_excludes_sensitive_files(self):
        sensitive_file = self.codex_home / "auth.json"
        sensitive_file.write_text("sensitive data")

        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            namelist = zf.namelist()
            self.assertNotIn("auth.json", namelist)

    def test_bundle_includes_inspection_report(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            namelist = zf.namelist()
            self.assertIn("inspection/inspect_report.json", namelist)


class ImportPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_plan_does_not_write(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        before_files = list(target_home.rglob("*"))
        result = target_migrator.import_plan(output_path, [f"{OLD_CWD}={NEW_CWD}"])
        after_files = list(target_home.rglob("*"))

        self.assertTrue(result["success"])
        self.assertEqual(len(before_files), len(after_files))

    def test_import_plan_identifies_no_session_on_empty_target(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_plan(output_path, [f"{OLD_CWD}={NEW_CWD}"])

        self.assertTrue(result["success"])
        self.assertEqual(result["target_status"]["status"], "none")

    def test_import_plan_identifies_existing_session(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        conn.execute(
            "INSERT INTO threads (id, cwd, sandbox_policy, title, source, created_at, updated_at, "
            "model_provider, approval_mode, tokens_used, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (SESSION_ID, OLD_CWD, "{}", "Existing", "cli", 0, 0, "openai", "on-request", 0, 0),
        )
        conn.commit()
        conn.close()

        result = target_migrator.import_plan(output_path, [f"{OLD_CWD}={NEW_CWD}"])

        self.assertTrue(result["success"])
        self.assertIn(result["target_status"]["status"], ["complete", "incomplete"])

    def test_import_plan_with_chinese_and_spaces_in_paths(self):
        chinese_old = "/old/工作目录 with spaces"
        chinese_new = "/new/新目录"
        output_path = str(self.tmp_path / "bundle.zip")

        jsonl_path = self.codex_home / "sessions" / "2026" / "04" / "28" / f"rollout-{SESSION_ID}.jsonl"
        _write_jsonl(jsonl_path, with_cwd=True)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE threads SET cwd = ? WHERE id = ?",
            (chinese_old, SESSION_ID)
        )
        conn.commit()
        conn.close()

        result = self.migrator.export_bundle(SESSION_ID, output_path)
        self.assertTrue(result["success"])

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        plan_result = target_migrator.import_plan(
            output_path,
            [f"{chinese_old}={chinese_new}"]
        )
        self.assertTrue(plan_result["success"])


class ImportBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_bundle_without_yes_does_not_write(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        before_sessions = list((target_home / "sessions").rglob("*.jsonl"))
        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=True,
        )
        after_sessions = list((target_home / "sessions").rglob("*.jsonl"))

        self.assertTrue(result["success"])
        self.assertIn("Dry run", result.get("note", ""))
        self.assertEqual(len(before_sessions), len(after_sessions))

    def test_import_bundle_yes_writes_to_mock_codex_home(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)
    
        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))
    
        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
        )
    
        self.assertTrue(result["success"])
        self.assertGreater(len(result["imported_files"]), 0)
    
        sessions_after = list((target_home / "sessions").rglob("*.jsonl"))
        self.assertGreater(len(sessions_after), 0)

    def test_import_bundle_mode_skip_skips_existing(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)
    
        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))
    
        conn = sqlite3.connect(target_home / "state_5.sqlite")
        conn.execute(
            "INSERT INTO threads (id, cwd, sandbox_policy, title, source, created_at, updated_at, "
            "model_provider, approval_mode, tokens_used, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (SESSION_ID, OLD_CWD, "{}", "Existing", "cli", 0, 0, "openai", "on-request", 0, 0),
        )
        conn.commit()
        conn.close()
    
        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            mode="skip",
            dry_run=False,
            allow_missing_cwd=True,
        )
    
        self.assertFalse(result["success"])
        self.assertTrue(any("already exists" in e for e in result["errors"]))

    def test_import_bundle_mode_overwrite_backs_up_and_replaces(self):
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)
    
        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))
    
        conn = sqlite3.connect(target_home / "state_5.sqlite")
        conn.execute(
            "INSERT INTO threads (id, cwd, sandbox_policy, title, source, created_at, updated_at, "
            "model_provider, approval_mode, tokens_used, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (SESSION_ID, OLD_CWD, json.dumps({"allow": [OLD_CWD]}), "Existing", "cli", 0, 0, "openai", "on-request", 0, 0),
        )
        conn.commit()
        conn.close()
    
        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            mode="overwrite",
            dry_run=False,
            allow_missing_cwd=True,
        )
    
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["backup"])
    
        conn = sqlite3.connect(target_home / "state_5.sqlite")
        (cwd,) = conn.execute(
            "SELECT cwd FROM threads WHERE id = ?",
            (SESSION_ID,)
        ).fetchone()
        conn.close()
        self.assertEqual(cwd, NEW_CWD)


class SensitiveFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sensitive_files_not_in_bundle(self):
        sensitive_files = [
            "auth.json",
            "tokens",
            "credentials",
            ".netrc",
            ".env",
        ]

        for filename in sensitive_files:
            (self.codex_home / filename).write_text("sensitive")

        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        with zipfile.ZipFile(output_path, "r") as zf:
            namelist = zf.namelist()
            for filename in sensitive_files:
                self.assertNotIn(
                    filename,
                    namelist,
                    f"{filename} should not be in bundle"
                )


class StrictImportBundleTests(unittest.TestCase):
    """Strict tests for import-bundle fixes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_jsonl_cwd_is_mapped(self):
        """Verify JSONL structured cwd is mapped to new CWD after import."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
        )

        self.assertTrue(result["success"])

        sessions = list((target_home / "sessions").rglob("*.jsonl"))
        self.assertGreater(len(sessions), 0, "Should have imported at least one JSONL file")

        for session_file in sessions:
            content = session_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("type") == "session_meta":
                    self.assertEqual(
                        data["payload"].get("cwd"),
                        NEW_CWD,
                        f"session_meta.cwd should be {NEW_CWD}, got {data['payload'].get('cwd')}"
                    )
                if data.get("type") == "turn_context":
                    self.assertEqual(
                        data["payload"].get("cwd"),
                        NEW_CWD,
                        f"turn_context.cwd should be {NEW_CWD}, got {data['payload'].get('cwd')}"
                    )

    def test_import_sqlite_cwd_and_sandbox_mapped(self):
        """Verify SQLite cwd and sandbox_policy are both mapped."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        policy = MigrationPolicy(rewrite_prefix_paths=True)
        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            policy=policy,
            allow_missing_cwd=True,
        )

        self.assertTrue(result["success"])

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        cursor = conn.cursor()
        cursor.execute("SELECT cwd, sandbox_policy FROM threads WHERE id = ?", (SESSION_ID,))
        row = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(row, "Session should exist in SQLite")
        cwd, sandbox_policy = row

        self.assertEqual(
            cwd, NEW_CWD,
            f"SQLite cwd should be {NEW_CWD}, got {cwd}"
        )

        sandbox_json = json.loads(sandbox_policy)
        allow_paths = sandbox_json.get("allow", [])
        for path in allow_paths:
            self.assertNotIn(
                OLD_CWD, path,
                f"sandbox_policy should not contain {OLD_CWD}, got {allow_paths}"
            )
            if path.endswith("/sub"):
                self.assertEqual(
                    path,
                    f"{NEW_CWD}/sub",
                    f"sandbox_policy /sub path should be {NEW_CWD}/sub, got {path}"
                )

    def test_import_session_index_updated(self):
        """Verify session_index.jsonl is updated after import."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        index_path = target_home / "session_index.jsonl"
        self.assertFalse(index_path.exists(), "Index should not exist before import")

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
        )

        self.assertTrue(result["success"])

        imported_index = False
        for item in result.get("imported_files", []):
            if item.get("type") == "session_index" and item.get("action") == "updated":
                imported_index = True
                break

        self.assertTrue(imported_index, "session_index should be updated")

    def test_import_backup_created(self):
        """Verify backup is created even when session doesn't exist."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
        )

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["backup"], "Backup should be created")

        backup_dir = Path(result["backup"])
        self.assertTrue(backup_dir.exists(), "Backup directory should exist")
        self.assertTrue((backup_dir / "MANIFEST.json").exists(), "Backup should have MANIFEST")

    def test_import_with_policy_flags(self):
        """Verify policy flags are passed to import."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        policy = MigrationPolicy(
            include_function_workdir=True,
            include_environment_context=True,
        )

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            policy=policy,
            allow_missing_cwd=True,
        )

        self.assertTrue(result["success"])


class EnhancedImportPlanTests(unittest.TestCase):
    """Tests for enhanced import-plan output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_plan_shows_detailed_status(self):
        """Verify import-plan shows complete status breakdown."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_plan(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
        )

        self.assertTrue(result["success"])
        self.assertIn("target_status", result)
        self.assertIn("import_plan", result)
        self.assertIn("files_to_backup", result)

        self.assertEqual(
            result["target_status"]["status"],
            "none",
            "Target machine should have no existing session"
        )

        self.assertGreater(
            len(result["files_to_backup"]),
            0,
            "Should list files to backup"
        )

    def test_import_plan_shows_jsonl_updates(self):
        """Verify import-plan shows JSONL update details."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_plan(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
        )

        self.assertTrue(result["success"])
        planned = result["import_plan"]

        self.assertTrue(planned.get("will_create_jsonl"))
        self.assertTrue(planned.get("cwd_mappings"))

    def test_import_plan_shows_policy(self):
        """Verify import-plan includes policy information."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        policy = MigrationPolicy(rewrite_prefix_paths=True)
        result = target_migrator.import_plan(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            policy=policy,
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            result["import_plan"]["policy"]["rewrite_prefix_paths"],
            True,
        )


class ImportAsNewTests(unittest.TestCase):
    """Tests for import-as-new session functionality."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_as_new_without_conflict(self):
        """Test import-as-new when target has no existing session."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["target_session_id"])
        self.assertNotEqual(result["source_session_id"], result["target_session_id"])
        self.assertEqual(result["id_rewrite_mode"], "auto")

    def test_import_as_new_with_existing_same_session_id(self):
        """Test import-as-new when target already has same session id."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO threads (id, cwd, title) VALUES (?, ?, ?)",
            (SESSION_ID, "/existing/path", "Existing Session")
        )
        conn.commit()
        conn.close()

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])
        new_session_id = result["target_session_id"]
        self.assertNotEqual(SESSION_ID, new_session_id)

    def test_import_as_new_preserves_existing_target_session(self):
        """Test import-as-new preserves existing session on target."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO threads (id, cwd, title) VALUES (?, ?, ?)",
            (SESSION_ID, "/existing/path", "Existing Session")
        )
        conn.commit()
        conn.close()

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM threads WHERE id = ?", (SESSION_ID,))
        count = cursor.fetchone()[0]
        conn.close()

        self.assertEqual(count, 1, "Original session should still exist")

    def test_import_as_new_updates_jsonl_filename(self):
        """Test import-as-new updates JSONL filename with new session id."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])
        new_session_id = result["target_session_id"]

        jsonl_imported = False
        for item in result.get("imported_files", []):
            if item.get("type") == "jsonl" and item.get("session_id_rewritten"):
                self.assertIn(new_session_id, item["target"])
                jsonl_imported = True
                break

        self.assertTrue(jsonl_imported, "JSONL should be imported with new session id")

    def test_import_as_new_updates_sqlite_thread_id(self):
        """Test import-as-new updates SQLite thread id."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])
        new_session_id = result["target_session_id"]

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        cursor = conn.cursor()
        cursor.execute("SELECT cwd FROM threads WHERE id = ?", (new_session_id,))
        row = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(row, "New session should exist in SQLite")
        self.assertEqual(row[0], NEW_CWD)

    def test_import_as_new_updates_session_index(self):
        """Test import-as-new updates session_index with new id."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])
        new_session_id = result["target_session_id"]

        index_path = target_home / "session_index.jsonl"
        self.assertTrue(index_path.exists())

        found_new_id = False
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    record = json.loads(line)
                    if record.get("id") == new_session_id:
                        found_new_id = True
                        break

        self.assertTrue(found_new_id, "New session id should be in session_index")

    def test_import_as_new_does_not_substring_replace_prompt_text(self):
        """Test import-as-new does not substring replace in text fields."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="import-as-new",
        )

        self.assertTrue(result["success"])
        new_session_id = result["target_session_id"]

        sessions_dir = target_home / "sessions"
        jsonl_files = list(sessions_dir.rglob("*.jsonl"))

        for jsonl_file in jsonl_files:
            content = jsonl_file.read_text(encoding="utf-8")
            self.assertNotIn(
                SESSION_ID, content,
                f"Old session id should not appear in imported JSONL: {jsonl_file}"
            )
            self.assertNotIn(
                SESSION_ID[:10], content,
                f"Partial session id should not appear: {jsonl_file}"
            )

    def test_import_plan_reports_source_and_target_session_id(self):
        """Test import-plan reports source and target session id."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        result = target_migrator.import_plan(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["source_session_id"], SESSION_ID)
        self.assertEqual(result["target_session_id"], SESSION_ID)
        self.assertFalse(result["session_exists_on_target"])

    def test_explicit_new_session_id_validation(self):
        """Test explicit new_session_id parameter."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        explicit_id = "explicit-new-session-456"
        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            new_session_id=explicit_id,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["target_session_id"], explicit_id)
        self.assertEqual(result["id_rewrite_mode"], "explicit")

    def test_on_conflict_abort_when_exists(self):
        """Test --on-conflict abort fails when session exists."""
        output_path = str(self.tmp_path / "bundle.zip")
        self.migrator.export_bundle(SESSION_ID, output_path)

        target_home = _setup_target_codex_home(self.tmp_path)
        target_migrator = CodexSessionMigrator(str(target_home))

        conn = sqlite3.connect(target_home / "state_5.sqlite")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO threads (id, cwd, title) VALUES (?, ?, ?)",
            (SESSION_ID, "/existing/path", "Existing Session")
        )
        conn.commit()
        conn.close()

        result = target_migrator.import_bundle(
            output_path,
            [f"{OLD_CWD}={NEW_CWD}"],
            backup_dir=str(self.tmp_path / "backups"),
            dry_run=False,
            allow_missing_cwd=True,
            on_conflict="abort",
        )

        self.assertFalse(result["success"])
        self.assertIn("already exists", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
