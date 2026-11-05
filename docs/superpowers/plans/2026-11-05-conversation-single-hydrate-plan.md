# Chat 与 Tracing 单次 metadata hydrate 实施计划

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in the existing conversation-mysql-nas worktree. Execute inline; no sub-agent delegation is required. Steps use checkbox syntax for tracking.

**Goal:** MySQL Chat/Tracing 当前详情与归档页只由领域 reader 完成一次必要 metadata hydrate，保持成功响应、授权、错误及文件后端语义。

**Architecture:** 将 indexed 分支前移到完整 Chat 查询之前，把既有授权规则交给 HistoryStore。Tracing 的私有目标解析函数在 indexed 路径只解析 workspace 和授权规则，不读取完整 Chat；文件路径继续返回已授权 ChatSpec。

**Tech Stack:** Python、FastAPI、pytest、现有 HistoryStore/MySQLChatRepository。无 schema、存储格式或 HTTP 模型变化。

---

## 范围与完成标准

来源：[SQL/NAS 成本设计 §5.1](../specs/2026-11-05-conversation-sql-nas-cost-design.md)。基线 `f66a603ba`。工作包 A 已实现，验证与范围见[实现报告](../reports/2026-11-05-conversation-single-hydrate-implementation.md)。

只修改两处运行时文件及既有相关测试：

| 文件 | 职责/变更 |
| --- | --- |
| `src/swe/app/runner/api.py` | `get_chat` 与 `get_chat_history_page` 前移 indexed 分支 |
| `src/swe/app/routers/tracing.py` | `_target_chat` 跳过 indexed 完整查询；归档 reader 用原请求 chat_id |
| `tests/unit/routers/test_tracing_chat_history_parity.py` | 使用已有 history_client；四个 route 的查询成本、授权、错误及真实 reader 回归 |
| `tests/unit/app/conversation_storage/test_history.py` | 保留组装、occurrence、权限与 reader 清理契约回归 |
| `analysis/playbook/conversation-storage-entry-inventory.md` | 实现后更新完成状态和证据 |

成功条件：indexed 的四个入口不调用 `mgr.get_chat`；reader 仍拿完整 metadata 授权，详情全内容/归档 cursor 语义不变；真实 reader 每请求一次 `_hydrate`；文件后端仍完整查询并授权。不是要求详情零 NAS，也不新增 metadata cache。

## Task 1：先写能暴露重复查询的路由成本回归

- [x] 将以下测试追加到 `tests/unit/routers/test_tracing_chat_history_parity.py`。复用同文件的 `history_client`，并使用文件已导入的 AsyncMock、ChatHistory、ChatArchivePage、SimpleNamespace。

```python
@pytest.mark.parametrize("target", [False, True])
@pytest.mark.parametrize("archive", [False, True])
async def test_indexed_routes_do_not_preload_complete_chat(
    history_client, target, archive,
):
    client, _, chat, workspace = history_client
    workspace.chat_manager.get_chat.side_effect = AssertionError(
        "indexed route duplicated complete metadata hydration",
    )

    async def current(chat_id, *, authorize):
        assert chat_id == chat.id
        authorize(chat)
        return ChatHistory(chat=chat, messages=[], status="running")

    async def archive_page(chat_id, *, before, limit, authorize):
        assert (chat_id, before, limit) == (chat.id, None, 2)
        authorize(chat)
        return ChatArchivePage()

    indexed = SimpleNamespace(
        current=AsyncMock(side_effect=current),
        archive_page=AsyncMock(side_effect=archive_page),
    )
    workspace.conversation_storage = SimpleNamespace(history=indexed)
    params = {"user_id": "target"} if target else {}
    if archive:
        params["limit"] = 2
    prefix = "/tracing/chats" if target else "/chats"
    suffix = "/history" if archive else ""
    response = client.get(f"{prefix}/{chat.id}{suffix}", params=params)
    assert response.status_code == 200
    workspace.chat_manager.get_chat.assert_not_awaited()
    assert indexed.current.await_count == (0 if archive else 1)
    assert indexed.archive_page.await_count == (1 if archive else 0)
```

- [x] 执行红灯命令：

```bash
../../venv/bin/python -m pytest tests/unit/routers/test_tracing_chat_history_parity.py -k do_not_preload_complete_chat -q
```

预期：四项因旧路由仍触发禁止的 `get_chat` 而失败，不能以导入或 fixture 错误作为有效红灯。

## Task 2：Chat indexed 分支前移

- [x] `get_chat` 保留原签名、decorator、docstring，函数正文在 `chat_spec = await mgr.get_chat` 前加入以下分支，并删除原函数中位于授权后的同一 indexed 分支。文件 fallback 的查询、404、授权和 `_build_chat_history` 保持原样。

```python
indexed = _indexed_history(workspace)
if indexed is not None:
    return await _indexed_history_call(
        indexed.current,
        chat_id,
        not_found_detail=f"Chat not found: {chat_id}",
        authorize=lambda chat: _authorize_chat(request, chat, workspace),
    )
```

- [x] `get_chat_history_page` 保留原签名和 decorator，替换正文为以下完整逻辑。归档的 ValueError 映射仍覆盖 indexed 和文件两个分支；参数范围校验仍交给 FastAPI。

```python
indexed = _indexed_history(workspace)
if indexed is not None:
    try:
        return await _indexed_history_call(
            indexed.archive_page,
            chat_id,
            before=before,
            limit=limit,
            not_found_detail=f"Chat not found: {chat_id}",
            authorize=lambda chat: _authorize_chat(
                request, chat, workspace,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

chat_spec = await mgr.get_chat(chat_id)
if not chat_spec:
    raise HTTPException(
        status_code=404, detail=f"Chat not found: {chat_id}",
    )
_authorize_chat(request, chat_spec, workspace)
try:
    return await _archive_page(workspace, chat_spec.id, before, limit)
except ValueError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc
```

- [ ] Chat 两项通过、Tracing 两项仍失败的中间运行未单独执行；实际记录为四入口整体红灯后实施两处路由改动，再验证整体绿灯。此项为过程记录，不影响最终验收。

## Task 3：Tracing 目标解析不重复读正文

- [x] `_target_chat` 保留 workspace 解析及现有 authorize 闭包。将 `storage = ...` 开始到返回值这一尾段替换为：

```python
storage = getattr(workspace, "conversation_storage", None)
if getattr(storage, "history", None) is not None:
    return workspace, None, authorize

chat = await workspace.chat_manager.get_chat(chat_id)
if chat is None:
    raise HTTPException(
        status_code=404, detail=f"Chat not found: {chat_id}",
    )
authorize(chat)
return workspace, chat, authorize
```

该私有函数只有当前详情和归档两个调用者。None 仅表示 indexed reader 将执行 Chat 查询，不能成为外部成功响应或授权通过的凭据。

- [x] `get_user_chat_history` 的 indexed 分支将第二个参数从 `chat.id` 改为请求参数 `chat_id`。`get_user_chat` 已使用 `chat_id`，无需改动。两个文件 fallback 仍使用已获取的 ChatSpec。

```python
return await _indexed_history_call(
    indexed.archive_page,
    chat_id,
    before=before,
    limit=limit,
    authorize=authorize,
)
```

- [x] 复跑 Task 1，预期四项全部通过。不要调用 obsolete `tracing_view` 替代 `current`；它会丢失之前已恢复的内容/响应语义。

## Task 4：调整被旧多读阶段绑定的测试，保留保护能力

- [x] 将现有 `test_indexed_target_checks_at_each_read` 整体替换为以下测试；不再参数化已删除的 lookup，改成 reader 的授权前后两次调用，保留 suffix、id/user/source/agent 的全部组合。

```python
@pytest.mark.parametrize("suffix", ["", "/history"])
@pytest.mark.parametrize("field", ["id", "user_id", "source_id", "agent_id"])
@pytest.mark.parametrize("authorize_phase", ["first", "last"])
async def test_indexed_target_checks_at_each_read(
    history_client, suffix, field, authorize_phase,
):
    client, _, chat, workspace = history_client
    changed = chat.model_copy(deep=True)
    if field in ("id", "user_id"):
        setattr(changed, field, "foreign")
    else:
        changed.meta[field] = "foreign"
    workspace.chat_manager.get_chat.side_effect = AssertionError(
        "indexed route must authorize through its reader",
    )

    async def read(*args, authorize, **kwargs):
        authorize(changed if authorize_phase == "first" else chat)
        authorize(changed if authorize_phase == "last" else chat)
        return ChatArchivePage() if suffix else ChatHistory(chat=chat)

    workspace.conversation_storage = SimpleNamespace(
        history=SimpleNamespace(
            current=AsyncMock(side_effect=read),
            archive_page=AsyncMock(side_effect=read),
        ),
    )
    response = client.get(
        f"/tracing/chats/{chat.id}{suffix}",
        params={"user_id": "target"},
        headers={"X-Source-Id": "source", "X-Agent-Id": "agent"},
    )
    assert response.status_code == 404
    workspace.chat_manager.get_chat.assert_not_awaited()
```

仍要求每项 404；测试拒绝的是 reader 内真实授权阶段，而不是被删除的额外查询。manager 设置禁止调用，并在所有项断言未 await。

- [x] 将 `test_indexed_tracing_storage_failure_is_unavailable` 整体替换为以下跨 Chat/Tracing 的测试。删除 lookup phase，保留原五类异常并增加 missing；不能保留一个用不到的 lookup fixture 并让错误用例实际返回 200。

```python
@pytest.mark.parametrize("target", [False, True])
@pytest.mark.parametrize("suffix", ["", "/history"])
@pytest.mark.parametrize(
    "error,status",
    [
        (StorageUnavailable("NAS unavailable"), 503),
        (ConflictError("revision changed"), 503),
        (OSError("read failed"), 503),
        (RuntimeError("unavailable"), 503),
        (MySQLError("database unavailable"), 503),
        (KeyError("missing"), 404),
    ],
)
async def test_indexed_route_reader_failure_mapping(
    history_client, target, suffix, error, status,
):
    client, _, chat, workspace = history_client
    workspace.chat_manager.get_chat.side_effect = AssertionError(
        "indexed failure must not fall back to complete lookup",
    )
    workspace.conversation_storage = SimpleNamespace(
        history=SimpleNamespace(
            current=AsyncMock(side_effect=error),
            archive_page=AsyncMock(side_effect=error),
        ),
    )
    prefix = "/tracing/chats" if target else "/chats"
    params = {"user_id": "target"} if target else {}
    response = client.get(f"{prefix}/{chat.id}{suffix}", params=params)
    assert response.status_code == status
    if status == 503:
        assert response.json()["detail"] == "Conversation storage unavailable"
    workspace.chat_manager.get_chat.assert_not_awaited()
```

- [x] 增加 missing/cursor/取消及普通 Chat 错误映射。使用与 Task 1 相同的 history_client，但读回调设置以下 side_effect 和期望结果：

| 分支 | reader 行为 | 验证 |
| --- | --- | --- |
| Chat/Tracing 当前与归档 | `KeyError(chat.id)` | 404 |
| Chat/Tracing 归档 | `ValueError("invalid history cursor")` | 422 |
| Chat 当前/归档 | 原有五类存储异常 | 503，detail 为 `Conversation storage unavailable` |
| Tracing 当前直接函数调用 | `asyncio.CancelledError()` | 向上传播，不能转换 503 |

取消测试直接调用已有路由函数，不用 TestClient 对取消的线程封装行为作断言；已有 `test_indexed_tracing_real_reader_keeps_current_history` 已覆盖直接取消及 `_finish`，应继续复用。

归档 ValueError 的新增测试：

```python
@pytest.mark.parametrize("target", [False, True])
async def test_indexed_archive_maps_invalid_cursor(history_client, target):
    client, _, chat, workspace = history_client
    workspace.chat_manager.get_chat.side_effect = AssertionError(
        "indexed archive must not preload complete metadata",
    )
    workspace.conversation_storage = SimpleNamespace(
        history=SimpleNamespace(
            archive_page=AsyncMock(
                side_effect=ValueError("invalid history cursor"),
            ),
        ),
    )
    prefix = "/tracing/chats" if target else "/chats"
    params = {"user_id": "target"} if target else {}
    params["before"] = "invalid"
    response = client.get(f"{prefix}/{chat.id}/history", params=params)
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid history cursor"
    workspace.chat_manager.get_chat.assert_not_awaited()
```

- [x] 为已有真实 reader 测试补充计数断言：每次请求 `store.chats._hydrate` 只 await 一次，manager.get_chat 未 await。测试同时请求 Chat 和 Tracing 后累计两次。保留 memory/task/error/anchor、hidden-context 脱敏、archive boundary、revision 冲突和 pin 清理的已有断言。

## Task 5：回归、自审与记录

- [x] 运行以下回归：

```bash
../../venv/bin/python -m pytest \
  tests/unit/routers/test_tracing_chat_history_parity.py \
  tests/unit/routers/test_tracing_chats_api.py \
  tests/unit/app/conversation_storage/test_history.py \
  tests/unit/app/test_chat_compaction_history_api.py \
  tests/unit/app/test_chat_history_message_identity.py \
  tests/unit/app/test_model_call_error_history.py \
  tests/unit/app/test_context_usage.py -q
```

预期无失败；Task 1 的四项应作为真实入口成本回归常驻。文件路径的 parity、missing/owner/source/agent 和归档参数测试全部保留。

- [x] 对实际改动文件执行 pre-commit、`git diff --check`。检查没有额外 metadata cache，没有放宽 authorize 闭包，没有删减 ChatHistory 字段，没有让 indexed 存储错误落回文件后端。
- [x] 在入口索引和实现报告记录成本边界：减少的是路由前置一次完整 metadata 查询/读取；详情必要正文和最终复核仍然存在。无真实 NAS 验证时不能给出延迟收益。
- [ ] 用户要求提交时只暂存本工作包文件；本轮未请求提交，未暂存；保留其他工作区改动，不读取或修改 `src/swe/config/envs/dev.json`。

## 已完成的计划前核对

2026-11-05：现有 Tracing parity/chats API、History 和 SourceMessages 四组测试基线 **70 passed，1.02 秒**。这是未实现前基线，不是以上新增成本回归或真实 MySQL/NAS 验收。
