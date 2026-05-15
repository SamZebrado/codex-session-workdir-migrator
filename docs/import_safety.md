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

- 仅 JSONL 或 SQLite 之一存在
- 使用 `--mode overwrite` 替换完整记录
- **警告**: 这可能丢失目标机器上的某些更新

## 备份恢复

如果导入出现问题，使用备份恢复：

```bash
# 找到备份目录
ls ~/codex_import_backups/

# 复制备份文件回原位置
cp backup_YYYYMMDD_HHMMSS/sessions/... ~/.codex/sessions/
cp backup_YYYYMMDD_HHMMSS/state_5.sqlite ~/.codex/
```

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
