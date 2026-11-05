# Conversation context usage SQL path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the existing conversation-mysql-nas worktree. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 context-usage 的正常请求只读 MySQL，同时保留历史授权字段、错误响应及文件存储路径。

**Architecture:** 路由先选择 indexed reader，避免先通过 ChatManager 读取完整 metadata。HistoryStore 使用 SQL 中的身份和轻量 metadata 授权；如果存在 NAS metadata 且 SQL 缺少 source_id 或 agent_id，则按现有带 pin、复核、清理的完整 Chat 读取路径回退授权。保持 schema、持久化格式和旧接口完整响应不变。

**Tech Stack:** Python、FastAPI、MySQL metadata、现有 BundleStore / RetentionStore、pytest。

---

## 范围与依赖

这是[整体设计](../specs/2026-11-04-conversation-storage-performance-redesign.md)第一阶段的一个独立交付单元。先建立可验证的“轻请求不预读 NAS”路径，再将同类模式用于 summary、详情及内部身份查询；本计划不代表整个性能重构完成。

| 后续工作包 | 前置条件与交付边界 |
|---|---|
| summary 与调用方迁移 | 列清 Console、Tracing、内部查询需要的字段；新增轻接口，保留旧列表；Console 合并详情 metadata 后再替换列表调用 |
| 引用与 SQL 批处理 | 分块查询 publication、批量 pin/dependency 写入、限定复核 IN 大小；保留事务和 GC 保护 |
| NAS 读取与调度 | 同 bundle extent 分组、有界缓存和任务队列；取消时排空正在执行的 IO |
| 写入和 checkpoint | record/prefix/page/full、逐页 pin、完整性复核；最终有效 plan 在 reserve/NAS 前检查容量 |
| 维护和锁优化 | 专用数据库/NAS 基线；证明锁顺序后再调整 scope guard；迁移和备份保持完整校验 |

**修改文件：**

- `src/swe/app/runner/api.py`：context-usage 分发顺序；indexed 错误适配支持调用方指定 missing detail。
- `src/swe/app/conversation_storage/history.py`：numeric read 的必要历史授权回退。
- `tests/unit/app/conversation_storage/test_context_usage.py`：真实路由、HistoryStore 和 metadata 转换配合 SQL/NAS 替身验证，不接业务数据库。
- `tests/integrated/conversation_storage/test_api_mysql.py`：真实 MySQL 下含 NAS metadata 的正常数值请求验收。
- `analysis/playbook/conversation-storage-entry-inventory.md`：记录已落地边界和回退条件。

## Task 1: 路由直接调用 numeric reader

- [x] **Step 1: 新增失败测试。** 注入 `context_usage` reader，同时让 `mgr.get_chat` 抛出 AssertionError。调用真实 `api.get_chat_context_usage`，断言正常返回且管理器未被调用。

```python
async def test_indexed_context_route_skips_full_chat_lookup():
    expected = ContextUsageUnavailable()
    reader = SimpleNamespace(context_usage=AsyncMock(return_value=expected))
    manager = SimpleNamespace(
        get_chat=AsyncMock(side_effect=AssertionError("full Chat read")),
    )
    result = await api.get_chat_context_usage(
        SimpleNamespace(state=SimpleNamespace(user_id="alice")),
        "chat", mgr=manager, session=object(),
        workspace=SimpleNamespace(
            conversation_storage=SimpleNamespace(history=reader),
        ),
    )
    assert result is expected
    manager.get_chat.assert_not_awaited()
```

- [x] **Step 2: 运行确认失败。**

```bash
../../venv/bin/python -m pytest tests/unit/app/conversation_storage/test_context_usage.py::test_indexed_context_route_skips_full_chat_lookup -q
```

预期失败原因为 `AssertionError: full Chat read`。

- [x] **Step 3: 调整分发。** 把 indexed 分支移到完整 Chat 读取前。为通用 adapter 增加 keyword-only `not_found_detail`，默认值仍为 `Chat history not found`；本路由传入 `f"Chat not found: {chat_id}"`。

```python
async def _indexed_history_call(
    operation,
    *args,
    not_found_detail="Chat history not found",
    **kwargs,
):
    from ..conversation_storage.errors import StorageError
    from mysql.connector.errors import Error as MySQLError

    try:
        return await operation(*args, **kwargs)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail=not_found_detail,
        ) from error
    except (StorageError, OSError, RuntimeError, MySQLError) as error:
        raise HTTPException(
            status_code=503,
            detail="Conversation storage unavailable",
        ) from error
```

在 `get_chat_context_usage` 的 docstring 后放置以下分支，并删除原来位于 `mgr.get_chat` 后的同类分支；后续 legacy 分支保留。

```python
indexed = _indexed_history(workspace)
if indexed is not None:
    return await _indexed_history_call(
        indexed.context_usage,
        chat_id,
        not_found_detail=f"Chat not found: {chat_id}",
        authorize=lambda chat: _authorize_chat(request, chat, workspace),
    )
```

- [x] **Step 4: 验证 missing=404、SQL/存储故障=503、跨 user/source/agent=404；文件路径仍读取完整 Chat、授权并读取已提交 state。**

## Task 2: 防止纯 SQL 授权跳过历史字段

- [x] **Step 1: 新增失败测试。** SQL metadata 缺少 source_id，content publication 非空，完整 Chat metadata 包含不同 source_id。使用真实 `_authorize_chat`，断言 numeric reader 返回 404，不能返回数值或 unavailable。分别覆盖 agent_id、超长字符串和回退期间删除。

```python
@pytest.mark.parametrize("field", ["source_id", "agent_id"])
@pytest.mark.parametrize("raw", [None, json.dumps(SNAPSHOT)])
async def test_historical_authorization_on_nas_is_still_enforced(
    numeric_store, field, raw,
):
    store, db, row, bundles = numeric_store
    light = json.loads(row["metadata_text"])
    del light[field]
    row["metadata_text"] = json.dumps(light)
    db.usage["context_usage_text"] = raw
    bundles.read_extent.return_value = json.dumps({field: "x" * 1025}).encode()
    with pytest.raises(HTTPException) as failure:
        await store.context_usage(
            "chat",
            authorize=lambda chat: api._authorize_chat(request(), chat, None),
        )
    assert failure.value.status_code == 404
    store.chats.retentions.finish_reader.assert_awaited_once()
```

本测试使用测试文件中 `numeric_store` 的真实 HistoryStore 和 Chat repository：SQL 替身返回 `chat_row` 及独立 usage 行；NAS 替身返回 JSON 字节；仅 pin 持久化由 AsyncMock 提供。SNAPSHOT 为测试文件中的合法数字快照常量。

- [x] **Step 2: 运行确认因未读取历史字段而失败。**

```bash
../../venv/bin/python -m pytest tests/unit/app/conversation_storage/test_context_usage.py -q
```

- [x] **Step 3: 实现必要回退。** 在事务读取完成后先用 SQL Chat 授权；满足下面条件时，通过 `self.chats.get_chat(chat_id)` 获取完整 Chat 并再次授权。结果为空抛 KeyError。保留原 SQL 行最终复核；成功末尾授权使用已取得的授权对象。

```python
if authorize is not None:
    authorization_chat = self.chats._spec(chat, {})
    authorize(authorization_chat)
    if chat["content_meta_publication_id"] is not None and any(
        key not in authorization_chat.meta for key in ("source_id", "agent_id")
    ):
        authorization_chat = await self.chats.get_chat(chat_id)
        if authorization_chat is None:
            raise KeyError(chat_id)
        authorize(authorization_chat)
```

- [x] **Step 4: 加入成本与兼容断言。** SQL 已含两个字段时，即使 content publication 非空，也不能调用完整读取、pin 或 BundleStore；没有 content publication 时也不回退。错误 owner 在回退前失败。缺失/无效数值保持 unavailable，运行中/invalid 保持 stale，metadata revision 变化保持 503。

## Task 3: 专用数据库验收入口与文档

- [x] **Step 1: 增加真实 MySQL 用例。** 为正常 Chat 写入 `source_id="source"`、`agent_id="agent"` 和未知 custom metadata，保留 NAS content metadata；然后将 bundle read_extent 设置为失败，断言 HTTP context-usage 成功。该用例必须经完整 HTTP 依赖和生产 adapter，禁止把 HistoryStore 替换成 mock。

```python
async def test_http_context_with_content_metadata_does_not_hydrate_nas(
    api_stack,
    monkeypatch,
):
    stack = api_stack
    await history_dataset(stack)
    chat = await stack.manager.get_chat("chat-a")
    chat.meta.update(
        source_id="source",
        agent_id="agent",
        custom={"must_survive": True},
    )
    await stack.manager.update_chat(chat)
    row = await stack.db.fetch_one(
        "SELECT content_meta_publication_id FROM swe_conversation_chats "
        "WHERE external_chat_key=%s",
        (b"chat-a",),
    )
    assert row["content_meta_publication_id"] is not None
    monkeypatch.setattr(
        stack.storage.bundles,
        "read_extent",
        lambda *args, **kwargs: pytest.fail("numeric usage read NAS metadata"),
    )
    result = await stack.client.get("/chats/chat-a/context-usage")
    assert result.status_code == 200
    assert result.json() == {"available": False}
```

- [x] **Step 2: 运行单元回归和专用数据库测试。**

```bash
../../venv/bin/python -m pytest tests/unit/app/conversation_storage/test_context_usage.py tests/unit/app/conversation_storage/test_history.py tests/unit/app/conversation_storage/test_chat_repository.py tests/unit/app/test_context_usage.py tests/unit/routers/test_tracing_chat_history_parity.py -q
../../venv/bin/python -m pytest tests/integrated/conversation_storage/test_api_mysql.py -k context -q
```

单元测试应全通过。集成测试仅使用 `SWE_TEST_DB_*` 指定的专用库；配置缺失则明确报告 skipped，不能算 MySQL 验收通过。

- [x] **Step 3: 更新入口索引。** 记录 context-usage 正常路径不再预读完整 Chat；说明缺失历史授权字段时允许必要 NAS 回退，未优化旧列表/详情/其他身份入口。

- [x] **Step 4: 校验交付差异。**

```bash
git diff --check -- src/swe/app/runner/api.py src/swe/app/conversation_storage/history.py tests/integrated/conversation_storage/test_api_mysql.py analysis/playbook/conversation-storage-entry-inventory.md
```

本轮执行到代码与验证完成；不自动提交或推送混有其他工作项的工作区。提交时只选择本工作包文件。


## 执行结果（2026-11-04）

- 已在修改前观察到路由测试因 `AssertionError: full Chat read` 失败；历史授权测试因未抛 404 失败。
- 最终相关单元/HTTP 回归：**121 passed**；本工作包新增 25 个参数化用例。
- 真实 MySQL context 用例：**5 skipped**，专用测试库未配置；不计为数据库验收通过。
- 修改的四个 Python 文件执行项目 pre-commit，全部适用检查通过；未运行整个脏工作区的格式改写。
- 常见 SQL 正常路径成本断言：三条业务查询，零 NAS read_extent、零 reader pin。这里只验证调用成本，不声称线上延迟或吞吐提升比例。
- schema、存储格式未变；未提交或推送。本工作包完成，整体性能重构的其他工作包仍未实施。
