# 迁移工具设计文档

## 架构概览

```
codex_workdir_migrate.py
├── Commands
│   ├── inspect - 探测会话相关的 cwd 引用
│   ├── plan - 生成迁移计划
│   ├── apply - 执行迁移（含备份和 dry-run）
│   └── verify - 验证迁移结果
└── Core
    ├── CodexSessionMigrator (主类)
    ├── _find_cwd_refs - 寻找 cwd 引用
    ├── _update_cwd_in_jsonl - JSONL 文件更新逻辑
    ├── create_backup - 创建备份
    └── migrate - 执行迁移
```

## 写入策略

### 1. JSONL 文件更新
- 逐行解析，只修改相关路径字段
- 使用临时文件 + `os.replace()` 原子替换
- 备份原文件（通过 create_backup）
- 修改前后记录变更摘要

### 2. SQLite 数据库更新
- 先备份整个数据库文件（含 -wal/-shm）
- 使用事务执行更新
- 执行前先 SELECT 目标行，执行后再 SELECT 确认
- 仅更新 `cwd` 字段和 `sandbox_policy` 中的旧路径引用

### 3. 备份策略
- 创建带时间戳的备份目录: `backup_YYYYMMDD_HHMMSS/`
- 备份所有相关文件:
  - JSONL 会话文件
  - session_index.jsonl
  - state_5.sqlite (以及 -wal/-shm)
- 生成 MANIFEST.json（包含文件路径、大小、SHA256）
- 生成 OPERATION_LOG.md（操作日志）

## 安全特性

1. **干运行模式 (默认)**: 显式需要 `--yes` 才真正写入
2. **自动备份**: apply 时自动创建完整备份
3. **原子操作**: JSONL 使用临时文件替换
4. **事务安全**: SQLite 操作使用事务
5. **验证命令**: verify 检查迁移是否成功

## 命令使用示例

### 1. 探测会话
```bash
./codex_workdir_migrate.py inspect --session <session-id>
```

### 2. 生成迁移计划
```bash
./codex_workdir_migrate.py plan \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path"
```

### 3. 执行迁移（干运行）
```bash
./codex_workdir_migrate.py apply \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path" \
  --backup-dir ./backups
```

### 4. 实际执行迁移
```bash
./codex_workdir_migrate.py apply \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path" \
  --backup-dir ./backups \
  --yes
```

### 5. 验证迁移结果
```bash
./codex_workdir_migrate.py verify \
  --session <session-id> \
  --from "/old/path" \
  --to "/new/path"
```

## 已知限制

1. 迁移前请**关闭所有 codex 相关程序**（CLI/Desktop/VS Code）
2. 不会处理子目录路径（如 session 内执行 cd，如本例中的 /Developing）
3. 工具会精确匹配旧路径，不会自动处理子路径
4. 官方建议的临时方案：`codex resume <session-id> -C /new/path`

## 恢复方法

如果迁移后出现问题，可以从备份目录中手动复制回原文件:
1. 复制 JSONL 文件回原位置
2. 复制 state_5.sqlite 及相关文件回原位置
3. 确保 codex 完全关闭后再操作
