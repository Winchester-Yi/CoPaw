## Context

当前 skill 系统已经基于：

- `workspace/skills`
- `workspace/skill.json`
- `skill_pool`

这三层做同步与运行时解析。`skills_manager` 已经有 manifest reconcile 能力，`AgentMdManager` 负责 markdown 文件读写，`read_text_file_with_encoding_fallback()` 负责内容编码 fallback。

本次需求不是修复“文件内容编码”，而是修复“路径文本不安全”导致的目录名、文件名、skill tree 和 JSON 响应不稳定。

## Goals / Non-Goals

**Goals**

- 为 filesystem-derived 文本提供统一清洗工具
- 自动重命名：
  - workspace skill 根目录中的坏 skill 目录名
  - `skill_pool` 根目录中的坏 skill 目录名
  - 工作区和 `memory/` 下的坏 markdown 文件名
- 冲突时保留原内容并生成唯一新名，不删除旧目录或旧文件
- skill tree 与 markdown 列表对外返回 UTF-8 安全文本
- `load_skill_file()` 支持通过安全路径名读取真实文件
- skill 注册后做展示级 path 清洗

**Non-Goals**

- 不自动回写 markdown 文件内容编码
- 不处理 `thinking / provider / stream`
- 不重构 skill manifest 结构
- 不新增 UI 配置入口

## Decisions

### Decision: `fs_text` 只负责文本清洗，不直接操作磁盘
新增 `sanitize_fs_text()` / `sanitize_text_for_json()` / `sanitize_json_payload()` / `log_sanitized_fs_text()`，把“如何把坏路径文本变成 UTF-8 安全文本”集中到一个模块里。

**Why**
- skill、markdown、API 展示层都需要统一规则
- 把“清洗”和“重命名”解耦，更容易复用和测试

### Decision: skill 根目录重命名接在 reconcile 最前面
`reconcile_workspace_manifest()` 与 `reconcile_pool_manifest()` 一开始就扫描根目录，发现坏 skill 目录名后先重命名，再继续 manifest reconcile。

**Why**
- manifest 内部 key 必须和真实目录名保持一致
- 这样后续所有读取逻辑都可以把“重命名后的真实目录名”当成事实来源

### Decision: skill file tree 只做展示级路径清洗，不重命名 `references/` / `scripts/`
`_directory_tree()` 返回安全路径段；`load_skill_file()` 按 raw/sanitized 两套名字逐级反查真实文件。

**Why**
- 计划要求只自动修复 skill 根目录与 markdown 文件名
- 嵌套文件树只需要展示安全化和读取兼容，不需要动磁盘

### Decision: markdown 文件名在 `AgentMdManager` 层自动迁移
工作区和 `memory/` 目录下的 `.md` 文件，在 `list_*` / `read_*` / `write_*` / `append_working_md()` 之前先扫描并重命名。

**Why**
- `AgentMdManager` 已经是 markdown 读写唯一入口
- 改动集中，不需要分散到 router 层

### Decision: 冲突时统一使用唯一后缀，不删除原文件/目录
当安全名冲突时，skill 目录复用 `suggest_conflict_name()`；markdown 文件则保留 `.md` 后缀并生成带时间戳的新 stem。

**Why**
- 用户明确禁止“删除冲突目录解决问题”
- 时间戳后缀与现有 skill 冲突命名策略一致

### Decision: `react_agent` 只做展示级 skill dir 清洗
新增 `_sanitize_registered_skill_dirs(toolkit)`，对 `toolkit.skills[*]["dir"]` 做 UTF-8 安全文本转换，但不改真实磁盘路径。

**Why**
- skill 实际目录已经在 reconcile 阶段修复
- 这里的目标只是避免 prompt / 工具展示中再次暴露坏路径文本

## Implementation Design

### 1. `fs_text`

新增 `src/copaw/utils/fs_text.py`：

- `SanitizedFsText`
- `sanitize_fs_text(text)`
- `sanitize_text_for_json(text)`
- `sanitize_json_payload(value)`
- `log_sanitized_fs_text(...)`

核心行为：
- UTF-8 可编码字符串原样返回
- surrogate / filesystem-derived 坏文本优先尝试通过常见东亚编码恢复
- 恢复失败时使用 replacement 路径，保证返回值可安全进入 UTF-8 / JSON

### 2. `skills_manager`

新增内部 helper：

- skill 根目录扫描与重命名
- manifest key 迁移
- raw/sanitized 子路径逐级反查
- skill tree 展示名清洗
- metadata 合并，保留迁移前已有的额外 metadata 字段

接入点：

- `reconcile_pool_manifest()`
- `reconcile_workspace_manifest()`
- `_directory_tree()`
- `_read_skill_from_dir()`
- `SkillService.load_skill_file()`

### 3. `AgentMdManager`

新增：

- markdown 文件名扫描与重命名
- raw/sanitized 文件名解析
- `append_working_md()`
- `append_memory_md()`

接入点：

- `list_working_mds()`
- `read_working_md()`
- `write_working_md()`
- `list_memory_mds()`
- `read_memory_md()`
- `write_memory_md()`

### 4. `react_agent`

在 `_register_skills()` 的末尾调用：

- `_sanitize_registered_skill_dirs(toolkit)`

该逻辑只修改 `toolkit.skills[*]["dir"]` 的展示值。

## Risks / Trade-offs

- `references/` / `scripts/` 只做展示级清洗，不改磁盘，意味着极端冲突场景下两个不同 raw 名称可能投影到同一个安全名
  - 当前接受这个风险，不在本次计划中扩展别名映射协议
- markdown 文件名首次触发 list/read/write 时可能发生重命名
  - 这是显式设计目标，不是副作用
- 旧的异常路径如果已经被前端缓存，重命名后需要以后端最新返回路径为准

## Validation Plan

- 新增测试：
  - `tests/unit/utils/test_fs_text.py`
  - `tests/unit/agents/test_utf8_skill_cleanup.py`
  - `tests/unit/agents/test_agent_md_manager_utf8.py`
- 回归测试：
  - `tests/unit/workspace/test_tenant_skill_seeding.py`
  - `tests/unit/agents/test_skill_env_isolation.py`
- 语法验证：
  - `python3 -m py_compile ...`
