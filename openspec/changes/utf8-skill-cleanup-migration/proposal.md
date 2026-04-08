## Why

当前 `v1.0.0` 基线在 skill 目录、markdown 文件名、skill 文件树和 API JSON 返回上，仍然会把不安全的路径文本直接暴露出来。表现为：

- `workspace/skills` 或 `skill_pool` 下的异常目录名会导致 manifest、skill 列表和文件树返回不稳定
- markdown 文件名在工作区或 `memory/` 下如果包含坏路径段，会导致文件列表和文件读取接口不稳定
- `load_skill_file()` 只能按原始磁盘路径命中，前端无法可靠使用被清洗后的安全路径
- skill 注册后的 `toolkit.skills[*]["dir"]` 在 prompt / 展示层可能仍携带不安全路径文本

这类问题会让：

- 前端接口返回的 `filename` / `path` / `name` / `references` / `scripts` 不是稳定 UTF-8 文本
- skill / markdown 的真实磁盘名和前端看到的显示名不一致
- 用户在修复异常路径后，需要手动处理 manifest 和读取路径兼容

## What Changes

- 新增 `src/copaw/utils/fs_text.py`，提供统一的 UTF-8 文本清洗工具
- 在 `skills_manager` 中接入 workspace skill 根目录与 pool 根目录的自动重命名
- 在 skill 文件树和 `load_skill_file()` 中支持安全路径显示与 raw/sanitized 反查
- 在 `AgentMdManager` 中接入工作区与 memory markdown 文件名自动重命名
- 在 `CoPawAgent` 注册 skill 后，补一层展示级 path 清洗

## Capabilities

### New Capabilities
- `utf8-safe-fs-text`: 提供通用的文件系统文本清洗能力
- `workspace-skill-dir-utf8-migration`: 自动迁移 workspace skill 目录名
- `pool-skill-dir-utf8-migration`: 自动迁移 `skill_pool` 目录名
- `markdown-filename-utf8-migration`: 自动迁移工作区与 memory markdown 文件名
- `sanitized-skill-file-path-resolution`: 允许 `load_skill_file()` 接受清洗后的安全路径

### Modified Capabilities
- `skill-tree-display`: `references` / `scripts` 返回安全路径段
- `agent-skill-prompt-path-display`: skill 注册后的 `dir` 展示值始终是 UTF-8 安全文本

## Impact

- Affected code:
  - `src/copaw/utils/fs_text.py`
  - `src/copaw/agents/skills_manager.py`
  - `src/copaw/agents/memory/agent_md_manager.py`
  - `src/copaw/agents/react_agent.py`
- Affected behavior:
  - skill 目录首次 reconcile 时可能发生磁盘重命名
  - markdown 文件首次 list/read/write 时可能发生磁盘重命名
  - API 返回的 `filename` / `path` / `name` / `references` / `scripts` 变为 UTF-8 安全文本
- Unchanged behavior:
  - markdown 文件内容编码仍由 `read_text_file_with_encoding_fallback()` 处理，不做自动回写转码
  - 冲突时不删除目录/文件
