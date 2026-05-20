# Suggestions 猜你想问可插拔式设计

> 设计日期: 2026-04-28
> 状态: 待评审

> 2026-05-19 更新：前端 external/mock 生成路径已被后端权威生成路径取代。当前设计以 `runner.py` 在回答完成后调用 `generate_suggestions()` 为准，前端只轮询 `/console/suggestions` 获取后端已生成结果。`qa_extraction_only` 保留为历史兼容模式，不再是默认推荐路径。

## 1. 背景与目标

当前 Suggestions（猜你想问）功能存在以下问题：

1. 后端生成逻辑被禁用（`runner.py` 中 `if False:`），前端使用 Mock 模式
2. Q&A 内容提取分散在前端本地实现，缺乏统一的智能提取逻辑
3. 异步环境下问题和答案可能错配，缺乏唯一标识机制
4. 功能不可配置切换，无法灵活适配不同场景

本次目标：

- 后端在模型回答完毕后，统一提取 Q&A 关键内容（不超过1500字）
- 通过用户问题匹配机制避免异步错配
- 前端从后端查询提取内容，调用外部接口生成猜你想问
- 实现可插拔式设计，通过配置开关控制行为
- 最小化代码变更，不影响原有聊天链路

### 1.1 本期范围

**本期实现：**

- 后端 Q&A 内容提取钩子（runner.py finally 块）
- 按用户问题 hash 存储和查询 Q&A 内容
- 后端 API：POST /console/suggestions/qa-content
- 前端：从当前 request/response card 本地提取 Q&A 后调用外部 suggestions API
- 配置开关：SuggestionMode（disabled/backend_generate/qa_extraction_only）
- Fallback 机制：本地或外部 API 未配置时使用 mock suggestions

**本期不实现：**

- 完全替代前端本地提取逻辑（保留 fallback）
- 多租户隔离的持久化存储（仍使用内存存储）
- Q&A 内容的历史记录和审计
- SSE/WebSocket 实时推送 suggestions

---

## 2. 设计方案

### 2.1 匹配机制

**核心思路：用用户问题作为唯一标识符**

- 后端存储 Q&A 到 `(chat_id, user_message_hash)`
- 前端发送 `(chat_id, user_message)` 查询
- 后端根据用户问题 hash 匹配找到对应的 Q&A

**优点：**

- 不需要额外生成和传递 turn_id
- 不需要修改 SSE 通信
- 实现简单，最小化代码变更
- 前端已有提取用户问题的函数 (`extractUserMessageText`)

**解决重复问题：**

- 使用 `user_message_hash` 而非原文作为键
- 存储时记录时间戳，查询时取最新一条（120秒内）

### 2.2 数据结构

**后端存储结构：**

```python
# Q&A 内容存储：chat_id -> {user_message_hash: QAContentEntry}
_qa_content_store: Dict[str, Dict[str, QAContentEntry]] = {}

class QAContentEntry:
    user_message: str           # 提取后的用户问题（用于匹配）
    user_message_hash: str      # 用户问题的 hash（作为键）
    assistant_response: str     # 提取后的助手回答
    ts: float                   # 存储时间戳
    tenant_id: str              # 租户 ID
```

**配置结构：**

```python
class SuggestionMode(str, Enum):
    DISABLED = "disabled"                  # 完全禁用
    BACKEND_GENERATE = "backend_generate"  # 后端生成（原有模式）
    QA_EXTRACTION_ONLY = "qa_extraction_only"  # 仅提取 Q&A（新模式）

class SuggestionConfig(BaseModel):
    enabled: bool = True
    mode: SuggestionMode = SuggestionMode.QA_EXTRACTION_ONLY
    qa_content_total_max_length: int = 1500  # Q&A 总长度上限
    qa_content_max_age_seconds: int = 120    # 存储有效期
```

---

## 3. 数据流

```
用户提问 → 模型响应 → 响应完成
                           │
           ┌───────────────┴───────────────┐
           │                               │
           ▼                               ▼
    后端: runner.py finally         前端: finishResponse()
    提取 Q&A 关键内容                │
    存储: (chat_id, user_msg_hash)   │
                                    ▼
                          pollSuggestions()
                          │
                          ▼
                          POST /suggestions/qa-content
                          { chat_id, user_message }
                          │
                          ▼
                          后端匹配 user_message_hash
                          返回 Q&A 内容
                          │
                          ▼
                          前端调用外部 API
                          生成 suggestions
                          │
                          ▼
                          渲染到回答中
```

---

## 4. 实现细节

### 4.1 后端钩子（runner.py）

在 `finally` 块中添加 Q&A 提取逻辑：

```python
if (
    agent_config.running.suggestions.enabled
    and agent_config.running.suggestions.mode == SuggestionMode.QA_EXTRACTION_ONLY
    and chat is not None
):
    assistant_response = _extract_assistant_response(agent)
    user_message = query

    if assistant_response and user_message:
        from ..suggestions.service import extract_key_content
        from ..suggestions.store import store_qa_content

        # 提取关键内容
        extracted_user = user_message[:200]
        extracted_assistant = extract_key_content(
            assistant_response,
            max_length=min(1500 - len(extracted_user), 500),
        )

        await store_qa_content(
            chat_id=chat.id,
            user_message=extracted_user,
            assistant_response=extracted_assistant,
            tenant_id=self.tenant_id,
        )
```

### 4.2 用户问题 Hash 函数

```python
def _hash_user_message(user_message: str) -> str:
    """生成用户问题的唯一 hash."""
    normalized = user_message.strip().lower()[:200]
    return hashlib.md5(normalized.encode()).hexdigest()
```

### 4.3 后端 API（console.py）

```python
@router.post("/suggestions/qa-content")
async def get_suggestions_qa_content(
    request: Request,
    body: QAContentRequest,
):
    """根据用户问题获取后端提取的 Q&A 内容."""
    entry = await get_qa_content(
        chat_id=body.chat_id,
        user_message=body.user_message,
        tenant_id=tenant_id,
    )
    return QAContentResponse(success=bool(entry), qa_content=entry)
```

### 4.4 前端流程（useSuggestionsPolling.tsx）

```typescript
const pollSuggestions = useCallback(async () => {
  const userMessage = extractUserMessageText(currentRequest...);
  const assistantMessage = extractCopyableText(currentResponse...);

  // 由前端直接调用 external/mock suggestions API，后端不重复生成。
  const suggestions = await fetchSuggestions({
    chatId, turnId,
    userMessage,
    assistantMessage,
  });

  updateMessage({ ...response, suggestions });
}, []);
```

---

## 5. 文件修改清单

| 文件 | 修改内容 |
|------|----------|
| `src/swe/config/config.py` | 添加 SuggestionMode 枚举和配置字段 |
| `src/swe/app/suggestions/store.py` | 新增 store_qa_content, get_qa_content 函数 |
| `src/swe/app/suggestions/__init__.py` | 导出新增函数 |
| `src/swe/app/runner/runner.py` | 不再调度后端 suggestions 生成，避免和前端 external API 重复 |
| `src/swe/app/routers/console.py` | 新增 POST /suggestions/qa-content |
| `console/src/api/modules/suggestions.ts` | 提供前端 external/mock suggestions API 调用 |
| `console/.../useSuggestionsPolling.tsx` | 本地提取 Q&A 后直接调用前端 suggestions API |

---

## 6. 配置开关设计

| mode | 后端行为 | 前端行为 |
|------|----------|----------|
| disabled | 不提取 | 不轮询 |
| backend_generate | 提取 + 异步生成建议 | 轮询 GET /suggestions |
| qa_extraction_only | 仅提取 Q&A | 获取 Q&A + 调用外部 API |

当前线上目标行为已调整为：后端 runner 在完成回答后调度
`generate_suggestions()`，由后端组装 prompt 并调用模型生成 suggestions；
前端不再默认调用 external/mock suggestions API，只消费
`/console/suggestions` 中的后端生成结果。

---

## 7. 验证方案

1. 发送问题，等待模型响应完成
2. 前端从当前 request/response card 提取用户问题和助手回答
3. 前端调用 external/mock suggestions API 获取 suggestions
4. 检查 UI 显示"猜你想问"按钮
5. 点击按钮触发新对话
6. 存在后校验继续执行提示时，不提前展示"猜你想问"

---

## 8. 风险与限制

### 8.1 已知限制

- 内存存储：重启后 Q&A 内容丢失，但建议有效期仅120秒，影响有限
- 重复问题：相同问题在120秒内会匹配到同一 Q&A，但通常用户不会短时间内重复问相同问题
- 长问题截断：用户问题超过200字符会被截断用于 hash，可能影响匹配精度

### 8.2 风险缓解

- Fallback 机制：后端无内容时前端本地提取，不影响用户体验
- 异步执行：Q&A 提取在 finally 块中执行，不影响主流程
- 过期清理：自动清理超过120秒的数据，避免内存泄漏

---

## 9. 后续规划

- 支持多轮对话后二次生成 suggestions
- 支持基于对话上下文的 suggestions 生成
- 支持用户反馈机制优化 suggestions 质量
- 支持持久化存储（Redis）以支持分布式部署
