# UTF-8 路径 / Skill 清洗特性移植手册

日期：2026-04-07

相关主档：
- `openspec/changes/utf8-skill-cleanup-migration/proposal.md`
- `openspec/changes/utf8-skill-cleanup-migration/design.md`
- `openspec/changes/utf8-skill-cleanup-migration/tasks.md`

---

## 1. 手册目的

本手册用于未来升级 CoPaw 基线版本后，快速把“UTF-8 路径 / Skill 清洗”特性补回新版本。

重点不是解释需求，而是明确：

- 改了哪些文件
- 改的是哪些函数 / 类 / 逻辑段
- 每个修改点的目的
- 回补顺序
- 对应测试与验证方式

---

## 2. 推荐回补顺序

建议按下面顺序移植：

1. `src/copaw/utils/fs_text.py`
2. `src/copaw/agents/skills_manager.py`
3. `src/copaw/agents/memory/agent_md_manager.py`
4. `src/copaw/agents/react_agent.py`
5. 新增测试
6. OpenSpec / 实施文档

原因：
- `skills_manager` 和 `AgentMdManager` 都依赖 `fs_text`
- `react_agent` 的展示级清洗依赖 `sanitize_text_for_json()`
- 测试需要建立在最终 helper 和调用点都落地之后

---

## 3. 代码改动明细

### 3.1 `src/copaw/utils/fs_text.py`

**新增内容**

- `SanitizedFsText`
- `sanitize_fs_text(text)`
- `sanitize_text_for_json(text)`
- `sanitize_json_payload(value)`
- `log_sanitized_fs_text(...)`

**目的**

- 统一处理 filesystem-derived 坏文本
- 确保返回值总能安全编码为 UTF-8、进入 JSON

**回补注意点**

- 这里不做磁盘 rename
- 这里只做“文本清洗”，不是“内容转码回写”

**对应测试**

- `tests/unit/utils/test_fs_text.py`

---

### 3.2 `src/copaw/agents/skills_manager.py`

**涉及位置**

- 顶部新增 `fs_text` 相关 import
- `_directory_tree()`
- 新增若干 helper：
  - `_sanitize_file_name_for_disk`
  - `_sanitize_md_name_for_disk`
  - `_build_unique_file_name`
  - `_merge_existing_metadata`
  - `_rename_skill_dirs_to_utf8_safe`
  - `_resolve_child_by_raw_or_sanitized`
  - `_resolve_path_by_raw_or_sanitized`
- `reconcile_pool_manifest()`
- `reconcile_workspace_manifest()`
- `_read_skill_from_dir()`
- `SkillService.load_skill_file()`

**修改目的**

1. **根目录 skill 自动迁移**
   - 在 workspace / pool reconcile 开头，扫描顶层 skill 目录名
   - 若目录名不安全，则重命名为安全名
   - 冲突时使用唯一后缀新名
   - 同步迁移 manifest key

2. **manifest 迁移保留状态**
   - workspace 侧保留：
     - `enabled`
     - `channels`
     - `source`
     - `config`
     - 旧 `metadata` 的附加字段
   - pool 侧保留已有 `config`

3. **文件树展示安全化**
   - `references` / `scripts` 的树结构返回安全路径段

4. **skill file 读取兼容**
   - `load_skill_file()` 支持用 sanitized 路径逐级反查真实文件

**回补注意点**

- 只迁移顶层 skill 目录名，不重命名 `references/` / `scripts/` 的磁盘文件
- `load_skill_file()` 的 sanitized 兼容依赖逐级 raw/sanitized 匹配，不要偷懒改成整串字符串替换
- workspace reconcile 里对 `metadata` 的合并是为了避免 key 迁移时丢掉旧附加字段

**对应测试**

- `tests/unit/agents/test_utf8_skill_cleanup.py`
- `tests/unit/workspace/test_tenant_skill_seeding.py`
- `tests/unit/agents/test_skill_env_isolation.py`

---

### 3.3 `src/copaw/agents/memory/agent_md_manager.py`

**涉及位置**

- 顶部新增：
  - `suggest_conflict_name`
  - `sanitize_fs_text`
- 新增内部方法：
  - `_sanitize_md_filenames()`
  - `_resolve_md_path()`
- 改动方法：
  - `list_working_mds()`
  - `read_working_md()`
  - `write_working_md()`
  - `list_memory_mds()`
  - `read_memory_md()`
  - `write_memory_md()`
- 新增：
  - `append_working_md()`
  - `append_memory_md()`

**修改目的**

- 在 working_dir 和 `memory/` 下自动迁移坏 `.md` 文件名
- 列表返回安全后的 `filename` / `path`
- 读取与写回同时兼容：
  - 原始文件名
  - 清洗后的安全文件名

**回补注意点**

- 只处理顶层 `.md` 文件名
- 不自动修复 markdown 文件内容编码
- 内容编码继续走 `read_text_file_with_encoding_fallback()`
- 冲突时要保留 `.md` 后缀

**对应测试**

- `tests/unit/agents/test_agent_md_manager_utf8.py`

---

### 3.4 `src/copaw/agents/react_agent.py`

**涉及位置**

- 顶部新增 `sanitize_text_for_json`
- `_register_skills()`
- 新增 `@staticmethod _sanitize_registered_skill_dirs(toolkit)`

**修改目的**

- skill 注册完成后，对 `toolkit.skills[*]["dir"]` 做展示级 UTF-8 安全清洗
- 避免 prompt / 工具展示再次暴露坏路径文本

**回补注意点**

- 这里只改展示值，不改真实磁盘路径
- 真正的目录迁移仍由 `skills_manager` reconcile 负责

**对应测试**

- `tests/unit/agents/test_utf8_skill_cleanup.py` 中的 prompt-only path 清洗测试

---

## 4. 测试清单

### 新增测试

- `tests/unit/utils/test_fs_text.py`
- `tests/unit/agents/test_utf8_skill_cleanup.py`
- `tests/unit/agents/test_agent_md_manager_utf8.py`

### 回归测试

- `tests/unit/workspace/test_tenant_skill_seeding.py`
- `tests/unit/agents/test_skill_env_isolation.py`

---

## 5. 本次验证结果

已通过：

```bash
"/mnt/c/Users/lenovo/Desktop/CoPaw/.venv/Scripts/python.exe" -m pytest \
  tests/unit/utils/test_fs_text.py \
  tests/unit/agents/test_utf8_skill_cleanup.py \
  tests/unit/agents/test_agent_md_manager_utf8.py \
  tests/unit/workspace/test_tenant_skill_seeding.py \
  tests/unit/agents/test_skill_env_isolation.py -q
```

结果：
- `25 passed`

建议额外保留的机械验证：

```bash
python3 -m py_compile \
  src/copaw/utils/fs_text.py \
  src/copaw/agents/skills_manager.py \
  src/copaw/agents/memory/agent_md_manager.py \
  src/copaw/agents/react_agent.py
```

---

## 6. 明确不包含的范围

这次特性回补时不要顺手带入：

- markdown 文件内容自动转码回写
- `thinking / provider / stream`
- skill nested file 的磁盘级重命名
- 通过删除目录/文件解决冲突

---

## 7. 回补检查清单

未来升级版本回补时，逐项确认：

- `skills_manager` 是否仍以 top-level skill 目录为事实来源
- workspace / pool manifest reconcile 入口是否仍存在
- `AgentMdManager` 是否仍是 markdown 读写唯一入口
- `react_agent` skill 注册后是否还能统一访问 `toolkit.skills[*]["dir"]`
- 若新版本已内建安全路径处理，优先合并到 upstream 逻辑，不要重复叠 patch

确认以上几点后，再按本手册第 2 节顺序回补即可。
