# Bundle 格式说明

## 概述

Bundle 是一个 ZIP 文件，用于在不同 Codex 安装之间迁移会话数据。

## 目录结构

```
bundle.zip/
├── MANIFEST.json              # Bundle 元数据
├── README_IMPORT.md           # 导入说明
├── inspection/
│   └── inspect_report.json    # 完整检测报告
├── sessions/
│   └── raw_jsonl/
│       └── rollout-<timestamp>-<session-id>.jsonl
├── index/
│   └── session_index_records.jsonl
├── sqlite/
│   └── state_db_matching_rows.json
└── checksums/                 # (内嵌在 MANIFEST.json 中)
    └── SHA256SUMS.txt
```

## 文件说明

### MANIFEST.json

Bundle 的元数据文件，包含：

```json
{
  "tool_version": "0.2.0",
  "export_time": "2026-04-28T12:00:00.000000",
  "source_hostname": "hostname",
  "source_os": "Darwin 26.2",
  "codex_version": "0.122.0",
  "session_id": "<SESSION_ID>",
  "detected_cwd": "/old/workdir",
  "included_files": [
    "sessions/raw_jsonl/rollout-....jsonl",
    "index/session_index_records.jsonl",
    "sqlite/state_db_matching_rows.json"
  ],
  "excluded_sensitive_files": [],
  "bundle_structure": {
    "inspection": {...},
    "sessions": {...},
    "index": {...},
    "sqlite": {...},
    "checksums": {...}
  },
  "sha256": "..."
}
```

### sessions/raw_jsonl/*.jsonl

原始会话 JSONL 文件，包含完整的对话记录。

### index/session_index_records.jsonl

会话索引记录（筛选后只包含目标会话）。

### sqlite/state_db_matching_rows.json

SQLite 数据库中匹配行的数据：

```json
{
  "schema_summary": "threads(id, cwd, sandbox_policy, ...)",
  "matching_rows": [
    {
      "id": "...",
      "cwd": "...",
      "sandbox_policy": "...",
      ...
    }
  ]
}
```

### inspection/inspect_report.json

完整的检测报告，包含：
- JSONL 文件路径
- CWD 引用位置
- SQLite 记录详情

## 敏感文件排除

以下文件**不会**被打包：

- `auth.json` - 认证信息
- `tokens` - 令牌
- `credentials` - 凭据
- `cookies` - Cookies
- `keychain` - 钥匙串
- `.netrc` - 网络凭据
- `.env` / `.env.local` - 环境变量
- 其他疑似认证凭据

## 校验

Bundle 包含 SHA256 校验和，可以通过 MANIFEST.json 中的 `sha256` 字段验证完整性。
