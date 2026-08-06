# Hook 管理无效脚本引用诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 无效脚本引用不阻断 Hook 管理页加载，并由页面提示；保存与人工测试仍拒绝该引用。

**Architecture:** 服务端在读取快照时把脚本存在性问题转为诊断数据，保留当前草稿内容；严格路径归一化仍仅用于保存和人工测试。诊断通过 HTTP 与 TypeScript 类型传给 React 页面的警告组件。

**Tech Stack:** Python、FastAPI/Pydantic、pytest、React、TypeScript、Ant Design、Vitest。

---

## 文件结构

- `src/swe/app/hook_management.py`：读配置时产生诊断，保存保持严格。
- `src/swe/app/routers/hook_management.py`：诊断 HTTP 响应字段。
- `tests/unit/app/test_hook_management.py`：服务端回归测试。
- `tests/unit/routers/test_hook_management.py`：路由契约测试。
- `console/src/api/modules/hookManagement.ts`：前端响应类型。
- `console/src/pages/Control/HookManagement/index.tsx`：告警渲染。
- `console/src/pages/Control/HookManagement/index.test.tsx`：页面回归测试。

### Task 1: 服务读取时收集脚本诊断

**Files:**

- Modify: `src/swe/app/hook_management.py:61-126,341-455`
- Test: `tests/unit/app/test_hook_management.py`

- [ ] **Step 1: 写入失败测试**

在服务测试中构造带 `argv: ["python", "hooks/scripts/missing.py"]`、事件 `PreToolUse`、组 `group`、Handler `missing-script` 的已保存 `agent.json`，并断言：

```python
snapshot = service.get_configuration()
assert snapshot.hooks["events"]["PreToolUse"][0]["hooks"][0]["argv"][1] == "hooks/scripts/missing.py"
assert snapshot.diagnostics == (
    HookScriptDiagnostic(
        event="PreToolUse", group_id="group", handler_id="missing-script",
        argument="hooks/scripts/missing.py",
        reason="script is not in the controlled library: missing.py",
    ),
)
with pytest.raises(HookManagementValidationError, match="controlled library"):
    service.save_configuration(
        hooks=snapshot.hooks, expected_revision=snapshot.revision, actor=_actor(),
    )
```

- [ ] **Step 2: 验证测试为红色**

Run: `../../venv/bin/python -m pytest tests/unit/app/test_hook_management.py::test_get_configuration_reports_missing_script_without_blocking_load -q`

Expected: FAIL，因为 `get_configuration()` 当前调用严格 `_validate_hooks()`。

- [ ] **Step 3: 实现最小的宽松读取路径**

在 `HookConfigurationSnapshot` 前添加：

```python
@dataclass(frozen=True)
class HookScriptDiagnostic:
    event: str
    group_id: str
    handler_id: str
    argument: str
    reason: str
```

给 `HookConfigurationSnapshot` 加 `diagnostics: tuple[HookScriptDiagnostic, ...] = ()`。把现有 `_validate_hooks()` 中的结构校验抽为：

```python
def _normalize_hook_shape(self, hooks: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(hooks, dict):
        raise HookManagementValidationError("hooks must be an object")
    self._reject_command_strings(hooks)
    try:
        config = HookConfig.model_validate(hooks)
    except ValidationError as exc:
        raise HookManagementValidationError(str(exc)) from exc
    self._validate_unique_ids(config)
    return config.model_dump(mode="json", by_alias=True)
```

让 `_validate_hooks()` 调用该方法后仍执行 `_normalize_script_references()`。修改 `get_configuration()`，对原始 hooks 调用 `_normalize_hook_shape()` 后使用新增 `_normalize_script_references_for_load()`：遍历 command Handler 的 `argv`，成功参数继续规范化；每个捕获的 `HookManagementValidationError` 保留原参数并产生包含事件、组、Handler、参数与错误原因的 `HookScriptDiagnostic`。不要捕获 JSON、Pydantic、重复 ID 或 command string 错误；revision 必须由返回给页面的 hooks 计算。

- [ ] **Step 4: 验证测试变绿**

Run: `../../venv/bin/python -m pytest tests/unit/app/test_hook_management.py -q`

Expected: PASS。

- [ ] **Step 5: 提交服务改动**

Run: `git add src/swe/app/hook_management.py tests/unit/app/test_hook_management.py && git commit -m "fix(hooks): diagnose invalid script references on load"`

### Task 2: 在配置接口中返回诊断

**Files:**

- Modify: `src/swe/app/routers/hook_management.py:40-94`
- Test: `tests/unit/routers/test_hook_management.py`

- [ ] **Step 1: 写入失败的 HTTP 契约测试**

让 `_FakeService.snapshot` 包含一个 `HookScriptDiagnostic`，新增：

```python
response = client.get("/hook-management/configuration")
assert response.status_code == 200
assert response.json()["diagnostics"] == [{
    "event": "PreToolUse", "group_id": "group",
    "handler_id": "missing-script", "argument": "hooks/scripts/missing.py",
    "reason": "script is not in the controlled library: missing.py",
}]
```

- [ ] **Step 2: 验证测试为红色**

Run: `../../venv/bin/python -m pytest tests/unit/routers/test_hook_management.py::test_get_configuration_returns_script_diagnostics -q`

Expected: FAIL，响应还没有 `diagnostics`。

- [ ] **Step 3: 扩展 Pydantic 响应模型**

导入 `HookScriptDiagnostic`，添加：

```python
class HookScriptDiagnosticResponse(BaseModel):
    event: str
    group_id: str
    handler_id: str
    argument: str
    reason: str
```

给 `HookConfigurationResponse` 加 `diagnostics: list[HookScriptDiagnosticResponse] = Field(default_factory=list)`，并在 `_configuration_response()` 加入：

```python
diagnostics=[
    HookScriptDiagnosticResponse(**diagnostic.__dict__)
    for diagnostic in snapshot.diagnostics
],
```

- [ ] **Step 4: 验证测试变绿**

Run: `../../venv/bin/python -m pytest tests/unit/routers/test_hook_management.py -q`

Expected: PASS。

- [ ] **Step 5: 提交路由改动**

Run: `git add src/swe/app/routers/hook_management.py tests/unit/routers/test_hook_management.py && git commit -m "feat(hooks): expose script diagnostics in configuration response"`

### Task 3: 在页面告警中显示诊断

**Files:**

- Modify: `console/src/api/modules/hookManagement.ts:22-25`
- Modify: `console/src/pages/Control/HookManagement/index.tsx:1-13,160-194,554-575`
- Test: `console/src/pages/Control/HookManagement/index.test.tsx`

- [ ] **Step 1: 写入失败的 UI 回归测试**

```tsx
mocks.getConfiguration.mockResolvedValueOnce({
  hooks,
  revision: "rev-1",
  diagnostics: [{
    event: "PreToolUse", group_id: "tool-guards", handler_id: "guard-shell",
    argument: "hooks/scripts/missing.py",
    reason: "script is not in the controlled library: missing.py",
  }],
});
render(<HookManagementPage />);
expect(await screen.findByRole("heading", { name: /Hook 管理/ })).toBeInTheDocument();
expect(screen.getByText("Hook 脚本引用需要修复")).toBeInTheDocument();
expect(screen.getByText(/PreToolUse · tool-guards · guard-shell/)).toBeInTheDocument();
expect(screen.getByText(/hooks\/scripts\/missing.py/)).toBeInTheDocument();
```

- [ ] **Step 2: 验证测试为红色**

Run: `pnpm test:run src/pages/Control/HookManagement/index.test.tsx`

Expected: FAIL，找不到 `Hook 脚本引用需要修复`。

- [ ] **Step 3: 声明类型、保存诊断并渲染 `Alert`**

在 `hookManagement.ts` 添加：

```ts
export type HookScriptDiagnostic = {
  event: string;
  group_id: string;
  handler_id: string;
  argument: string;
  reason: string;
};
```

给 `HookConfigurationResponse` 加 `diagnostics: HookScriptDiagnostic[]`。页面导入 `Alert`，添加 diagnostics state，在 `load()` 和保存成功时分别执行 `setDiagnostics(snapshot.diagnostics ?? [])`。在 header 后、`formError` 前添加：

```tsx
{diagnostics.length > 0 && (
  <Alert
    type="warning"
    showIcon
    message="Hook 脚本引用需要修复"
    description={<ul>{diagnostics.map((diagnostic) => (
      <li key={`${diagnostic.event}-${diagnostic.group_id}-${diagnostic.handler_id}-${diagnostic.argument}`}>
        {`${diagnostic.event} · ${diagnostic.group_id} · ${diagnostic.handler_id}：${diagnostic.argument}（${diagnostic.reason}）`}
      </li>
    ))}</ul>}
  />
)}
```

不修改 `error` 状态，也不禁用编辑或保存按钮。

- [ ] **Step 4: 验证测试变绿**

Run: `pnpm test:run src/pages/Control/HookManagement/index.test.tsx`

Expected: PASS。

- [ ] **Step 5: 提交控制台改动**

Run: `git add console/src/api/modules/hookManagement.ts console/src/pages/Control/HookManagement/index.tsx console/src/pages/Control/HookManagement/index.test.tsx && git commit -m "fix(console): show invalid hook script diagnostics"`

### Task 4: 最终验证与影响范围检查

**Files:**

- Verify: `tests/unit/app/test_hook_management.py`
- Verify: `tests/unit/routers/test_hook_management.py`
- Verify: `console/src/pages/Control/HookManagement/index.test.tsx`

- [ ] **Step 1: 运行后端测试**

Run: `../../venv/bin/python -m pytest tests/unit/app/test_hook_management.py tests/unit/routers/test_hook_management.py -q`

Expected: PASS。

- [ ] **Step 2: 运行控制台构建与测试**

Run: `pnpm build:test && pnpm test:run src/pages/Control/HookManagement/index.test.tsx`

Expected: 两个命令退出码均为 0。

- [ ] **Step 3: 检查差异范围**

Run: `git diff --check && git status --short`，再运行 `gitnexus_detect_changes({scope: "all", repo: "CoPaw"})`。

Expected: 无空白错误；仅变更本计划列出的 Hook 服务、路由、控制台和测试文件；没有高风险的意外执行流影响。
