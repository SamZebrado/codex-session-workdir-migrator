# 已知限制

## 当前版本

v0.2.1

## 已实现功能

### 本地迁移
- ✅ inspect - 检测会话的 cwd 引用
- ✅ plan - 生成迁移计划
- ✅ apply - 执行迁移（支持 dry-run）
- ✅ verify - 验证迁移结果

### 跨电脑迁移
- ✅ export-bundle - 导出会话为 ZIP
- ✅ import-plan - 生成导入计划（只读）
- ✅ import-bundle - 执行导入（支持 dry-run）
- ✅ 敏感文件排除
- ✅ 敏感内容扫描
- ✅ 导出 SHA256 校验和供人工审计（导入不自动校验）

### 安全特性
- ✅ --yes 需要 --backup-dir 或 --no-backup
- ✅ 目标 cwd 检查（默认不存在则失败）
- ✅ --mode overwrite 删除旧 JSONL 文件
- ✅ SQLite 表检查

## 已知限制

### 1. SQLite 数据库

**限制**: 仅支持 `state_5.sqlite`

**说明**: Codex 可能使用多个 `state_*.sqlite` 文件，但当前工具只处理 `state_5.sqlite`。

**影响**:
- 如果会话数据存储在其他 state 文件中，可能不会被迁移
- Codex 未来版本可能更改数据库结构

**建议**: 在执行迁移前，先用 `inspect` 确认会话数据位置。

### 2. 冲突处理

**限制**: `--mode merge` 尚未实现

**说明**: 当前支持 `skip` 和 `overwrite` 模式。

**影响**:
- 无法智能合并两个会话版本
- 如需合并，需要手动处理

**建议**: 使用 `--mode overwrite` 时，确保备份完整。

### 3. CWD 映射

**限制**: 仅支持单一映射

**说明**:
- `--map-cwd` 目前只支持一个 OLD=NEW 映射

**影响**:
- 多个工作目录需要多次迁移

**建议**: 在 `plan` 阶段确认所有需要迁移的路径。

### 4. 历史文本

**限制**: 默认不修改历史消息文本

**说明**: 以下内容默认不会被修改：
- message 文本中的 `<environment_context><cwd>...</cwd>`
- `function_call.arguments.workdir` 中的历史值
- 用户消息中的路径引用

**影响**:
- 某些对话历史可能显示旧路径
- 需要使用 `--include-function-workdir` 或 `--include-environment-context` 显式开启

**建议**: 如需完整迁移，使用扩展标志。

### 5. Codex 版本兼容性

**限制**: 仅在 Codex CLI 0.122.0 上测试

**说明**: Codex 架构可能随时变化。

**影响**:
- 未来版本可能不兼容
- 桌面客户端可能使用不同存储

**建议**: 在执行重要迁移前，备份所有数据。

### 6. 会话 ID 格式

**限制**: 导入 ID 必须可安全用于文件名；不要求 UUID 格式

**说明**: 导入支持 UUID 和非 UUID ID。ID 必须以 ASCII 字母或数字开头，其余字符仅限 ASCII 字母、数字、点、下划线和连字符。空 ID、绝对路径、路径分隔符和控制字符会被拒绝。

**影响**:
- 不符合上述文件名规则的 bundle ID 或显式新 ID 无法导入
- JSONL 目标路径会在写入前检查；指向 Codex home 外的符号链接目标会被拒绝

**建议**: 在 `inspect` 阶段确认会话 ID 格式正确。

### 7. 多工作目录

**限制**: 单次迁移仅支持一个工作目录

**说明**: 如果会话使用多个工作目录，需要多次迁移。

**影响**:
- 需要多次运行 `apply`
- 每次需要不同的 `--from` 参数

**建议**: 在 `plan` 阶段确认所有需要迁移的路径。

### 8. 文件系统权限

**限制**: 依赖文件系统权限

**说明**: 工具需要读写 `~/.codex` 目录的权限。

**影响**:
- 权限不足时迁移会失败
- 无法处理符号链接或网络挂载

**建议**: 确保有足够的文件系统权限。

## 未验证场景

以下场景尚未在真实环境中验证：

- ⚠️ Codex Desktop App 的会话迁移
- ⚠️ VS Code 扩展的会话迁移
- ⚠️ 跨操作系统迁移（macOS ↔ Linux ↔ Windows）
- ⚠️ 大型会话（>100MB JSONL）处理
- ⚠️ 并发迁移（多个会话同时迁移）

## 常见问题

### Q: 迁移后 Codex 无法启动？

A: 先关闭所有访问目标 Codex home 的程序。备份文件按 basename 平铺保存，不能把整个备份目录直接复制到 Codex home。检查备份的 `MANIFEST.json`：本地迁移使用 `files`，导入备份使用 `files_backed_up`；每项记录 `backup_path`、`original_path` 和 `sha256`。人工确认原路径属于要恢复的目标 home、备份校验和正确，再将各文件复制回各自的原路径（JSONL 通常位于多层 `sessions/` 目录）。

恢复新导入的数据还可能需要人工删除本次新增的 JSONL；备份仅包含原先存在的文件，不是自动回滚工具。避免在 Codex 或 SQLite 仍运行时复制数据库/WAL/SHM。当前没有跨文件事务回滚或在线数据库备份保证。

### Q: 会话在 Codex 中不可见？

A: 检查 `verify` 结果，确认 SQLite 记录正确：
```bash
python3 codex_workdir_migrate.py verify --session <id> ...
```

### Q: 旧路径仍然出现在新会话中？

A: 使用扩展标志重新迁移：
```bash
python3 codex_workdir_migrate.py apply ... \
  --include-function-workdir \
  --include-environment-context
```

### Q: import-bundle 失败提示目标目录不存在？

A: 确保目标目录存在，或使用 `--allow-missing-cwd`：
```bash
python3 codex_workdir_migrate.py import-bundle ... --allow-missing-cwd
```

### Q: import-bundle 失败提示需要 --backup-dir？

A: 提供备份目录或使用 `--no-backup`：
```bash
python3 codex_workdir_migrate.py import-bundle ... --backup-dir ./backups --yes
# 或
python3 codex_workdir_migrate.py import-bundle ... --no-backup --yes
```