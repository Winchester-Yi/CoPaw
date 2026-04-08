## 1. 通用 UTF-8 文本清洗工具

- [x] 1.1 新增 `src/copaw/utils/fs_text.py`
- [x] 1.2 覆盖 surrogate / JSON 递归清洗 / 日志行为测试

## 2. Skill 目录自动迁移

- [x] 2.1 在 workspace reconcile 前扫描并重命名坏 skill 目录名
- [x] 2.2 在 pool reconcile 前扫描并重命名坏 skill 目录名
- [x] 2.3 迁移 manifest key，保留运行时状态字段
- [x] 2.4 对冲突目录生成唯一新名，不删除旧目录

## 3. Skill 文件树与文件读取兼容

- [x] 3.1 `_directory_tree()` 返回安全路径段
- [x] 3.2 `_read_skill_from_dir()` 返回安全 skill 名和安全树
- [x] 3.3 `load_skill_file()` 支持 sanitized path 逐级反查

## 4. Markdown 文件名自动迁移

- [x] 4.1 `AgentMdManager` 在工作区和 memory 下自动重命名坏 `.md` 文件名
- [x] 4.2 `list_*` / `read_*` / `write_*` 兼容清洗后文件名
- [x] 4.3 增加 `append_working_md()` / `append_memory_md()`

## 5. Agent prompt 展示兜底

- [x] 5.1 `react_agent` 在 skill 注册后清洗 `toolkit.skills[*]["dir"]`

## 6. 验证与文档

- [x] 6.1 新增 3 组单元测试
- [x] 6.2 跑通相关老测试回归
- [x] 6.3 编写高精度移植手册
