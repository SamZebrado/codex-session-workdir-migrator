#!/usr/bin/env python3
"""
Codex Session Working Directory Migration Tool
Safely migrates a Codex session from one working directory to another.
Supports local migration and cross-machine bundle export/import.
"""

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote


ENV_CWD_RE = re.compile(r"<cwd>(.*?)</cwd>", re.DOTALL)
JSONL_CATEGORIES = (
    "session_meta.cwd",
    "turn_context.cwd",
    "function_call.workdir",
    "environment_context.cwd",
)
SQLITE_FIELDS = ("threads.cwd", "threads.sandbox_policy")

TOOL_VERSION = "0.2.1"

SENSITIVE_FILES = {
    "auth.json",
    "cookies",
    "credentials",
    "tokens",
    "keychain",
    ".netrc",
    ".pypirc",
    ".npmrc",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    ".env",
    ".env.local",
}

SENSITIVE_DIRS = {
    "backups",
    "reports",
    "node_modules",
    ".git",
    "__pycache__",
}

SENSITIVE_PATTERNS = [
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"pk_[a-zA-Z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----"),
    re.compile(r"-----BEGIN DSA PRIVATE KEY-----"),
    re.compile(r"-----BEGIN EC PRIVATE KEY-----"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"gho_[a-zA-Z0-9]{36}"),
    re.compile(r"glpat-[a-zA-Z0-9]{20}"),
]


@dataclass(frozen=True)
class MigrationPolicy:
    include_function_workdir: bool = False
    include_environment_context: bool = False
    rewrite_prefix_paths: bool = False


@dataclass
class BundleManifest:
    tool_version: str
    export_time: str
    source_hostname: str
    source_os: str
    codex_version: Optional[str] = None
    session_id: str = ""
    detected_cwd: Optional[str] = None
    included_files: List[str] = field(default_factory=list)
    sqlite_rows: List[Dict] = field(default_factory=list)
    excluded_sensitive_files: List[str] = field(default_factory=list)
    bundle_structure: Dict = field(default_factory=dict)
    checksums: Dict[str, str] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


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
            suffix = value[len(old_cwd):]
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

    def _scan_sensitive_content(self, content, file_name):
        """Scan content for sensitive patterns. Returns list of findings."""
        findings = []
        if not isinstance(content, str):
            content = content.decode("utf-8", errors="ignore")
        
        for pattern in SENSITIVE_PATTERNS:
            matches = pattern.findall(content)
            for match in matches[:3]:
                findings.append({
                    "file": file_name,
                    "pattern": pattern.pattern[:50] + "..." if len(pattern.pattern) > 50 else pattern.pattern,
                    "match": match[:80] + "..." if len(match) > 80 else match,
                })
            if len(matches) > 3:
                findings.append({
                    "file": file_name,
                    "pattern": pattern.pattern[:50] + "..." if len(pattern.pattern) > 50 else pattern.pattern,
                    "match": f"{len(matches) - 3} more matches",
                })
        return findings

    def _rewrite_session_id_in_json(self, data, old_id, new_id):
        """Rewrite session ID in JSON data conservatively.
        
        Only replaces string values that exactly match the old session ID.
        Does not modify text fields like messages, prompts, or descriptions.
        """
        text_field_names = {"message", "text", "content", "summary", "description", "title"}
        
        def rewrite(node):
            if isinstance(node, str):
                if node == old_id:
                    return new_id
                return node
            if isinstance(node, list):
                return [rewrite(item) for item in node]
            if isinstance(node, dict):
                result = {}
                for key, value in node.items():
                    key_lower = key.lower()
                    if key_lower in text_field_names:
                        result[key] = value
                    else:
                        result[key] = rewrite(value)
                return result
            return node
        
        return rewrite(data)

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

    def _get_codex_version(self):
        """Attempt to get Codex CLI version."""
        try:
            result = subprocess.run(
                ["codex", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def export_bundle(self, session_id, output_path, allow_sensitive_content=False):
        """Export session bundle for cross-machine migration."""
        result = {
            "success": False,
            "bundle_path": None,
            "manifest": None,
            "errors": [],
            "sensitive_findings": [],
        }

        manifest = BundleManifest(
            tool_version=TOOL_VERSION,
            export_time=datetime.now().isoformat(),
            source_hostname=socket.gethostname(),
            source_os=f"{platform.system()} {platform.release()}",
            codex_version=self._get_codex_version(),
            session_id=session_id,
        )

        inspection = self.inspect_session(session_id)
        if not inspection["jsonl_files"] and not inspection["sqlite_thread"]:
            result["errors"].append(f"Session {session_id} not found")
            return result

        manifest.detected_cwd = inspection["sqlite_thread"].get("cwd")

        bundle_dir = os.path.dirname(output_path) or "."
        os.makedirs(bundle_dir, exist_ok=True)

        included_files = []
        excluded_files = []

        try:
            bundle_structure = {
                "inspection": {},
                "sessions": {"raw_jsonl": []},
                "index": {},
                "sqlite": {},
                "checksums": {},
            }

            all_content_for_scan = []

            for jsonl_file_info in inspection["jsonl_files"]:
                jsonl_path = jsonl_file_info["path"]
                if os.path.exists(jsonl_path):
                    arcname = f"sessions/raw_jsonl/{os.path.basename(jsonl_path)}"
                    with open(jsonl_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        all_content_for_scan.append((arcname, content))

            if inspection["session_index"]:
                idx_path = self.session_index_path
                if os.path.exists(idx_path):
                    arcname = "index/session_index_records.jsonl"
                    with open(idx_path, "r", encoding="utf-8") as f:
                        filtered_lines = []
                        for line in f:
                            try:
                                data = json.loads(line.strip())
                                if data.get("id") == session_id:
                                    filtered_lines.append(line)
                            except json.JSONDecodeError:
                                continue
                    content = "".join(filtered_lines)
                    all_content_for_scan.append((arcname, content))

            if inspection["sqlite_thread"]:
                arcname = "sqlite/state_db_matching_rows.json"
                content = json.dumps({
                    "schema_summary": "threads(id, cwd, sandbox_policy, ...)",
                    "matching_rows": [inspection["sqlite_thread"]],
                }, indent=2, ensure_ascii=False)
                all_content_for_scan.append((arcname, content))

            arcname = "inspection/inspect_report.json"
            content = json.dumps(inspection, indent=2, ensure_ascii=False)
            all_content_for_scan.append((arcname, content))

            for arcname, content in all_content_for_scan:
                sensitive = self._scan_sensitive_content(content, arcname)
                if sensitive:
                    result["sensitive_findings"].extend(sensitive)

            if result["sensitive_findings"] and not allow_sensitive_content:
                result["errors"].append(
                    f"Sensitive content detected in bundle. Use --allow-sensitive-content to export anyway. "
                    f"Found {len(result['sensitive_findings'])} potential sensitive patterns."
                )
                return result

            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for jsonl_file_info in inspection["jsonl_files"]:
                    jsonl_path = jsonl_file_info["path"]
                    if os.path.exists(jsonl_path):
                        arcname = f"sessions/raw_jsonl/{os.path.basename(jsonl_path)}"
                        zf.write(jsonl_path, arcname)
                        included_files.append(arcname)
                        bundle_structure["sessions"]["raw_jsonl"].append(arcname)
                        bundle_structure["checksums"][arcname] = self._sha256_file(jsonl_path)

                if inspection["session_index"]:
                    idx_path = self.session_index_path
                    if os.path.exists(idx_path):
                        arcname = "index/session_index_records.jsonl"
                        with open(idx_path, "r", encoding="utf-8") as f:
                            filtered_lines = []
                            for line in f:
                                try:
                                    data = json.loads(line.strip())
                                    if data.get("id") == session_id:
                                        filtered_lines.append(line)
                                except json.JSONDecodeError:
                                    continue
                            temp_idx = f"{bundle_dir}/.tmp_session_index.jsonl"
                            with open(temp_idx, "w", encoding="utf-8") as tmp:
                                tmp.writelines(filtered_lines)
                            if filtered_lines:
                                zf.write(temp_idx, arcname)
                                included_files.append(arcname)
                                bundle_structure["index"]["session_index_records"] = arcname
                                bundle_structure["checksums"][arcname] = self._sha256_file(temp_idx)
                            os.unlink(temp_idx)

                if inspection["sqlite_thread"]:
                    sqlite_info = {
                        "schema_summary": "threads(id, cwd, sandbox_policy, ...)",
                        "matching_rows": [inspection["sqlite_thread"]],
                    }
                    temp_sqlite = f"{bundle_dir}/.tmp_state_db_rows.json"
                    with open(temp_sqlite, "w", encoding="utf-8") as f:
                        json.dump(sqlite_info, f, indent=2, ensure_ascii=False)
                    arcname = "sqlite/state_db_matching_rows.json"
                    zf.write(temp_sqlite, arcname)
                    included_files.append(arcname)
                    bundle_structure["sqlite"]["state_db_matching_rows"] = arcname
                    bundle_structure["checksums"][arcname] = self._sha256_file(temp_sqlite)
                    os.unlink(temp_sqlite)

                temp_inspect = f"{bundle_dir}/.tmp_inspect.json"
                with open(temp_inspect, "w", encoding="utf-8") as f:
                    json.dump(inspection, f, indent=2, ensure_ascii=False)
                arcname = "inspection/inspect_report.json"
                zf.write(temp_inspect, arcname)
                included_files.append(arcname)
                bundle_structure["inspection"]["inspect_report"] = arcname
                os.unlink(temp_inspect)

                readme_content = """# Codex Session Migration Bundle

## How to Import

1. Copy this ZIP to the target machine
2. Run: `python3 codex_workdir_migrate.py import-plan --bundle <this_file.zip> --codex-home ~/.codex --map-cwd "OLD=NEW"`
3. Review the plan, then run: `python3 codex_workdir_migrate.py import-bundle --bundle <this_file.zip> --codex-home ~/.codex --map-cwd "OLD=NEW" --yes`

## Bundle Contents

- `sessions/raw_jsonl/`: Session conversation records
- `index/`: Session index records
- `sqlite/`: Database schema and matching thread rows
- `inspection/`: Full inspection report

## Security Notes

- No authentication files are included
- Review the MANIFEST.json for exact contents
"""
                zf.writestr("README_IMPORT.md", readme_content)

                manifest.included_files = included_files
                manifest.excluded_sensitive_files = excluded_files
                manifest.bundle_structure = bundle_structure
                manifest.checksums = bundle_structure.get("checksums", {})

                zf.writestr("MANIFEST.json", json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False))
                
                checksums_content = "\n".join(
                    f"{checksum}  {path}" 
                    for path, checksum in sorted(bundle_structure.get("checksums", {}).items())
                )
                zf.writestr("checksums/SHA256SUMS.txt", checksums_content)

            result["success"] = True
            result["bundle_path"] = output_path
            result["manifest"] = manifest.to_dict()

        except Exception as e:
            if os.path.exists(output_path):
                os.unlink(output_path)
            result["errors"].append(f"Failed to create bundle: {e}")

        return result

    def check_import_target(self, session_id):
        """Check if session already exists on target machine."""
        target_info = {
            "session_exists": False,
            "jsonl_files": [],
            "sqlite_record": None,
            "session_index_record": None,
        }

        target_jsonl_files = self.find_session_file(session_id)
        if target_jsonl_files:
            target_info["session_exists"] = True
            target_info["jsonl_files"] = target_jsonl_files

        try:
            if os.path.exists(self.state_db_path):
                db = self._connect_state_db(readonly=True)
                cursor = db.cursor()
                cursor.execute("SELECT * FROM threads WHERE id = ?", (session_id,))
                row = cursor.fetchone()
                if row:
                    cursor.execute("PRAGMA table_info(threads)")
                    col_names = [c[1] for c in cursor.fetchall()]
                    target_info["sqlite_record"] = dict(zip(col_names, row))
                db.close()
        except Exception:
            pass

        try:
            if os.path.exists(self.session_index_path):
                with open(self.session_index_path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            data = json.loads(line.strip())
                            if data.get("id") == session_id:
                                target_info["session_index_record"] = data
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception:
            pass

        return target_info

    def import_plan(self, bundle_path, cwd_mappings, policy=None):
        """Generate detailed import plan from bundle."""
        if policy is None:
            policy = MigrationPolicy()

        result = {
            "success": False,
            "bundle_contents": {},
            "target_status": {},
            "import_plan": {},
            "files_to_backup": [],
            "errors": [],
            "warnings": [],
            "source_session_id": None,
            "target_session_id": None,
            "session_exists_on_target": False,
        }

        if not os.path.exists(bundle_path):
            result["errors"].append(f"Bundle not found: {bundle_path}")
            return result

        cwd_map = {}
        for mapping in cwd_mappings:
            if "=" in mapping:
                old, new = mapping.split("=", 1)
                cwd_map[old] = new

        if len(cwd_map) > 1:
            result["errors"].append(
                "Multiple cwd_mappings not supported. Please provide exactly one --map-cwd."
            )
            return result

        try:
            with zipfile.ZipFile(bundle_path, "r") as zf:
                manifest_str = zf.read("MANIFEST.json").decode("utf-8")
                manifest_data = json.loads(manifest_str)
                namelist = zf.namelist()

                result["bundle_contents"] = {
                    "tool_version": manifest_data.get("tool_version"),
                    "export_time": manifest_data.get("export_time"),
                    "session_id": manifest_data.get("session_id"),
                    "detected_cwd": manifest_data.get("detected_cwd"),
                    "included_files": manifest_data.get("included_files", []),
                    "source_hostname": manifest_data.get("source_hostname"),
                    "source_os": manifest_data.get("source_os"),
                    "has_jsonl": any("sessions/raw_jsonl/" in m for m in namelist),
                    "has_index": any("index/" in m for m in namelist),
                    "has_sqlite": any("sqlite/" in m for m in namelist),
                }

                source_session_id = manifest_data.get("session_id")
                result["source_session_id"] = source_session_id
                result["target_session_id"] = source_session_id

                target_info = self.check_import_target(source_session_id)

                has_jsonl = target_info["session_exists"]
                has_sqlite = target_info["sqlite_record"] is not None
                has_index = target_info["session_index_record"] is not None
                session_exists = has_jsonl or has_sqlite
                result["session_exists_on_target"] = session_exists

                if not has_jsonl and not has_sqlite and not has_index:
                    result["target_status"] = {
                        "status": "none",
                        "message": "Session does not exist on target machine",
                        "has_jsonl": False,
                        "has_sqlite": False,
                        "has_index": False,
                    }
                elif has_jsonl and has_sqlite and has_index:
                    result["target_status"] = {
                        "status": "complete",
                        "message": "Session exists with JSONL, SQLite record, and index",
                        "has_jsonl": True,
                        "has_sqlite": True,
                        "has_index": True,
                        "jsonl_files": target_info["jsonl_files"],
                        "sqlite_cwd": target_info["sqlite_record"].get("cwd"),
                    }
                else:
                    missing = []
                    if not has_jsonl:
                        missing.append("jsonl")
                    if not has_sqlite:
                        missing.append("sqlite")
                    if not has_index:
                        missing.append("index")
                    result["target_status"] = {
                        "status": "incomplete",
                        "message": f"Session exists but missing: {', '.join(missing)}",
                        "has_jsonl": has_jsonl,
                        "has_sqlite": has_sqlite,
                        "has_index": has_index,
                        "jsonl_files": target_info["jsonl_files"],
                        "sqlite_record": target_info["sqlite_record"],
                    }

                target_jsonl_dir = os.path.join(self.sessions_dir, datetime.now().strftime("%Y/%m/%d"))
                files_to_backup = []

                if os.path.exists(self.state_db_path):
                    files_to_backup.append(self.state_db_path)
                    for ext in ["-wal", "-shm"]:
                        wal_path = self.state_db_path + ext
                        if os.path.exists(wal_path):
                            files_to_backup.append(wal_path)
                if os.path.exists(self.session_index_path):
                    files_to_backup.append(self.session_index_path)
                for jsonl_path in target_info.get("jsonl_files", []):
                    if os.path.exists(jsonl_path):
                        files_to_backup.append(jsonl_path)

                planned_jsonl_updates = []
                if any("sessions/raw_jsonl/" in m for m in namelist):
                    planned_jsonl_updates.append({
                        "action": "create",
                        "target_dir": target_jsonl_dir,
                        "cwd_mapping": bool(cwd_map),
                        "affected_fields": ["session_meta.cwd", "turn_context.cwd"],
                    })

                planned_sqlite_updates = []
                has_sqlite_in_bundle = any("sqlite/" in m for m in namelist)
                
                if has_sqlite_in_bundle:
                    if not os.path.exists(self.state_db_path):
                        result["errors"].append(
                            "Bundle contains SQLite data but target state_5.sqlite does not exist. Import will fail. Initialize Codex on target machine first."
                        )
                    else:
                        try:
                            db = self._connect_state_db(readonly=True)
                            cursor = db.cursor()
                            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'")
                            if not cursor.fetchone():
                                result["errors"].append(
                                    "Bundle contains SQLite data but target has no 'threads' table. Import will fail. Initialize Codex on target machine first."
                                )
                            elif cwd_map:
                                planned_sqlite_updates.append({
                                    "action": "update",
                                    "table": "threads",
                                    "fields": ["cwd", "sandbox_policy"],
                                    "cwd_mapping": cwd_map,
                                })
                            db.close()
                        except Exception as e:
                            result["warnings"].append(
                                f"Could not check target SQLite: {e}. SQLite import may fail."
                            )
                elif cwd_map:
                    planned_sqlite_updates.append({
                        "action": "update",
                        "table": "threads",
                        "fields": ["cwd", "sandbox_policy"],
                        "cwd_mapping": cwd_map,
                    })

                planned_index_updates = []
                if any("index/" in m for m in namelist):
                    planned_index_updates.append({
                        "action": "update" if has_index else "create",
                        "target_file": self.session_index_path,
                    })

                risks = []
                if cwd_map and manifest_data.get("detected_cwd") not in cwd_map:
                    risks.append(f"Detected CWD '{manifest_data.get('detected_cwd')}' not in cwd_mappings")

                if session_exists:
                    risks.append("Target machine has existing session data - will be overwritten")

                if not cwd_map:
                    risks.append("No cwd_mappings provided - imported session will keep original CWD")

                result["import_plan"] = {
                    "session_id": source_session_id,
                    "cwd_mappings": cwd_map,
                    "will_create_jsonl": bool(planned_jsonl_updates),
                    "will_update_sqlite": bool(planned_sqlite_updates),
                    "will_update_index": bool(planned_index_updates),
                    "target_jsonl_dir": target_jsonl_dir,
                    "planned_jsonl_updates": planned_jsonl_updates,
                    "planned_sqlite_updates": planned_sqlite_updates,
                    "planned_index_updates": planned_index_updates,
                    "backup_required": True,
                    "risks": risks,
                    "policy": self._policy_dict(policy),
                }

                result["files_to_backup"] = files_to_backup
                if not result["errors"]:
                    result["success"] = True

        except Exception as e:
            result["errors"].append(f"Failed to read bundle: {e}")

        return result

    def import_bundle(
        self,
        bundle_path,
        cwd_mappings,
        backup_dir,
        mode="skip",
        dry_run=True,
        policy=None,
        allow_missing_cwd=False,
        no_backup=False,
        on_conflict="abort",
        new_session_id=None,
    ):
        """Import bundle to target machine."""
        if policy is None:
            policy = MigrationPolicy()

        result = {
            "dry_run": dry_run,
            "success": False,
            "backup": None,
            "imported_files": [],
            "errors": [],
            "warnings": [],
            "source_session_id": None,
            "target_session_id": None,
            "id_rewrite_mode": "none",
        }

        if not os.path.exists(bundle_path):
            result["errors"].append(f"Bundle not found: {bundle_path}")
            return result

        if dry_run:
            result["note"] = "Dry run - no files written. Use --yes to actually import."

        cwd_map = {}
        for mapping in cwd_mappings:
            if "=" in mapping:
                old, new = mapping.split("=", 1)
                cwd_map[old] = new

        if len(cwd_map) > 1:
            result["errors"].append(
                "Multiple cwd_mappings not supported. Please provide exactly one --map-cwd."
            )
            return result

        if not allow_missing_cwd and not dry_run and cwd_map:
            for old_cwd, new_cwd in cwd_map.items():
                if new_cwd and not os.path.exists(new_cwd):
                    result["errors"].append(
                        f"Target directory does not exist: {new_cwd}. Use --allow-missing-cwd to allow."
                    )
                    return result

        backup_manifest = {"timestamp": datetime.now().strftime("%Y%m%d_%H%M%S_%f"), "files_backed_up": []}

        try:
            with zipfile.ZipFile(bundle_path, "r") as zf:
                manifest_str = zf.read("MANIFEST.json").decode("utf-8")
                manifest_data = json.loads(manifest_str)
                source_session_id = manifest_data.get("session_id")
                result["source_session_id"] = source_session_id

                target_info = self.check_import_target(source_session_id)

                has_jsonl = target_info["session_exists"]
                has_sqlite = target_info["sqlite_record"] is not None
                has_index = target_info["session_index_record"] is not None
                session_exists = has_jsonl or has_sqlite

                effective_on_conflict = on_conflict
                if mode == "overwrite" and on_conflict == "abort":
                    effective_on_conflict = "overwrite"

                target_session_id = source_session_id
                id_rewrite_mode = "none"

                if new_session_id:
                    target_session_id = new_session_id
                    id_rewrite_mode = "explicit"
                elif effective_on_conflict == "import-as-new":
                    target_session_id = str(uuid.uuid4())
                    id_rewrite_mode = "auto"

                result["target_session_id"] = target_session_id
                result["id_rewrite_mode"] = id_rewrite_mode

                if target_session_id != source_session_id:
                    target_info_new = self.check_import_target(target_session_id)
                    has_jsonl_new = target_info_new["session_exists"]
                    has_sqlite_new = target_info_new["sqlite_record"] is not None
                    has_index_new = target_info_new["session_index_record"] is not None
                    target_session_exists = has_jsonl_new or has_sqlite_new or has_index_new

                    if target_session_exists:
                        result["errors"].append(
                            f"Target session id '{target_session_id}' already exists on target. Use a different --new-session-id or --on-conflict overwrite."
                        )
                        return result

                if session_exists and effective_on_conflict == "abort":
                    result["errors"].append(
                        f"Session {source_session_id} already exists on target. Use --on-conflict overwrite or import-as-new."
                    )
                    return result

                if mode == "merge":
                    result["errors"].append("Mode 'merge' is not yet implemented.")
                    return result

                if session_exists and mode == "skip" and id_rewrite_mode == "none" and effective_on_conflict != "overwrite":
                    result["errors"].append(
                        f"Session {source_session_id} already exists. Use --mode overwrite, --on-conflict overwrite, or --on-conflict import-as-new."
                    )
                    return result

                sqlite_member = "sqlite/state_db_matching_rows.json"
                has_sqlite_in_bundle = sqlite_member in zf.namelist()
                
                bundle_sqlite_data = None
                if has_sqlite_in_bundle:
                    bundle_sqlite_data = json.loads(zf.read(sqlite_member).decode("utf-8"))
                    bundle_rows = bundle_sqlite_data.get("matching_rows", [])
                    
                    if not os.path.exists(self.state_db_path):
                        result["errors"].append(
                            f"Bundle contains SQLite data but target state_5.sqlite does not exist. "
                            f"Import will fail. Initialize Codex on target machine first."
                        )
                        return result
                    
                    try:
                        db = self._connect_state_db(readonly=True)
                        cursor = db.cursor()
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'")
                        if not cursor.fetchone():
                            result["errors"].append(
                                f"Bundle contains SQLite data but target has no 'threads' table. "
                                f"Import will fail. Initialize Codex on target machine first."
                            )
                            db.close()
                            return result
                        
                        cursor.execute("PRAGMA table_info(threads)")
                        not_null_columns = set()
                        for row in cursor.fetchall():
                            col_name = row[1]
                            not_null = row[3]
                            dflt_value = row[4]
                            if not_null and dflt_value is None:
                                not_null_columns.add(col_name)
                        
                        if bundle_rows and not_null_columns:
                            missing_not_null = []
                            bundle_row = bundle_rows[0]
                            for col in not_null_columns:
                                if col not in bundle_row or bundle_row.get(col) is None:
                                    missing_not_null.append(col)
                            
                            if missing_not_null:
                                result["errors"].append(
                                    f"Bundle SQLite row missing NOT NULL columns without defaults: {missing_not_null}. "
                                    f"Import will fail. Ensure bundle contains all required columns."
                                )
                                db.close()
                                return result
                        
                        db.close()
                    except Exception as e:
                        result["errors"].append(
                            f"Failed to check target SQLite: {e}. Import will fail."
                        )
                        return result

                files_to_backup = []
                if os.path.exists(self.state_db_path):
                    files_to_backup.append(self.state_db_path)
                    for ext in ["-wal", "-shm"]:
                        wal_path = self.state_db_path + ext
                        if os.path.exists(wal_path):
                            files_to_backup.append(wal_path)
                if os.path.exists(self.session_index_path):
                    files_to_backup.append(self.session_index_path)
                for jsonl_path in target_info.get("jsonl_files", []):
                    if os.path.exists(jsonl_path):
                        files_to_backup.append(jsonl_path)

                if not dry_run and not no_backup and backup_dir and files_to_backup:
                    os.makedirs(backup_dir, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    backup_path = os.path.join(backup_dir, f"import_backup_{timestamp}")
                    os.makedirs(backup_path, exist_ok=True)

                    backup_manifest = {
                        "timestamp": timestamp,
                        "files_backed_up": [],
                    }

                    for src_path in files_to_backup:
                        if os.path.exists(src_path):
                            dst = os.path.join(backup_path, os.path.basename(src_path))
                            shutil.copy2(src_path, dst)
                            stat = os.stat(src_path)
                            backup_manifest["files_backed_up"].append({
                                "original_path": src_path,
                                "backup_path": dst,
                                "size": stat.st_size,
                                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                                "sha256": self._sha256_file(src_path),
                            })

                    manifest_path = os.path.join(backup_path, "MANIFEST.json")
                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(backup_manifest, f, indent=2, ensure_ascii=False)
                    result["backup"] = backup_path
                elif not dry_run and not no_backup and files_to_backup and not backup_dir:
                    result["warnings"].append(
                        "No backup directory provided. Proceeding without backup."
                    )

                jsonl_members = [m for m in zf.namelist() if m.startswith("sessions/raw_jsonl/")]
                
                if effective_on_conflict == "overwrite" and target_info["jsonl_files"] and not dry_run and id_rewrite_mode == "none":
                    for old_jsonl in target_info["jsonl_files"]:
                        if os.path.exists(old_jsonl):
                            os.unlink(old_jsonl)
                            result["imported_files"].append({
                                "type": "jsonl",
                                "action": "deleted_old",
                                "path": old_jsonl,
                            })

                for member in jsonl_members:
                    content_bytes = zf.read(member)
                    basename = os.path.basename(member)

                    if id_rewrite_mode != "none":
                        basename = basename.replace(source_session_id, target_session_id)

                    target_dir = os.path.join(self.sessions_dir, datetime.now().strftime("%Y/%m/%d"))
                    target_path = os.path.join(target_dir, basename)

                    if not dry_run:
                        os.makedirs(target_dir, exist_ok=True)

                    if (cwd_map or id_rewrite_mode != "none") and not dry_run:
                        content_str = content_bytes.decode("utf-8")
                        updated_lines = []
                        changes_made = 0
                        warnings = []

                        for line in content_str.splitlines(keepends=True):
                            stripped = line.strip()
                            if not stripped:
                                updated_lines.append(line)
                                continue
                            try:
                                data = json.loads(stripped)
                                updated_data = data

                                if cwd_map:
                                    updated_data, categories = self._update_json_line(
                                        data, list(cwd_map.keys())[0], list(cwd_map.values())[0], policy
                                    )
                                    if categories:
                                        changes_made += len(categories)

                                if id_rewrite_mode != "none":
                                    updated_data = self._rewrite_session_id_in_json(
                                        updated_data, source_session_id, target_session_id
                                    )

                                updated_lines.append(
                                    json.dumps(updated_data, ensure_ascii=False) + "\n"
                                )
                            except json.JSONDecodeError as e:
                                warnings.append(f"Could not parse JSON line: {e}")
                                updated_lines.append(line)

                        if warnings:
                            result["warnings"].extend(warnings)

                        temp_path = target_path + ".tmp"
                        with open(temp_path, "w", encoding="utf-8") as f:
                            f.writelines(updated_lines)
                        os.replace(temp_path, target_path)
                    else:
                        if not dry_run:
                            with open(target_path, "wb") as f:
                                f.write(content_bytes)

                    result["imported_files"].append({
                        "type": "jsonl",
                        "source": member,
                        "target": target_path,
                        "cwd_mapped": bool(cwd_map),
                        "session_id_rewritten": id_rewrite_mode != "none",
                    })

                sqlite_member = "sqlite/state_db_matching_rows.json"
                if sqlite_member in zf.namelist():
                    sqlite_data = json.loads(zf.read(sqlite_member).decode("utf-8"))
                    rows = sqlite_data.get("matching_rows", [])

                    if rows:
                        thread_data = copy.deepcopy(rows[0])

                        if cwd_map:
                            old_cwd = list(cwd_map.keys())[0]
                            new_cwd = list(cwd_map.values())[0]

                            if thread_data.get("cwd") == old_cwd:
                                thread_data["cwd"] = new_cwd

                            sandbox_policy = thread_data.get("sandbox_policy", "")
                            rewritten_policy, sandbox_changed = self._rewrite_sandbox_policy(
                                sandbox_policy, old_cwd, new_cwd, policy
                            )
                            if sandbox_changed:
                                thread_data["sandbox_policy"] = rewritten_policy

                        if id_rewrite_mode != "none":
                            thread_data["id"] = target_session_id
                            if "rollout_path" in thread_data:
                                thread_data["rollout_path"] = thread_data["rollout_path"].replace(
                                    source_session_id, target_session_id
                                )

                        if not dry_run:
                            db = self._connect_state_db(readonly=False)
                            cursor = db.cursor()

                            cursor.execute("PRAGMA table_info(threads)")
                            target_columns = {row[1] for row in cursor.fetchall()}
                            required_columns = {"id"}

                            missing_required = required_columns - target_columns
                            if missing_required:
                                result["errors"].append(
                                    f"Target threads table missing required columns: {missing_required}"
                                )
                                db.close()
                                return result

                            cursor.execute("PRAGMA table_info(threads)")
                            not_null_columns = set()
                            for row in cursor.fetchall():
                                col_name = row[1]
                                not_null = row[3]
                                dflt_value = row[4]
                                if not_null and dflt_value is None:
                                    not_null_columns.add(col_name)

                            cursor.execute(
                                "SELECT COUNT(*) FROM threads WHERE id = ?", (target_session_id,)
                            )
                            exists = cursor.fetchone()[0] > 0

                            if exists:
                                update_columns = []
                                update_values = []
                                if "cwd" in target_columns and "cwd" in thread_data:
                                    update_columns.append("cwd = ?")
                                    update_values.append(thread_data["cwd"])
                                if "sandbox_policy" in target_columns and "sandbox_policy" in thread_data:
                                    update_columns.append("sandbox_policy = ?")
                                    update_values.append(thread_data["sandbox_policy"])
                                if "updated_at" in target_columns and "updated_at" in thread_data:
                                    update_columns.append("updated_at = ?")
                                    update_values.append(thread_data["updated_at"])

                                if update_columns:
                                    update_values.append(target_session_id)
                                    cursor.execute(
                                        f"UPDATE threads SET {', '.join(update_columns)} WHERE id = ?",
                                        update_values
                                    )
                            else:
                                insert_columns = []
                                insert_placeholders = []
                                insert_values = []

                                for col in sorted(target_columns):
                                    if col in thread_data and thread_data[col] is not None:
                                        insert_columns.append(col)
                                        insert_placeholders.append("?")
                                        insert_values.append(thread_data[col])

                                if insert_columns and "id" in insert_columns:
                                    cursor.execute(
                                        f"INSERT INTO threads ({', '.join(insert_columns)}) VALUES ({', '.join(insert_placeholders)})",
                                        insert_values
                                    )
                                else:
                                    result["errors"].append(
                                        "Cannot insert thread: no valid columns or missing id"
                                    )
                                    db.close()
                                    return result

                            db.commit()
                            db.close()

                        result["imported_files"].append({
                            "type": "sqlite",
                            "session_id": target_session_id,
                            "cwd_updated": bool(cwd_map),
                            "session_id_rewritten": id_rewrite_mode != "none",
                        })

                index_member = "index/session_index_records.jsonl"
                if index_member in zf.namelist():
                    index_content = zf.read(index_member).decode("utf-8")

                    index_path = self.session_index_path
                    existing_records = {}

                    if os.path.exists(index_path):
                        with open(index_path, "r", encoding="utf-8") as f:
                            for line in f:
                                try:
                                    record = json.loads(line.strip())
                                    if record.get("id"):
                                        existing_records[record["id"]] = line
                                except json.JSONDecodeError:
                                    continue

                    session_index_record = None
                    for line in index_content.splitlines():
                        if line.strip():
                            try:
                                record = json.loads(line.strip())
                                if record.get("id") == source_session_id:
                                    session_index_record = record
                                    break
                            except json.JSONDecodeError:
                                continue

                    if session_index_record:
                        if cwd_map:
                            old_cwd = list(cwd_map.keys())[0]
                            new_cwd = list(cwd_map.values())[0]

                            path_tokens = {"cwd", "path", "workdir", "dir", "root", "directory"}
                            non_text_fields = {"id", "title", "message", "summary", "description"}

                            for field_name in list(session_index_record.keys()):
                                if field_name.lower() in non_text_fields:
                                    continue
                                field_lower = field_name.lower()
                                if any(token in field_lower for token in path_tokens):
                                    if isinstance(session_index_record[field_name], str):
                                        if session_index_record[field_name] == old_cwd:
                                            session_index_record[field_name] = new_cwd
                                        elif (
                                            policy and policy.rewrite_prefix_paths
                                            and session_index_record[field_name].startswith(old_cwd + os.sep)
                                        ):
                                            suffix = session_index_record[field_name][len(old_cwd):]
                                            session_index_record[field_name] = new_cwd + suffix

                        if id_rewrite_mode != "none":
                            session_index_record["id"] = target_session_id
                            if "rollout_path" in session_index_record:
                                session_index_record["rollout_path"] = session_index_record["rollout_path"].replace(
                                    source_session_id, target_session_id
                                )

                        if mode == "skip" and target_session_id in existing_records:
                            result["imported_files"].append({
                                "type": "session_index",
                                "action": "skipped",
                                "reason": "session exists",
                            })
                        else:
                            existing_records[target_session_id] = json.dumps(session_index_record, ensure_ascii=False) + "\n"

                            if not dry_run:
                                temp_path = index_path + ".tmp"
                                with open(temp_path, "w", encoding="utf-8") as f:
                                    for rec_line in existing_records.values():
                                        f.write(rec_line)
                                os.replace(temp_path, index_path)

                            result["imported_files"].append({
                                "type": "session_index",
                                "action": "updated",
                                "session_id": target_session_id,
                                "session_id_rewritten": id_rewrite_mode != "none",
                            })

                result["success"] = True

        except Exception as e:
            result["errors"].append(f"Import failed: {e}")

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

    export_parser = subparsers.add_parser("export-bundle", help="Export session bundle for cross-machine migration")
    export_parser.add_argument("--session", required=True, help="Session ID")
    export_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")
    export_parser.add_argument("--out", required=True, help="Output ZIP path")
    export_parser.add_argument("--allow-sensitive-content", action="store_true", help="Allow export even if sensitive content is detected")

    import_plan_parser = subparsers.add_parser("import-plan", help="Generate import plan from bundle")
    import_plan_parser.add_argument("--bundle", required=True, help="Bundle ZIP path")
    import_plan_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")
    import_plan_parser.add_argument(
        "--map-cwd",
        action="append",
        default=[],
        help="CWD mapping in format OLD=NEW (single mapping supported)",
    )
    _add_policy_args(import_plan_parser)

    import_parser = subparsers.add_parser("import-bundle", help="Import session bundle to target machine")
    import_parser.add_argument("--bundle", required=True, help="Bundle ZIP path")
    import_parser.add_argument("--codex-home", help="Custom Codex home (default: ~/.codex)")
    import_parser.add_argument(
        "--map-cwd",
        action="append",
        default=[],
        help="CWD mapping in format OLD=NEW (single mapping supported)",
    )
    import_parser.add_argument("--backup-dir", help="Backup directory for target files")
    import_parser.add_argument(
        "--mode",
        choices=["skip", "overwrite", "merge"],
        default="skip",
        help="Conflict resolution mode: skip=don't overwrite existing, overwrite=replace, merge=not implemented",
    )
    import_parser.add_argument("--yes", action="store_true", help="Actually write changes")
    import_parser.add_argument("--no-backup", action="store_true", help="Skip backup (use with caution)")
    import_parser.add_argument("--allow-missing-cwd", action="store_true", help="Allow importing to non-existent target directory")
    import_parser.add_argument(
        "--on-conflict",
        choices=["abort", "overwrite", "import-as-new"],
        default="abort",
        help="Conflict handling when target has same session id: abort=fail, overwrite=replace, import-as-new=create new session",
    )
    import_parser.add_argument(
        "--new-session-id",
        help="New session ID to use (use 'auto' for auto-generated UUID, or specify explicit ID)",
    )
    _add_policy_args(import_parser)

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

    if args.command == "export-bundle":
        result = migrator.export_bundle(
            args.session, 
            args.out, 
            allow_sensitive_content=getattr(args, "allow_sensitive_content", False)
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    if args.command == "import-plan":
        policy = _build_policy_from_args(args)
        result = migrator.import_plan(args.bundle, args.map_cwd, policy=policy)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    if args.command == "import-bundle":
        if args.yes and not args.backup_dir and not getattr(args, "no_backup", False):
            print(json.dumps({
                "success": False,
                "errors": ["--yes requires either --backup-dir or --no-backup (use with caution)"],
            }, indent=2, ensure_ascii=False))
            return 1
        
        policy = _build_policy_from_args(args)
        new_session_id = getattr(args, "new_session_id", None)
        if new_session_id == "auto":
            new_session_id = str(uuid.uuid4())
        
        result = migrator.import_bundle(
            args.bundle,
            args.map_cwd,
            args.backup_dir,
            mode=args.mode,
            dry_run=not args.yes,
            policy=policy,
            allow_missing_cwd=getattr(args, "allow_missing_cwd", False),
            no_backup=getattr(args, "no_backup", False),
            on_conflict=getattr(args, "on_conflict", "abort"),
            new_session_id=new_session_id,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["success"] else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
