#!/usr/bin/env python3
"""
Codex Session Working Directory Migration Tool
Safely migrates a Codex session from one working directory to another.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import quote


ENV_CWD_RE = re.compile(r"<cwd>(.*?)</cwd>", re.DOTALL)
JSONL_CATEGORIES = (
    "session_meta.cwd",
    "turn_context.cwd",
    "function_call.workdir",
    "environment_context.cwd",
)
SQLITE_FIELDS = ("threads.cwd", "threads.sandbox_policy")


@dataclass(frozen=True)
class MigrationPolicy:
    include_function_workdir: bool = False
    include_environment_context: bool = False
    rewrite_prefix_paths: bool = False


class CodexSessionMigrator:
    def __init__(self, codex_home=None):
        self.codex_home = codex_home or os.path.expanduser("~/.codex")
        self.sessions_dir = os.path.join(self.codex_home, "sessions")
        self.archived_sessions_dir = os.path.join(self.codex_home, "archived_sessions")
        self.state_db_path = os.path.join(self.codex_home, "state_5.sqlite")
        self.session_index_path = os.path.join(self.codex_home, "session_index.jsonl")

    def find_session_file(self, session_id):
        """Find JSONL file(s) for a given session ID."""
        candidates = []
        for base_dir in [self.sessions_dir, self.archived_sessions_dir]:
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for file in files:
                    if file.endswith(".jsonl") and session_id in file:
                        candidates.append(os.path.join(root, file))

        if not candidates:
            for base_dir in [self.sessions_dir, self.archived_sessions_dir]:
                if not os.path.exists(base_dir):
                    continue
                for root, _, files in os.walk(base_dir):
                    for file in files:
                        if not file.endswith(".jsonl"):
                            continue
                        full_path = os.path.join(root, file)
                        try:
                            with open(full_path, "r", encoding="utf-8") as f:
                                for line in f:
                                    try:
                                        data = json.loads(line)
                                        if data.get("payload", {}).get("id") == session_id:
                                            candidates.append(full_path)
                                            break
                                    except json.JSONDecodeError:
                                        continue
                        except Exception:
                            continue

        return candidates

    def _rewrite_path_value(
        self, value: Optional[str], old_cwd: str, new_cwd: str, rewrite_prefix_paths: bool
    ) -> Tuple[Optional[str], bool]:
        if not isinstance(value, str):
            return value, False
        if value == old_cwd:
            return new_cwd, True
        if rewrite_prefix_paths and value.startswith(old_cwd + os.sep):
            suffix = value[len(old_cwd) :]
            return new_cwd + suffix, True
        return value, False

    def _empty_jsonl_counts(self):
        return {category: 0 for category in JSONL_CATEGORIES}

    def _empty_sqlite_counts(self):
        return {field: 0 for field in SQLITE_FIELDS}

    def _policy_dict(self, policy: MigrationPolicy):
        return {
            "mode": "structured-only",
            "include_function_workdir": policy.include_function_workdir,
            "include_environment_context": policy.include_environment_context,
            "rewrite_prefix_paths": policy.rewrite_prefix_paths,
        }

    def _rewrite_sandbox_policy(self, value, old_cwd, new_cwd, policy: MigrationPolicy):
        if not isinstance(value, str):
            return value, False

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return self._rewrite_path_value(
                value, old_cwd, new_cwd, policy.rewrite_prefix_paths
            )

        def rewrite_node(node):
            if isinstance(node, str):
                return self._rewrite_path_value(
                    node, old_cwd, new_cwd, policy.rewrite_prefix_paths
                )
            if isinstance(node, list):
                changed = False
                updated = []
                for item in node:
                    new_item, item_changed = rewrite_node(item)
                    updated.append(new_item)
                    changed = changed or item_changed
                return updated, changed
            if isinstance(node, dict):
                changed = False
                updated = {}
                for key, item in node.items():
                    new_item, item_changed = rewrite_node(item)
                    updated[key] = new_item
                    changed = changed or item_changed
                return updated, changed
            return node, False

        rewritten, changed = rewrite_node(parsed)
        if not changed:
            return value, False
        return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")), True

    def _connect_state_db(self, readonly=False):
        if readonly:
            db_uri = "file:" + quote(os.path.abspath(self.state_db_path)) + "?mode=ro"
            return sqlite3.connect(db_uri, uri=True)
        return sqlite3.connect(self.state_db_path)

    def _extract_refs(self, data, line_num):
        refs = []
        payload = data.get("payload", {})

        if data.get("type") == "session_meta" and "cwd" in payload:
            refs.append(
                {
                    "line": line_num,
                    "category": "session_meta.cwd",
                    "value": payload.get("cwd"),
                    "updatable": True,
                }
            )

        if data.get("type") == "turn_context" and "cwd" in payload:
            refs.append(
                {
                    "line": line_num,
                    "category": "turn_context.cwd",
                    "value": payload.get("cwd"),
                    "updatable": True,
                }
            )

        if data.get("type") == "response_item" and payload.get("type") == "function_call":
            args_raw = payload.get("arguments", "{}")
            try:
                args = json.loads(args_raw)
                if "workdir" in args:
                    refs.append(
                        {
                            "line": line_num,
                            "category": "function_call.workdir",
                            "value": args.get("workdir"),
                            "updatable": False,
                        }
                    )
            except json.JSONDecodeError:
                pass

        if data.get("type") == "response_item" and payload.get("type") == "message":
            for item in payload.get("content", []):
                if item.get("type") != "input_text":
                    continue
                text = item.get("text", "")
                for env_cwd in ENV_CWD_RE.findall(text):
                    refs.append(
                        {
                            "line": line_num,
                            "category": "environment_context.cwd",
                            "value": env_cwd,
                            "updatable": False,
                            "text_preview": text[:200],
                        }
                    )

        return refs

    def _update_json_line(self, data, old_cwd, new_cwd, policy: MigrationPolicy):
        """Return (updated_data, changes) without relying on object equality checks."""
        updated = copy.deepcopy(data)
        payload = updated.get("payload", {})
        changes = []

        if updated.get("type") == "session_meta":
            new_val, changed = self._rewrite_path_value(
                payload.get("cwd"), old_cwd, new_cwd, policy.rewrite_prefix_paths
            )
            if changed:
                payload["cwd"] = new_val
                changes.append("session_meta.cwd")

        if updated.get("type") == "turn_context":
            new_val, changed = self._rewrite_path_value(
                payload.get("cwd"), old_cwd, new_cwd, policy.rewrite_prefix_paths
            )
            if changed:
                payload["cwd"] = new_val
                changes.append("turn_context.cwd")

        if (
            policy.include_function_workdir
            and updated.get("type") == "response_item"
            and payload.get("type") == "function_call"
        ):
            try:
                args = json.loads(payload.get("arguments", "{}"))
                new_val, changed = self._rewrite_path_value(
                    args.get("workdir"), old_cwd, new_cwd, policy.rewrite_prefix_paths
                )
                if changed:
                    args["workdir"] = new_val
                    payload["arguments"] = json.dumps(args, ensure_ascii=False)
                    changes.append("function_call.workdir")
            except json.JSONDecodeError:
                pass

        if (
            policy.include_environment_context
            and updated.get("type") == "response_item"
            and payload.get("type") == "message"
        ):
            for item in payload.get("content", []):
                if item.get("type") != "input_text":
                    continue
                text = item.get("text", "")
                if not text:
                    continue
                did_change = False
                def _replace(match):
                    nonlocal did_change
                    original = match.group(1)
                    new_val, changed = self._rewrite_path_value(
                        original, old_cwd, new_cwd, policy.rewrite_prefix_paths
                    )
                    if changed:
                        did_change = True
                    return f"<cwd>{new_val}</cwd>"
                updated_text = ENV_CWD_RE.sub(_replace, text)
                if did_change:
                    item["text"] = updated_text
                    changes.append("environment_context.cwd")

        return updated, changes

    def _classify_jsonl_would_change(self, ref, old_cwd, policy: MigrationPolicy):
        value = ref.get("value")
        _, match = self._rewrite_path_value(value, old_cwd, "__NEW__", policy.rewrite_prefix_paths)
        if not match:
            return False

        return self._jsonl_ref_in_policy_scope(ref, policy)

    def _jsonl_ref_in_policy_scope(self, ref, policy: MigrationPolicy):
        category = ref["category"]
        if category in {"session_meta.cwd", "turn_context.cwd"}:
            return True
        if category == "function_call.workdir":
            return policy.include_function_workdir
        if category == "environment_context.cwd":
            return policy.include_environment_context
        return False

    def _jsonl_ref_matches(self, ref, cwd, policy: MigrationPolicy):
        _, match = self._rewrite_path_value(
            ref.get("value"), cwd, "__REWRITE_SENTINEL__", policy.rewrite_prefix_paths
        )
        return match

    def inspect_session(self, session_id):
        result = {
            "session_id": session_id,
            "jsonl_files": [],
            "session_index": {},
            "sqlite_thread": {},
        }

        jsonl_files = self.find_session_file(session_id)
        for jsonl_path in jsonl_files:
            file_info = {"path": jsonl_path, "cwd_references": []}
            try:
                with open(jsonl_path, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            data = json.loads(stripped)
                            refs = self._extract_refs(data, line_num)
                            if refs:
                                file_info["cwd_references"].extend(refs)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                file_info["error"] = str(e)
            result["jsonl_files"].append(file_info)

        try:
            if os.path.exists(self.session_index_path):
                with open(self.session_index_path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            data = json.loads(line)
                            if data.get("id") == session_id:
                                result["session_index"] = data
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            result["session_index"]["error"] = str(e)

        try:
            if os.path.exists(self.state_db_path):
                db = self._connect_state_db(readonly=True)
                cursor = db.cursor()
                cursor.execute("SELECT * FROM threads WHERE id = ?", (session_id,))
                row = cursor.fetchone()
                if row:
                    cursor.execute("PRAGMA table_info(threads)")
                    col_names = [c[1] for c in cursor.fetchall()]
                    result["sqlite_thread"] = dict(zip(col_names, row))
                db.close()
        except Exception as e:
            result["sqlite_thread"]["error"] = str(e)

        return result

    def plan(self, session_id, old_cwd, new_cwd, policy: MigrationPolicy):
        inspection = self.inspect_session(session_id)
        detected_jsonl = self._empty_jsonl_counts()
        summary = self._empty_jsonl_counts()
        files_with_changes = []

        for file_info in inspection["jsonl_files"]:
            file_counts = self._empty_jsonl_counts()
            for ref in file_info.get("cwd_references", []):
                if self._jsonl_ref_matches(ref, old_cwd, policy):
                    detected_jsonl[ref["category"]] += 1
                if self._classify_jsonl_would_change(ref, old_cwd, policy):
                    category = ref["category"]
                    summary[category] += 1
                    file_counts[category] += 1
            if any(v > 0 for v in file_counts.values()):
                files_with_changes.append({"path": file_info["path"], "counts": file_counts})

        sqlite_changes = {field: False for field in SQLITE_FIELDS}
        sqlite_detected = {field: False for field in SQLITE_FIELDS}
        thread = inspection.get("sqlite_thread", {})
        thread_cwd = thread.get("cwd")
        _, sqlite_cwd_change = self._rewrite_path_value(
            thread_cwd, old_cwd, new_cwd, policy.rewrite_prefix_paths
        )
        sqlite_detected["threads.cwd"] = sqlite_cwd_change
        sqlite_changes["threads.cwd"] = sqlite_cwd_change

        sandbox_policy = thread.get("sandbox_policy")
        _, sandbox_change = self._rewrite_sandbox_policy(
            sandbox_policy, old_cwd, new_cwd, policy
        )
        sqlite_detected["threads.sandbox_policy"] = sandbox_change
        sqlite_changes["threads.sandbox_policy"] = sandbox_change

        return {
            "session_id": session_id,
            "old_cwd": old_cwd,
            "new_cwd": new_cwd,
            "policy": self._policy_dict(policy),
            "inspection": inspection,
            "detected": {
                "jsonl": detected_jsonl,
                "sqlite": sqlite_detected,
            },
            "summary": {
                "jsonl": summary,
                "sqlite": sqlite_changes,
            },
            "files_with_changes": files_with_changes,
        }

    def _sha256_file(self, file_path):
        hash_sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()

    def create_backup(self, backup_dir, files_to_backup):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = os.path.join(backup_dir, f"backup_{timestamp}")
        os.makedirs(backup_path, exist_ok=True)

        manifest = {"timestamp": timestamp, "files": []}
        for src_path in sorted(set(files_to_backup)):
            if not os.path.exists(src_path):
                continue
            dst_path = os.path.join(backup_path, os.path.basename(src_path))
            shutil.copy2(src_path, dst_path)
            manifest["files"].append(
                {
                    "original_path": src_path,
                    "backup_path": dst_path,
                    "size": os.path.getsize(src_path),
                    "sha256": self._sha256_file(src_path),
                }
            )

        manifest_path = os.path.join(backup_path, "MANIFEST.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return backup_path

    def _backup_file_list(self, session_id, backup_mode, plan_result):
        jsonl_files = [entry["path"] for entry in plan_result.get("files_with_changes", [])]
        sqlite_files = []
        if plan_result["summary"]["sqlite"]["threads.cwd"] or plan_result["summary"]["sqlite"]["threads.sandbox_policy"]:
            sqlite_files.extend([
                self.state_db_path,
                self.state_db_path + "-shm",
                self.state_db_path + "-wal",
            ])

        if backup_mode == "minimal":
            return jsonl_files + sqlite_files

        full = []
        full.extend(self.find_session_file(session_id))
        if os.path.exists(self.session_index_path):
            full.append(self.session_index_path)
        full.extend([
            self.state_db_path,
            self.state_db_path + "-shm",
            self.state_db_path + "-wal",
        ])
        return full

    def _scan_jsonl_updates(self, jsonl_path, old_cwd, new_cwd, policy: MigrationPolicy):
        changes_made = 0
        per_category = self._empty_jsonl_counts()
        updated_lines = []

        with open(jsonl_path, "r", encoding="utf-8") as f_in:
            for line in f_in:
                stripped = line.strip()
                if not stripped:
                    updated_lines.append(line)
                    continue
                try:
                    data = json.loads(stripped)
                    updated_data, categories = self._update_json_line(
                        data, old_cwd, new_cwd, policy
                    )
                    if categories:
                        changes_made += 1
                        for category in categories:
                            per_category[category] += 1
                    updated_lines.append(json.dumps(updated_data, ensure_ascii=False) + "\n")
                except json.JSONDecodeError:
                    updated_lines.append(line)

        return updated_lines, changes_made, per_category

    def migrate(
        self,
        session_id,
        old_cwd,
        new_cwd,
        backup_dir,
        policy: MigrationPolicy,
        backup_mode="minimal",
        dry_run=True,
    ):
        result = {
            "dry_run": dry_run,
            "backup": None,
            "backup_mode": backup_mode,
            "policy": self._policy_dict(policy),
            "modified_files": [],
            "errors": [],
            "summary": {
                "jsonl": self._empty_jsonl_counts(),
                "sqlite": self._empty_sqlite_counts(),
            },
        }

        if not os.path.exists(new_cwd):
            result["errors"].append(f"New working directory does not exist: {new_cwd}")
            return result

        plan_result = self.plan(session_id, old_cwd, new_cwd, policy)

        if not dry_run and backup_dir:
            files_to_backup = self._backup_file_list(session_id, backup_mode, plan_result)
            result["backup"] = self.create_backup(backup_dir, files_to_backup)

        jsonl_files = self.find_session_file(session_id)
        for jsonl_path in jsonl_files:
            temp_path = jsonl_path + ".tmp"
            try:
                updated_lines, changes_made, per_category = self._scan_jsonl_updates(
                    jsonl_path, old_cwd, new_cwd, policy
                )

                if changes_made > 0:
                    if not dry_run:
                        with open(temp_path, "w", encoding="utf-8") as f_out:
                            f_out.writelines(updated_lines)
                        os.replace(temp_path, jsonl_path)
                    result["modified_files"].append(
                        {"path": jsonl_path, "line_changes": changes_made, "counts": per_category}
                    )
                    for category, count in per_category.items():
                        result["summary"]["jsonl"][category] += count
            except Exception as e:
                result["errors"].append(f"Failed to update {jsonl_path}: {e}")
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

        if os.path.exists(self.state_db_path):
            try:
                db = self._connect_state_db(readonly=dry_run)
                cursor = db.cursor()
                cursor.execute(
                    "SELECT id, cwd, sandbox_policy FROM threads WHERE id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row:
                    _, current_cwd, sandbox_policy = row
                    sqlite_changed = []

                    new_sqlite_cwd, cwd_changed = self._rewrite_path_value(
                        current_cwd, old_cwd, new_cwd, policy.rewrite_prefix_paths
                    )
                    if cwd_changed:
                        if not dry_run:
                            cursor.execute(
                                "UPDATE threads SET cwd = ? WHERE id = ?",
                                (new_sqlite_cwd, session_id),
                            )
                        sqlite_changed.append("threads.cwd")
                        result["summary"]["sqlite"]["threads.cwd"] += 1

                    new_policy, policy_changed = self._rewrite_sandbox_policy(
                        sandbox_policy, old_cwd, new_cwd, policy
                    )
                    if policy_changed:
                        if not dry_run:
                            cursor.execute(
                                "UPDATE threads SET sandbox_policy = ? WHERE id = ?",
                                (new_policy, session_id),
                            )
                        sqlite_changed.append("threads.sandbox_policy")
                        result["summary"]["sqlite"]["threads.sandbox_policy"] += 1

                    if sqlite_changed:
                        if not dry_run:
                            db.commit()
                        result["modified_files"].append(
                            {"path": self.state_db_path, "changes": sqlite_changed}
                        )
                db.close()
            except Exception as e:
                result["errors"].append(f"Failed to update SQLite: {e}")

        return result

    def verify(self, session_id, old_cwd, new_cwd, policy: MigrationPolicy):
        inspection = self.inspect_session(session_id)
        result = {
            "success": True,
            "issues": [],
            "old_cwd_refs": [],
            "new_cwd_refs": [],
            "policy": self._policy_dict(policy),
            "summary": {
                "jsonl": self._empty_jsonl_counts(),
                "sqlite": self._empty_sqlite_counts(),
            },
        }

        for file_info in inspection["jsonl_files"]:
            for ref in file_info.get("cwd_references", []):
                category = ref["category"]
                value = ref.get("value")
                _, old_match = self._rewrite_path_value(
                    value, old_cwd, "__NEW__", policy.rewrite_prefix_paths
                )
                _, new_match = self._rewrite_path_value(
                    value, new_cwd, "__OLD__", policy.rewrite_prefix_paths
                )
                in_scope = self._jsonl_ref_in_policy_scope(ref, policy)

                if old_match and in_scope:
                    result["old_cwd_refs"].append({"file": file_info["path"], "ref": ref})
                    result["summary"]["jsonl"][category] += 1
                    result["success"] = False
                if new_match and in_scope:
                    result["new_cwd_refs"].append({"file": file_info["path"], "ref": ref})

        thread = inspection.get("sqlite_thread", {})
        cwd_val = thread.get("cwd")
        _, old_sqlite_cwd = self._rewrite_path_value(
            cwd_val, old_cwd, "__NEW__", policy.rewrite_prefix_paths
        )
        _, new_sqlite_cwd = self._rewrite_path_value(
            cwd_val, new_cwd, "__OLD__", policy.rewrite_prefix_paths
        )
        if old_sqlite_cwd:
            result["old_cwd_refs"].append({"source": "sqlite.cwd", "cwd": cwd_val})
            result["summary"]["sqlite"]["threads.cwd"] += 1
            result["success"] = False
        if new_sqlite_cwd:
            result["new_cwd_refs"].append({"source": "sqlite.cwd", "cwd": cwd_val})

        sandbox_policy = thread.get("sandbox_policy", "")
        _, old_sandbox_policy = self._rewrite_sandbox_policy(
            sandbox_policy, old_cwd, "__NEW__", policy
        )
        _, new_sandbox_policy = self._rewrite_sandbox_policy(
            sandbox_policy, new_cwd, "__OLD__", policy
        )
        if old_sandbox_policy:
            result["old_cwd_refs"].append(
                {"source": "sqlite.sandbox_policy", "preview": sandbox_policy[:200]}
            )
            result["summary"]["sqlite"]["threads.sandbox_policy"] += 1
            result["success"] = False
        if new_sandbox_policy:
            result["new_cwd_refs"].append(
                {"source": "sqlite.sandbox_policy", "preview": sandbox_policy[:200]}
            )

        return result


def _build_policy_from_args(args):
    return MigrationPolicy(
        include_function_workdir=getattr(args, "include_function_workdir", False),
        include_environment_context=getattr(args, "include_environment_context", False),
        rewrite_prefix_paths=getattr(args, "rewrite_prefix_paths", False),
    )


def _add_policy_args(parser):
    parser.add_argument(
        "--include-function-workdir",
        action="store_true",
        help="Expand scope: also rewrite function_call.arguments.workdir (off by default).",
    )
    parser.add_argument(
        "--include-environment-context",
        action="store_true",
        help="Expand scope: also rewrite <environment_context><cwd>...</cwd> text blocks (off by default).",
    )
    parser.add_argument(
        "--rewrite-prefix-paths",
        action="store_true",
        help="Risky mode: rewrite prefixed paths like old/path/subdir -> new/path/subdir (off by default).",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Codex Session Working Directory Migration Tool"
    )
    subparsers = parser.add_subparsers(title="commands", dest="command")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect cwd references")
    inspect_parser.add_argument("--session", required=True, help="Session ID")
    inspect_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")

    plan_parser = subparsers.add_parser("plan", help="Generate a migration plan")
    plan_parser.add_argument("--session", required=True, help="Session ID")
    plan_parser.add_argument("--from", dest="old_cwd", required=True, help="Old CWD")
    plan_parser.add_argument("--to", dest="new_cwd", required=True, help="New CWD")
    plan_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")
    _add_policy_args(plan_parser)

    apply_parser = subparsers.add_parser("apply", help="Apply migration")
    apply_parser.add_argument("--session", required=True, help="Session ID")
    apply_parser.add_argument("--from", dest="old_cwd", required=True, help="Old CWD")
    apply_parser.add_argument("--to", dest="new_cwd", required=True, help="New CWD")
    apply_parser.add_argument("--backup-dir", required=True, help="Backup directory")
    apply_parser.add_argument(
        "--backup-mode",
        choices=["full", "minimal"],
        default="minimal",
        help="Backup scope: full=all related files, minimal=only files that this run will modify.",
    )
    apply_parser.add_argument("--yes", action="store_true", help="Actually write changes")
    apply_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")
    _add_policy_args(apply_parser)

    verify_parser = subparsers.add_parser("verify", help="Verify migration")
    verify_parser.add_argument("--session", required=True, help="Session ID")
    verify_parser.add_argument("--from", dest="old_cwd", required=True, help="Old CWD")
    verify_parser.add_argument("--to", dest="new_cwd", required=True, help="New CWD")
    verify_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")
    _add_policy_args(verify_parser)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    migrator = CodexSessionMigrator(codex_home=getattr(args, "codex_home", None))

    if args.command == "inspect":
        print(json.dumps(migrator.inspect_session(args.session), indent=2, ensure_ascii=False))
        return 0

    if args.command == "plan":
        policy = _build_policy_from_args(args)
        print(
            json.dumps(
                migrator.plan(args.session, args.old_cwd, args.new_cwd, policy),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "apply":
        policy = _build_policy_from_args(args)
        result = migrator.migrate(
            args.session,
            args.old_cwd,
            args.new_cwd,
            args.backup_dir,
            policy=policy,
            backup_mode=args.backup_mode,
            dry_run=not args.yes,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if not result["errors"] else 1

    if args.command == "verify":
        policy = _build_policy_from_args(args)
        result = migrator.verify(args.session, args.old_cwd, args.new_cwd, policy)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
