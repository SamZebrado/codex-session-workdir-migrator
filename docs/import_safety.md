# 导入安全指南

## 概述

本指南帮助你在跨电脑迁移时安全地使用 `import-bundle` 命令。

## 安全流程

### 1. 导出前

1. **确认源机器会话完整**
   ```bash
   python3 codex_workdir_migrate.py inspect --session <session-id>
   ```

2. **确认目标机器 Codex 未运行**
   - 关闭 Codex CLI
   - 关闭 Codex Desktop App
   - 关闭 VS Code（如果安装了 Codex 扩展）

### 2. 导入前

1. **在目标机器上运行 import-plan**
   ```bash
   python3 codex_workdir_migrate.py import-plan \
     --bundle /path/to/bundle.zip \
     --codex-home ~/.codex \
     --map-cwd "OLD=NEW"
   ```

2. **检查 import-plan 输出**
   - 确认 bundle 内容正确
   - 确认目标状态符合预期
   - 检查风险提示

3. **确认备份目录可用**
   ```bash
   mkdir -p ~/codex_import_backups
   ```

### 3. 导入时

1. **先做 dry-run**
   ```bash
   python3 codex_workdir_migrate.py import-bundle \
     --bundle /path/to/bundle.zip \
     --codex-home ~/.codex \
     --map-cwd "OLD=NEW" \
     --backup-dir ~/codex_import_backups \
     --mode skip
   ```

2. **确认无错误后，添加 --yes**
   ```bash
   python3 codex_workdir_migrate.py import-bundle \
     --bundle /path/to/bundle.zip \
     --codex-home ~/.codex \
     --map-cwd "OLD=NEW" \
     --backup-dir ~/codex_import_backups \
     --mode skip \
     --yes
   ```

### 4. 导入后

1. **验证导入结果**
   ```bash
   python3 codex_workdir_migrate.py verify \
     --session <session-id> \
     --from OLD \
     --to NEW \
     --codex-home ~/.codex
   ```

2. **启动 Codex 验证会话可见性**
   ```bash
   codex resume <session-id>
   ```

## 冲突处理

### 场景 1: 目标机器无该会话

- 使用 `--mode skip` 或 `--mode overwrite` 均可
- 工具会创建新会话记录

### 场景 2: 目标机器已有该会话（完整）

- 建议使用 `--mode skip` 跳过
- 如需替换，使用 `--mode overwrite` 并确认备份已创建

### 场景 3: 目标机器已有该会话（残缺）

- JSONL、SQLite 或 index 任意一项存在，都视为会话冲突；残缺状态不是不存在
- 使用 `--mode overwrite` 替换完整记录
- **警告**: 这可能丢失目标机器上的某些更新

## 备份恢复

如果导入出现问题，先关闭所有使用目标 Codex home 的程序，再人工恢复：

1. 找到本次 `import_backup_YYYYMMDD_HHMMSS_ffffff/MANIFEST.json`。备份文件按 basename 平铺保存，不包含原来的 `sessions/` 目录层级。
2. 导入 manifest 的 `files_backed_up` 数组记录每个文件的 `backup_path`、`original_path` 和 `sha256`（本地迁移备份使用 `files` 数组）。先验证备份哈希，并确认原路径属于本次目标 home。
3. 将每个备份文件复制回其对应的原路径；不要把平铺的 JSONL 直接复制到 home 根目录。
4. 备份只保存原先存在的文件。若本次导入新增了 JSONL，人工检查导入结果中的路径并移除对应新增文件，避免恢复后仍保留重复会话。

没有自动回滚命令或跨 JSONL/SQLite/index 的事务保证。活跃 SQLite 的数据库/WAL/SHM 复制不保证一致性；恢复时必须停止写入，先在隔离 fixture 验证。

Bundle 的 SHA256SUMS.txt 和 manifest checksums 仅供人工审计；导入当前不自动校验，不能把导入成功当成完整性认证。

## 风险提示

1. **模式不支持**: `--mode merge` 尚未实现，不要使用
2. **路径映射**: 确认 `--map-cwd` 映射正确，否则 CWD 可能不正确
3. **跨版本**: Codex 版本不同可能导致兼容性问题
4. **会话 ID 冲突**: 如果目标机器已有相同 ID 的会话，必须选择覆盖或跳过

## 禁止事项

- ❌ 不要删除备份目录
- ❌ 不要使用 merge 模式
- ❌ 不要跳过 import-plan 直接 import
- ❌ 不要在 Codex 运行时执行导入

## 目标检查失败与不确定重试

默认 `--on-conflict abort` 会拒绝任何同 ID 的 JSONL、SQLite 或 index 记录。只有成功检查且三者都不存在，才视为目标不存在。数据库锁定、损坏、权限不足、缺少 `threads.id`，或 index 非空行不是带非空字符串 ID 的对象、JSON 损坏或 ID 重复时，`import-plan` 报告 `unknown`，导入在备份、删除和写入之前失败。不会静默修复或丢弃残缺 index；保留原始字节，先人工恢复再重新检查。空行和有效的末行没有换行符可以读取，导入保留无关记录及空行。

JSONL 文件名不证明会话归属。工具扫描 `sessions/` 和 `archived_sessions/`，只按 `session_meta.payload.id` 精确匹配，支持不规则文件名及安全的非 UUID ID。无法读取、JSON 损坏、缺少或冲突的 metadata ID 会阻止检查；其他消息的 `payload.id` 不作为会话身份。目的文件属于其他 ID 时，即使选择 overwrite 也拒绝写入。明确选择 `--mode overwrite` 或 `--on-conflict overwrite` 才允许替换同 ID 的数据，包括 index。

自动 `--on-conflict import-as-new` 每次生成新的 UUID；重复执行是两次有意克隆，不按源内容相似度去重。需要可重启的克隆时，在首次执行前选定 `--new-session-id <explicit-id>`，保存原始 bundle、返回/指定的目标 ID、路径映射、冲突模式、备份和 `imported_files`。同 ID 已存在只证明冲突，不证明内容等价或导入完整。

若响应丢失或导入中断，先用已知目标 ID 检查目标再决定下一步，不要直接再次自动克隆。`success=false` 也可能已经写入 JSONL；工具仍没有跨文件事务或自动回滚保证。部分导入会阻止默认重试；覆盖必须是检查后的明确选择。导入期间目标 home 必须停止其他写入，本次检查不是并发锁或在线快照。
