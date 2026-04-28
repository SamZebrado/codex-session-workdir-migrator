#!/usr/bin/env python3
"""Create a minimal fake Codex home for CLI smoke tests."""

import argparse
import json
import sqlite3
from pathlib import Path


SESSION_ID = "smoke-session"
OLD_CWD = "/tmp/codex-migrator-old"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", help="Directory where the fake Codex home will be created")
    args = parser.parse_args()

    root = Path(args.root)
    codex_home = root / "codex_home"
    new_cwd = root / "new_workdir"
    sessions_dir = codex_home / "sessions" / "2026" / "04" / "28"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    new_cwd.mkdir(parents=True, exist_ok=True)

    rollout = sessions_dir / f"rollout-2026-04-28-{SESSION_ID}.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": SESSION_ID, "cwd": OLD_CWD}},
        {"type": "turn_context", "payload": {"cwd": OLD_CWD}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"<environment_context><cwd>{OLD_CWD}</cwd></environment_context>",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "arguments": json.dumps({"cmd": "pwd", "workdir": OLD_CWD}),
            },
        },
    ]
    with rollout.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": SESSION_ID}) + "\n", encoding="utf-8"
    )

    db_path = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, sandbox_policy TEXT)")
    cur.execute(
        "INSERT INTO threads (id, cwd, sandbox_policy) VALUES (?, ?, ?)",
        (
            SESSION_ID,
            OLD_CWD,
            json.dumps({"allow": [OLD_CWD, f"{OLD_CWD}/child"]}),
        ),
    )
    conn.commit()
    conn.close()

    print(f"CODEX_HOME={codex_home}")
    print(f"SESSION_ID={SESSION_ID}")
    print(f"OLD_CWD={OLD_CWD}")
    print(f"NEW_CWD={new_cwd}")
    print(f"BACKUP_DIR={root / 'backups'}")


if __name__ == "__main__":
    main()
