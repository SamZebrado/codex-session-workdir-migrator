# Current Capability Audit - 2026-05-15

## Current Commands

| Command | Purpose | Write? |
|---------|---------|--------|
| `inspect` | 探测会话的 cwd 引用 | No |
| `plan` | 生成迁移计划 | No |
| `apply` | 执行迁移 | Yes (--yes) |
| `verify` | 验证迁移结果 | No |
| `export-bundle` | 导出会话为 portable ZIP | Yes |
| `import-plan` | 在目标机器生成导入计划 | No |
| `import-bundle` | 执行跨电脑导入 | Yes (--yes) |

## Current Inspect Scope

检查以下位置：

### JSONL Files
- `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<session-id>.jsonl`
- `~/.codex/archived_sessions/*.jsonl`

检查字段：
- `session_meta.payload.cwd`
- `turn_context.payload.cwd`
- `function_call.arguments.workdir` (仅标记，不修改除非 --include-function-workdir)
- `<environment_context><cwd>...</cwd>` (仅标记，不修改除非 --include-environment-context)

### Session Index
- `~/.codex/session_index.jsonl` - 检查 cwd/path 相关字段

### SQLite
- `~/.codex/state_5.sqlite`
- 表：`threads`
- 字段：`cwd`, `sandbox_policy`

## Current plan/apply/verify Boundaries

### plan
- 输出检测到的 cwd 引用
- 分类为 structured (session_meta, turn_context, sqlite) vs optional (function_call, environment_context)
- 不写入任何文件

### apply
- 默认 dry-run
- 使用 `--yes` 才写入
- 支持 `--backup-mode full|minimal`
- 支持 `--include-function-workdir`
- 支持 `--include-environment-context`
- 支持 `--rewrite-prefix-paths`

### verify
- 检查旧 cwd 是否仍存在于 structured fields
- 检查新 cwd 是否已写入
- 分类残留类型
- 默认使用 structured-only 策略

## Current backup vs portable bundle

### Current backup
- 目的：本地迁移前安全网
- 存储：本地 backups/ 目录
- 内容：受影响的 JSONL, SQLite
- 保留时间：用户决定

### Portable bundle (IMPLEMENTED)
- 目的：跨电脑迁移
- 存储：ZIP 文件
- 内容：完整会话数据 + 元数据 + 校验和
- 携带性：自包含，可复制到另一台电脑

## Cross-machine Migration Features

| Feature | Status | Notes |
|---------|--------|-------|
| export-bundle | ✅ | 打包会话为 portable ZIP |
| import-plan | ✅ | 生成导入计划（只读） |
| import-bundle | ✅ | 执行导入（支持 --yes） |
| verify-import | ✅ | 使用 verify 命令验证 |
| --mode skip | ✅ | 跳过已存在的会话 |
| --mode overwrite | ✅ | 覆盖已存在的会话 |
| --map-cwd | ✅ | CWD 映射 |
| --backup-dir | ✅ | 备份目录 |
| --no-backup | ✅ | 跳过备份 |
| --allow-missing-cwd | ✅ | 允许不存在的目标目录 |

## Sensitive Content Protection

- ✅ 敏感认证文件不会被打包（auth.json, tokens, credentials 等）
- ✅ 会话内容扫描（API keys, tokens, private keys, emails）
- ✅ 敏感内容检测默认阻止导出
- ✅ `--allow-sensitive-content` 可覆盖

## Bundle Integrity

- ✅ MANIFEST.json 包含 checksums 字段
- ✅ checksums/SHA256SUMS.txt 包含每个文件的校验和
- ✅ 不包含 whole-zip hash（避免自指问题）

## Not Yet Supported Capabilities

### Advanced SQLite Support
- ⚠️ 仅支持 state_5.sqlite
- ⚠️ 未扫描 state_*.sqlite 多数据库

### Conflict Resolution
- ❌ --mode merge (未实现)
- ✅ --mode skip
- ✅ --mode overwrite

### Sensitive File Exclusion
- ✅ 本地 backup 不包含敏感文件
- ✅ 排除 auth.json, tokens, credentials

## Evidence

- 31+ existing unittest tests pass
- CLI help shows 7 commands
- codex_workdir_migrate.py implements CodexSessionMigrator class
- .gitignore correctly excludes backups/, reports/, *.sqlite, *.zip