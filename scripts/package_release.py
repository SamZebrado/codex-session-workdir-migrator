#!/usr/bin/env python3
"""
Package release script for Codex Session Workdir Migrator
Creates a clean release zip without sensitive files.
"""

import hashlib
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path


EXCLUDE_PATTERNS = [
    "reports/",
    "reports/**",
    "backups/",
    "backups/**",
    "__pycache__/",
    "__pycache__/**",
    ".pytest_cache/",
    ".pytest_cache/**",
    "*.pyc",
    "*.pyo",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.zip",
    ".DS_Store",
    ".git/",
    ".gitignore",
    "*.egg-info/",
    "AUDIT_REPORT.md",
    "docs/inspection_report.md",
]


def _load_sensitive_patterns_from_env():
    """Load sensitive patterns from environment variable or return defaults.

    Default patterns are intentionally generic to avoid false positives.
    Use SENSITIVE_PATTERNS environment variable for project-specific patterns.
    All patterns are treated as regular expressions (regex).
    """
    env_patterns = os.environ.get("SENSITIVE_PATTERNS", "")
    if env_patterns:
        patterns = []
        for p in env_patterns.split(","):
            p = p.strip()
            if not p:
                continue
            try:
                patterns.append(re.compile(p))
            except re.error as e:
                print(f"Error: Invalid regex in SENSITIVE_PATTERNS: '{p}' - {e}", file=sys.stderr)
                sys.exit(1)
        return patterns
    return [
        re.compile(r"GoogleDrive-[a-zA-Z0-9_-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        re.compile(r"[\u4e00-\u9fff]{3,}"),
    ]


def is_data_file(path):
    """Check if file is a data file (JSONL, SQLite dump) that may contain real paths."""
    ext = os.path.splitext(path)[1].lower()
    return ext in {".jsonl", ".json", ".db"}


SENSITIVE_PATTERNS = _load_sensitive_patterns_from_env()


def should_exclude(path):
    """Check if path matches exclude patterns."""
    path_str = str(path)
    for pattern in EXCLUDE_PATTERNS:
        if pattern.endswith("/"):
            if path_str.startswith(pattern) or f"/{pattern}" in path_str:
                return True
        elif pattern.startswith("*."):
            if path_str.endswith(pattern[1:]):
                return True
        elif path_str == pattern:
            return True
    return False


def check_sensitive_content(zip_path):
    """Check if zip contains sensitive content using regex matching."""
    sensitive_found = []
    code_extensions = {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".bash"}

    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            ext = os.path.splitext(name)[1]
            is_code = ext in code_extensions
            is_data = is_data_file(name)

            for pattern in SENSITIVE_PATTERNS:
                if pattern.search(name):
                    sensitive_found.append(f"filename: {name} matches pattern {pattern.pattern}")

            content = zf.read(name)
            if isinstance(content, bytes):
                content_str = content.decode("utf-8", errors="ignore")
            else:
                content_str = str(content)

            if is_code:
                lines = content_str.splitlines()
                safe_lines = []
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    safe_lines.append(line)
                safe_content = "\n".join(safe_lines)
            else:
                safe_content = content_str

            for pattern in SENSITIVE_PATTERNS:
                if pattern.search(safe_content):
                    if pattern.pattern == r"[\u4e00-\u9fff]{3,}" and not is_data:
                        continue
                    sensitive_found.append(
                        f"content: {name} matches pattern {pattern.pattern[:50]}..."
                    )
    return sensitive_found


def package_release(source_dir, output_path=None):
    """Package the repository into a clean release zip."""
    source_path = Path(source_dir).resolve()

    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = source_path.parent / f"codex-session-workdir-migrator-{timestamp}.zip"
    else:
        output_path = Path(output_path).resolve()

    print(f"Packaging {source_path} -> {output_path}")

    included_files = []
    excluded_files = []

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_path):
            root_path = Path(root)

            dirs[:] = [d for d in dirs if not should_exclude(root_path / d)]

            for file in files:
                file_path = root_path / file
                rel_path = file_path.relative_to(source_path)

                if should_exclude(rel_path):
                    excluded_files.append(str(rel_path))
                    continue

                arcname = f"codex-session-workdir-migrator/{rel_path}"
                zf.write(file_path, arcname)
                included_files.append(str(rel_path))

    print(f"\nIncluded {len(included_files)} files:")
    for f in sorted(included_files):
        print(f"  + {f}")

    if excluded_files:
        print(f"\nExcluded {len(excluded_files)} files:")
        for f in sorted(excluded_files)[:10]:
            print(f"  - {f}")
        if len(excluded_files) > 10:
            print(f"  ... and {len(excluded_files) - 10} more")

    print(f"\nVerifying zip integrity...")
    try:
        with zipfile.ZipFile(output_path, "r") as zf:
            bad_file = zf.testzip()
            if bad_file:
                print(f"ERROR: Corrupt file in zip: {bad_file}")
                return False
    except Exception as e:
        print(f"ERROR: Failed to verify zip: {e}")
        return False

    sensitive = check_sensitive_content(output_path)
    if sensitive:
        print(f"\nWARNING: Sensitive content found in zip:")
        for item in sensitive[:5]:
            print(f"  ! {item}")
        if len(sensitive) > 5:
            print(f"  ... and {len(sensitive) - 5} more")
        return False

    print(f"\nZip created successfully: {output_path}")
    print(f"Size: {output_path.stat().st_size / 1024:.1f} KB")

    sha256 = hashlib.sha256()
    with open(output_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    print(f"SHA256: {sha256.hexdigest()}")

    return True


def main():
    if len(sys.argv) < 2:
        source_dir = Path(__file__).parent.parent
    else:
        source_dir = Path(sys.argv[1])

    if len(sys.argv) >= 3:
        output_path = Path(sys.argv[2])
    else:
        output_path = None

    if not source_dir.exists():
        print(f"ERROR: Source directory not found: {source_dir}")
        sys.exit(1)

    success = package_release(source_dir, output_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
