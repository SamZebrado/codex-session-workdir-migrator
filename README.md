# Codex Session Workdir Migrator

⚠️ **重要声明**：这是一个**非官方、不受支持**的工具。使用风险自负。
⚠️ 该工具仅适用于特定的 Codex 版本，架构可能随时变化。
⚠️ This tool only edits local Codex metadata on your own machine and is not affiliated with OpenAI.

## 核心功能

本工具提供两种迁移模式：

1. **本地迁移（Local Migration）**：在同一台机器上将 Codex 会话的工作目录从旧路径迁移到新路径
2. **跨电脑迁移（Portable Cross-machine Migration）**：将会话打包为 ZIP，在不同电脑之间迁移

## 默认行为（structured-only）

默认采用保守策略：**只改结构化状态，不改历史自然语言文本**。

默认只会修改：
- JSONL `session_meta.payload.cwd`
- JSONL `turn_context.payload.cwd`
- SQLite `threads.cwd`
- SQLite `threads.sandbox_policy`

默认不会修改：
- message 文本中的 `<environment_context><cwd>...</cwd>`
- `function_call.arguments.workdir`
- 普通 user/assistant 文本

## 扩展开关（默认关闭）

- `--include-function-workdir`
  - 扩大范围，允许改 `function_call.arguments.workdir`
- `--include-environment-context`
  - 扩大范围，允许改 `<environment_context><cwd>...</cwd>` 文本块
- `--rewrite-prefix-paths`
  - 风险模式，允许前缀路径重写（如 `old/sub` -> `new/sub`）
  - 默认关闭，仅精确匹配 `old_cwd`

## 备份模式

- `--backup-mode minimal`（默认）
  - 仅备份本次会改到的文件
- `--backup-mode full`
  - 备份会话 JSONL、`session_index.jsonl`、SQLite 主文件和 WAL/SHM

## 运行要求

- Python 3.10+（当前代码只使用 Python 标准库）
- 不需要 `pytest`
- 不需要 repo-local `.venv`
- 不需要额外安装包

推荐测试命令：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -q
```

---

# Part 1: 本地迁移命令

## inspect

```bash
./codex_workdir_migrate.py inspect --session <session-id> --codex-home <fixture-codex-home>
```

## plan

```bash
./codex_workdir_migrate.py plan \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path" \
  --codex-home <fixture-codex-home>
```

`plan` 会输出：
- JSONL 分类统计：
  - `session_meta.cwd`
  - `turn_context.cwd`
  - `function_call.workdir`
  - `environment_context.cwd`
- SQLite 是否会改：
  - `threads.cwd`
  - `threads.sandbox_policy`

## apply（默认 dry-run）

```bash
./codex_workdir_migrate.py apply \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path" \
  --backup-dir ./backups \
  --backup-mode minimal \
  --codex-home <fixture-codex-home>
```

真实写入需加 `--yes`：

```bash
./codex_workdir_migrate.py apply ... --yes
```

dry-run 会输出：
- 哪些文件会改（`modified_files`）
- 每类改动计数（`summary`）

## verify

```bash
./codex_workdir_migrate.py verify \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path" \
  --codex-home <fixture-codex-home>
```

`verify` 与 `plan/apply` 使用同一策略口径，避免漏检或误报。

---

# Part 2: 跨电脑迁移命令

本部分功能用于在不同电脑之间迁移 Codex 会话。

## export-bundle

将会话打包为 portable ZIP：

```bash
./codex_workdir_migrate.py export-bundle \
  --session <session-id> \
  --codex-home ~/.codex \
  --out /tmp/codex_session_<session-id>_migration_bundle.zip
```

Bundle 结构：
```
bundle.zip/
  MANIFEST.json
  README_IMPORT.md
  inspection/
    inspect_report.json
  sessions/
    raw_jsonl/
      rollout-....jsonl
  index/
    session_index_records.jsonl
  sqlite/
    state_db_matching_rows.json
  checksums/
    SHA256SUMS.txt
```

**安全说明**：Bundle 不包含认证文件（auth.json, tokens, credentials 等）。

## import-plan

在目标机器上生成导入计划（只读）：

```bash
./codex_workdir_migrate.py import-plan \
  --bundle /tmp/codex_session_<session-id>_migration_bundle.zip \
  --codex-home ~/.codex \
  --map-cwd "/old/path=/new/path"
```

`import-plan` 会输出：
- Bundle 内容摘要
- 目标机器是否已有该会话
- 计划修改的文件
- CWD 映射详情
- 风险提示

## import-bundle

执行跨电脑导入：

```bash
./codex_workdir_migrate.py import-bundle \
  --bundle /tmp/codex_session_<session-id>_migration_bundle.zip \
  --codex-home ~/.codex \
  --map-cwd "/old/path=/new/path" \
  --backup-dir ./import_backups \
  --mode skip
```

**Legacy 冲突策略（`--mode`）**：
- `--mode skip`（默认）：如果目标机器已有该会话，则跳过
- `--mode overwrite`：备份后覆盖已有会话

**推荐冲突策略（`--on-conflict`）**：
- `--on-conflict abort`（默认）：如果目标机器已有该会话，则报错退出
- `--on-conflict overwrite`：备份后覆盖已有会话
- `--on-conflict import-as-new`：即使目标机器已有该会话，导入为新会话

**导入时新建 session ID**：
- `--new-session-id auto`：自动生成新 session ID
- `--new-session-id <explicit-id>`：使用指定的 session ID

**备份控制**：
- `--no-backup`：跳过备份（危险，仅在确信数据可恢复时使用）

**其他选项**：
- `--allow-missing-cwd`：允许源 bundle 中缺少 CWD 信息的情况

**Preflight 检查**：
- 敏感内容扫描（邮箱、云盘路径、中文路径）
- SQLite schema 完整性检查（NOT NULL 约束风险）
- 目标 session ID 冲突检测
- SHA256SUMS.txt 供人工审计，自动校验功能尚未实现

**真实写入需加 `--yes`**：
```bash
./codex_workdir_migrate.py import-bundle ... --yes
```

## CWD 映射说明

`--map-cwd` 支持：
- 路径包含空格
- 路径包含中文字符
- 路径包含 Google Drive 特殊路径

**注意**：当前版本仅支持一个 `--map-cwd` 映射。

---

# Part 3: 安全建议

- 执行真实迁移前先做 `plan` 和 dry-run `apply`
- 如需扩大修改范围，显式添加开关，不建议默认开启
- 对真实 `~/.codex` 操作前，先在 fixture 上完整验证流程
- 跨电脑迁移前，先运行 `import-plan` 查看导入计划
- 使用 `--mode overwrite` 时，工具会自动创建备份

## 敏感文件排除

以下文件**不会**被打包进 bundle：
- `auth.json`
- `tokens`
- `credentials`
- `cookies`
- `keychain`
- `.netrc`
- `.env`
- 其他疑似认证凭据

---

# Part 4: 可复现 smoke flow（不碰真实 ~/.codex）

下面的流程只会写入 `/private/tmp/.../codex_home` 这个 fake Codex home。

```bash
SMOKE_ROOT="$(mktemp -d /private/tmp/codex-migrator-smoke.XXXXXX)"
python3 tests/make_smoke_fixture.py "$SMOKE_ROOT"
```

脚本会输出本轮要使用的 `CODEX_HOME`、`SESSION_ID`、`OLD_CWD`、`NEW_CWD`、`BACKUP_DIR`。随后用这些值运行：

### 本地迁移 smoke flow

```bash
python3 codex_workdir_migrate.py inspect \
  --session smoke-session \
  --codex-home "$SMOKE_ROOT/codex_home"

python3 codex_workdir_migrate.py plan \
  --session smoke-session \
  --from /tmp/codex-migrator-old \
  --to "$SMOKE_ROOT/new_workdir" \
  --codex-home "$SMOKE_ROOT/codex_home"

python3 codex_workdir_migrate.py apply \
  --session smoke-session \
  --from /tmp/codex-migrator-old \
  --to "$SMOKE_ROOT/new_workdir" \
  --backup-dir "$SMOKE_ROOT/backups" \
  --backup-mode minimal \
  --codex-home "$SMOKE_ROOT/codex_home"

python3 codex_workdir_migrate.py verify \
  --session smoke-session \
  --from /tmp/codex-migrator-old \
  --to "$SMOKE_ROOT/new_workdir" \
  --codex-home "$SMOKE_ROOT/codex_home"
```

`apply` 不加 `--yes` 是 dry-run，不写 fixture 文件；因此 dry-run 后的 `verify` 应返回失败并报告仍存在的 structured-only old cwd 引用。要在 fixture 上验证真实写入闭环，可继续运行：

```bash
python3 codex_workdir_migrate.py apply \
  --session smoke-session \
  --from /tmp/codex-migrator-old \
  --to "$SMOKE_ROOT/new_workdir" \
  --backup-dir "$SMOKE_ROOT/backups" \
  --backup-mode minimal \
  --codex-home "$SMOKE_ROOT/codex_home" \
  --yes

python3 codex_workdir_migrate.py verify \
  --session smoke-session \
  --from /tmp/codex-migrator-old \
  --to "$SMOKE_ROOT/new_workdir" \
  --codex-home "$SMOKE_ROOT/codex_home"
```

### 跨电脑迁移 smoke flow

```bash
# Step 1: 在 source 机器上导出 bundle
python3 codex_workdir_migrate.py export-bundle \
  --session smoke-session \
  --codex-home "$SMOKE_ROOT/codex_home" \
  --out /tmp/bundle.zip

# Step 2: 在 target 机器上生成导入计划
python3 codex_workdir_migrate.py import-plan \
  --bundle /tmp/bundle.zip \
  --codex-home "$SMOKE_ROOT/target_codex_home" \
  --map-cwd "/tmp/codex-migrator-old=$SMOKE_ROOT/new_workdir"

# Step 3: 执行导入（dry-run）
python3 codex_workdir_migrate.py import-bundle \
  --bundle /tmp/bundle.zip \
  --codex-home "$SMOKE_ROOT/target_codex_home" \
  --map-cwd "/tmp/codex-migrator-old=$SMOKE_ROOT/new_workdir" \
  --backup-dir "$SMOKE_ROOT/import_backups" \
  --mode skip

# Step 4: 真实写入
python3 codex_workdir_migrate.py import-bundle \
  --bundle /tmp/bundle.zip \
  --codex-home "$SMOKE_ROOT/target_codex_home" \
  --map-cwd "/tmp/codex-migrator-old=$SMOKE_ROOT/new_workdir" \
  --backup-dir "$SMOKE_ROOT/import_backups" \
  --mode skip \
  --yes
```

真实迁移时如果省略 `--codex-home`，工具会默认指向 `~/.codex`；在测试和 smoke 中始终传入临时 fixture 的 `--codex-home`。

---

# 已知限制

详见 [docs/known_limits.md](docs/known_limits.md)

---

# 文档索引

- [current_capability_audit.md](docs/current_capability_audit.md) - 当前能力审计
- [bundle_format.md](docs/bundle_format.md) - Bundle 格式说明
- [import_safety.md](docs/import_safety.md) - 导入安全指南
- [known_limits.md](docs/known_limits.md) - 已知限制
- [migration_design.md](docs/migration_design.md) - 迁移设计文档
