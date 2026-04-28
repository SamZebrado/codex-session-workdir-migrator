import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_workdir_migrate import CodexSessionMigrator, MigrationPolicy


SESSION_ID = "sess-123"
OLD = "/old/workdir"
NEW = "/new/workdir"


def _write_jsonl(path: Path):
    records = [
        {"type": "session_meta", "payload": {"cwd": OLD}},
        {"type": "turn_context", "payload": {"cwd": OLD}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"<environment_context><cwd>{OLD}</cwd></environment_context>",
                    },
                    {"type": "input_text", "text": "plain user text should stay untouched"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "arguments": json.dumps({"workdir": OLD, "cmd": "ls"}),
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _setup_codex_home(tmp_path: Path):
    codex_home = tmp_path / "codex_home"
    session_dir = codex_home / "sessions" / "2026" / "04" / "28"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-2026-04-28-{SESSION_ID}.jsonl"
    _write_jsonl(rollout)

    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": SESSION_ID}) + "\n", encoding="utf-8"
    )

    db_path = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, sandbox_policy TEXT)"
    )
    cur.execute(
        "INSERT INTO threads (id, cwd, sandbox_policy) VALUES (?, ?, ?)",
        (SESSION_ID, OLD, json.dumps({"allow": [OLD, f"{OLD}/sub"]})),
    )
    conn.commit()
    conn.close()
    return codex_home, rollout, db_path


class MigratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.codex_home, self.rollout, self.db_path = _setup_codex_home(self.tmp_path)
        self.migrator = CodexSessionMigrator(str(self.codex_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_structured_only_default(self):
        plan = self.migrator.plan(SESSION_ID, OLD, NEW, MigrationPolicy())
        self.assertEqual(plan["summary"]["jsonl"]["session_meta.cwd"], 1)
        self.assertEqual(plan["summary"]["jsonl"]["turn_context.cwd"], 1)
        self.assertEqual(plan["summary"]["jsonl"]["function_call.workdir"], 0)
        self.assertEqual(plan["summary"]["jsonl"]["environment_context.cwd"], 0)
        self.assertTrue(plan["summary"]["sqlite"]["threads.cwd"])
        self.assertTrue(plan["summary"]["sqlite"]["threads.sandbox_policy"])

    def test_plan_with_optional_flags(self):
        policy = MigrationPolicy(include_function_workdir=True, include_environment_context=True)
        plan = self.migrator.plan(SESSION_ID, OLD, NEW, policy)
        self.assertEqual(plan["summary"]["jsonl"]["function_call.workdir"], 1)
        self.assertEqual(plan["summary"]["jsonl"]["environment_context.cwd"], 1)

    def test_apply_dry_run_does_not_write(self):
        new_dir = self.tmp_path / "new_workdir"
        new_dir.mkdir()

        before_jsonl = self.rollout.read_text(encoding="utf-8")
        conn = sqlite3.connect(self.db_path)
        before_db = conn.execute(
            "SELECT cwd, sandbox_policy FROM threads WHERE id = ?", (SESSION_ID,)
        ).fetchone()
        conn.close()

        result = self.migrator.migrate(
            SESSION_ID,
            OLD,
            str(new_dir),
            backup_dir=str(self.tmp_path / "backups"),
            policy=MigrationPolicy(),
            dry_run=True,
            backup_mode="minimal",
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["summary"]["jsonl"]["session_meta.cwd"], 1)
        self.assertEqual(result["summary"]["jsonl"]["turn_context.cwd"], 1)
        self.assertEqual(result["summary"]["jsonl"]["function_call.workdir"], 0)
        self.assertEqual(result["summary"]["jsonl"]["environment_context.cwd"], 0)

        after_jsonl = self.rollout.read_text(encoding="utf-8")
        self.assertEqual(after_jsonl, before_jsonl)
        self.assertFalse(Path(str(self.rollout) + ".tmp").exists())

        conn = sqlite3.connect(self.db_path)
        after_db = conn.execute(
            "SELECT cwd, sandbox_policy FROM threads WHERE id = ?", (SESSION_ID,)
        ).fetchone()
        conn.close()
        self.assertEqual(after_db, before_db)

    def test_apply_real_writes_and_minimal_backup(self):
        new_dir = self.tmp_path / "new_workdir"
        new_dir.mkdir()
        backup_dir = self.tmp_path / "backups"
        backup_dir.mkdir()

        result = self.migrator.migrate(
            SESSION_ID,
            OLD,
            str(new_dir),
            backup_dir=str(backup_dir),
            policy=MigrationPolicy(),
            dry_run=False,
            backup_mode="minimal",
        )

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["backup"])

        rows = _read_jsonl(self.rollout)
        self.assertEqual(rows[0]["payload"]["cwd"], str(new_dir))
        self.assertEqual(rows[1]["payload"]["cwd"], str(new_dir))
        message_text = rows[2]["payload"]["content"][0]["text"]
        self.assertIn(f"<cwd>{OLD}</cwd>", message_text)
        fn_args = json.loads(rows[3]["payload"]["arguments"])
        self.assertEqual(fn_args["workdir"], OLD)

        conn = sqlite3.connect(self.db_path)
        cwd, sandbox = conn.execute(
            "SELECT cwd, sandbox_policy FROM threads WHERE id = ?", (SESSION_ID,)
        ).fetchone()
        conn.close()
        self.assertEqual(cwd, str(new_dir))
        sandbox_json = json.loads(sandbox)
        self.assertIn(str(new_dir), sandbox_json["allow"])
        self.assertIn(f"{OLD}/sub", sandbox_json["allow"])
        self.assertNotIn(OLD, sandbox_json["allow"])
        self.assertIn(str(new_dir), sandbox)

        backup_root = Path(result["backup"])
        copied = {p.name for p in backup_root.iterdir()}
        self.assertIn("MANIFEST.json", copied)
        self.assertIn("state_5.sqlite", copied)
        self.assertIn(self.rollout.name, copied)

        verify = self.migrator.verify(SESSION_ID, OLD, str(new_dir), MigrationPolicy())
        self.assertTrue(verify["success"])
        new_jsonl_categories = {
            item["ref"]["category"]
            for item in verify["new_cwd_refs"]
            if "ref" in item
        }
        self.assertEqual(new_jsonl_categories, {"session_meta.cwd", "turn_context.cwd"})

    def test_prefix_paths_only_change_with_explicit_flag(self):
        new_dir = self.tmp_path / "new_workdir"
        new_dir.mkdir()

        self.migrator.migrate(
            SESSION_ID,
            OLD,
            str(new_dir),
            backup_dir=str(self.tmp_path / "backups"),
            policy=MigrationPolicy(rewrite_prefix_paths=True),
            dry_run=False,
            backup_mode="minimal",
        )

        conn = sqlite3.connect(self.db_path)
        (sandbox,) = conn.execute(
            "SELECT sandbox_policy FROM threads WHERE id = ?", (SESSION_ID,)
        ).fetchone()
        conn.close()
        sandbox_json = json.loads(sandbox)
        self.assertIn(f"{str(new_dir)}/sub", sandbox_json["allow"])
        self.assertNotIn(f"{OLD}/sub", sandbox_json["allow"])

    def test_apply_optional_flags_change_extra_fields(self):
        new_dir = self.tmp_path / "new_workdir"
        new_dir.mkdir()

        result = self.migrator.migrate(
            SESSION_ID,
            OLD,
            str(new_dir),
            backup_dir=str(self.tmp_path / "backups"),
            policy=MigrationPolicy(include_function_workdir=True, include_environment_context=True),
            dry_run=False,
            backup_mode="minimal",
        )

        self.assertEqual(result["summary"]["jsonl"]["function_call.workdir"], 1)
        self.assertEqual(result["summary"]["jsonl"]["environment_context.cwd"], 1)

        rows = _read_jsonl(self.rollout)
        message_text = rows[2]["payload"]["content"][0]["text"]
        self.assertIn(f"<cwd>{str(new_dir)}</cwd>", message_text)
        fn_args = json.loads(rows[3]["payload"]["arguments"])
        self.assertEqual(fn_args["workdir"], str(new_dir))

    def test_verify_uses_policy_consistently(self):
        default_verify = self.migrator.verify(SESSION_ID, OLD, NEW, MigrationPolicy())
        self.assertFalse(default_verify["success"])
        self.assertEqual(default_verify["summary"]["jsonl"]["session_meta.cwd"], 1)
        self.assertEqual(default_verify["summary"]["jsonl"]["turn_context.cwd"], 1)
        self.assertEqual(default_verify["summary"]["jsonl"]["function_call.workdir"], 0)
        self.assertEqual(default_verify["summary"]["jsonl"]["environment_context.cwd"], 0)

        expanded_verify = self.migrator.verify(
            SESSION_ID,
            OLD,
            NEW,
            MigrationPolicy(include_function_workdir=True, include_environment_context=True),
        )
        self.assertEqual(expanded_verify["summary"]["jsonl"]["function_call.workdir"], 1)
        self.assertEqual(expanded_verify["summary"]["jsonl"]["environment_context.cwd"], 1)


if __name__ == "__main__":
    unittest.main()
