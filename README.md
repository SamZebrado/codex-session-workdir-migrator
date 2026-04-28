# Codex Session Workdir Migrator

⚠️ **重要声明**：这是一个**非官方、不受支持**的工具。使用风险自负。
⚠️ 该工具仅适用于特定的 Codex 版本，架构可能随时变化。
⚠️ This tool only edits local Codex metadata on your own machine and is not affiliated with OpenAI.

将 Codex 会话的工作目录引用从 `old_cwd` 迁移到 `new_cwd`，默认采用保守策略：**只改结构化状态，不改历史自然语言文本**。

## 默认行为（structured-only）

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

## 命令

### inspect

```bash
./codex_workdir_migrate.py inspect --session <session-id> --codex-home <fixture-codex-home>
```

### plan

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

### apply（默认 dry-run）

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

### verify

```bash
./codex_workdir_migrate.py verify \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path" \
  --codex-home <fixture-codex-home>
```

`verify` 与 `plan/apply` 使用同一策略口径，避免漏检或误报。

## 安全建议

- 执行真实迁移前先做 `plan` 和 dry-run `apply`
- 如需扩大修改范围，显式添加开关，不建议默认开启
- 对真实 `~/.codex` 操作前，先在 fixture 上完整验证流程

## 可复现 smoke flow（不碰真实 ~/.codex）

下面的流程只会写入 `/private/tmp/.../codex_home` 这个 fake Codex home。

```bash
SMOKE_ROOT="$(mktemp -d /private/tmp/codex-migrator-smoke.XXXXXX)"
python3 tests/make_smoke_fixture.py "$SMOKE_ROOT"
```

脚本会输出本轮要使用的 `CODEX_HOME`、`SESSION_ID`、`OLD_CWD`、`NEW_CWD`、`BACKUP_DIR`。随后用这些值运行：

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

真实迁移时如果省略 `--codex-home`，工具会默认指向 `~/.codex`；在测试和 smoke 中始终传入临时 fixture 的 `--codex-home`。
