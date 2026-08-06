import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Empty,
  Input,
  Modal,
  Select,
  Skeleton,
  Switch,
  Table,
  Tabs,
  Tag,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  hookManagementApi,
  type HookScript,
  type HookScriptDiagnostic,
} from "@/api/modules/hookManagement";
import { useAppMessage } from "@/hooks/useAppMessage";

import {
  addHandler,
  addEvent,
  addGroup,
  moveHandler,
  replaceEvent,
  defaultContext,
  isScriptReference,
  removeGroup,
  removeHandler,
} from "./draft";
import { EventEditorDrawer } from "./components/EventEditorDrawer";
import { EventOverview } from "./components/EventOverview";
import { createScenarioEvent, scenarioTemplates } from "./scenarioTemplates";
import { eventMetadata } from "./eventMetadata";
import type {
  HookConfigDraft,
  HookEventName,
  HookHandlerDraft,
  HookMatcherGroupDraft,
  HookTreeSelection,
} from "./types";
import styles from "./index.module.less";

const events: HookEventName[] = [
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "Stop",
];

function findHandler(
  config: HookConfigDraft,
  selected: HookTreeSelection,
): HookHandlerDraft | null {
  if (selected.kind !== "handler") return null;
  return (
    config.events[selected.event]
      ?.find((group) => group.id === selected.groupId)
      ?.hooks.find((handler) => handler.id === selected.handlerId) ?? null
  );
}

function findGroup(
  config: HookConfigDraft,
  selected: HookTreeSelection,
): HookMatcherGroupDraft | null {
  if (selected.kind === "root") return null;
  return (
    config.events[selected.event]?.find(
      (group) => group.id === selected.groupId,
    ) ?? null
  );
}

function updateHandler(
  config: HookConfigDraft,
  selected: HookTreeSelection,
  changes: Partial<HookHandlerDraft>,
): HookConfigDraft {
  if (selected.kind !== "handler") return config;
  return {
    ...config,
    events: {
      ...config.events,
      [selected.event]: config.events[selected.event]?.map((group) =>
        group.id !== selected.groupId
          ? group
          : {
              ...group,
              hooks: group.hooks.map((handler) =>
                handler.id === selected.handlerId
                  ? { ...handler, ...changes }
                  : handler,
              ),
            },
      ),
    },
  };
}

function updateGroup(
  config: HookConfigDraft,
  selected: HookTreeSelection,
  changes: Partial<HookMatcherGroupDraft>,
): HookConfigDraft {
  if (selected.kind === "root") return config;
  return {
    ...config,
    events: {
      ...config.events,
      [selected.event]: config.events[selected.event]?.map((group) =>
        group.id === selected.groupId ? { ...group, ...changes } : group,
      ),
    },
  };
}

function removeEvent(
  config: HookConfigDraft,
  event: HookEventName,
): HookConfigDraft {
  const nextEvents = { ...config.events };
  delete nextEvents[event];
  return { ...config, events: nextEvents };
}

function serializeDraft(config: HookConfigDraft): string {
  return JSON.stringify(config);
}

function parseJsonRecord(value: string): Record<string, string> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      !parsed ||
      Array.isArray(parsed) ||
      typeof parsed !== "object" ||
      Object.values(parsed).some((item) => typeof item !== "string")
    ) {
      return null;
    }
    return parsed as Record<string, string>;
  } catch {
    return null;
  }
}

function validateDraft(config: HookConfigDraft): string | null {
  const groupIds = new Set<string>();
  const handlerIds = new Set<string>();
  for (const groups of Object.values(config.events)) {
    for (const group of groups ?? []) {
      if (!group.id.trim()) return "Matcher Group ID 不能为空";
      if (groupIds.has(group.id)) return `Matcher Group ID 重复：${group.id}`;
      groupIds.add(group.id);
      for (const handler of group.hooks) {
        if (!handler.id.trim()) return "Handler ID 不能为空";
        if (handlerIds.has(handler.id)) return `Handler ID 重复：${handler.id}`;
        handlerIds.add(handler.id);
        if (
          handler.type === "command" &&
          (!Array.isArray(handler.argv) ||
            !handler.argv.some((arg) => String(arg).trim()))
        ) {
          return `Handler ${handler.id} 至少需要一个非空命令参数`;
        }
        const timeout = Number(handler.timeout ?? 10);
        if (!Number.isFinite(timeout) || timeout <= 0) {
          return `Handler ${handler.id} 的超时必须大于 0`;
        }
        const snapshotLimit = Number(handler.conversationSnapshotLimit ?? 50);
        if (
          !Number.isInteger(snapshotLimit) ||
          snapshotLimit < 1 ||
          snapshotLimit > 200
        ) {
          return `Handler ${handler.id} 的会话快照消息数必须在 1 到 200 之间`;
        }
        if (handler.type === "http" && !String(handler.url ?? "").trim()) {
          return `HTTP Handler ${handler.id} 需要请求地址`;
        }
        if (handler.type === "prompt" && !String(handler.prompt ?? "").trim()) {
          return `Prompt Handler ${handler.id} 需要 Prompt`;
        }
        if (
          handler.type === "command" &&
          Array.isArray(handler.argv) &&
          handler.argv.some((arg) => isScriptReference(String(arg))) &&
          String(handler.cwd ?? "").trim()
        ) {
          return `脚本 Handler ${handler.id} 不能同时设置工作目录`;
        }
      }
    }
  }
  return null;
}

function HookManagementPage() {
  const { message } = useAppMessage();
  const [draft, setDraft] = useState<HookConfigDraft | null>(null);
  const [savedDraft, setSavedDraft] = useState("");
  const [revision, setRevision] = useState("");
  const [scripts, setScripts] = useState<HookScript[]>([]);
  const [diagnostics, setDiagnostics] = useState<HookScriptDiagnostic[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<HookTreeSelection>({ kind: "root" });
  const [editingEvent, setEditingEvent] = useState<HookEventName | null>(
    null,
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [templatePickerOpen, setTemplatePickerOpen] = useState(false);
  const [templateLabel, setTemplateLabel] = useState<string>();
  const [testOpen, setTestOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, unknown> | null>(
    null,
  );
  const [testContext, setTestContext] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const [conflictOpen, setConflictOpen] = useState(false);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [overwriteOpen, setOverwriteOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<{
    accepted: string[];
    warned: string[];
    failed: Array<{ filename: string; reason: string }>;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [snapshot, files] = await Promise.all([
        hookManagementApi.getConfiguration(),
        hookManagementApi.listScripts(),
      ]);
      setDraft(snapshot.hooks as HookConfigDraft);
      setSavedDraft(serializeDraft(snapshot.hooks as HookConfigDraft));
      setRevision(snapshot.revision);
      setDiagnostics(snapshot.diagnostics ?? []);
      setScripts(files);
      setSelected({ kind: "root" });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "加载 Hook 配置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handler = useMemo(
    () => (draft ? findHandler(draft, selected) : null),
    [draft, selected],
  );
  const group = useMemo(
    () => (draft ? findGroup(draft, selected) : null),
    [draft, selected],
  );
  const dirty = Boolean(draft) && serializeDraft(draft) !== savedDraft;

  const save = async () => {
    if (!draft) return;
    const validationError = validateDraft(draft);
    if (validationError) {
      setFormError(validationError);
      return;
    }
    setFormError(null);
    setSaving(true);
    try {
      const snapshot = await hookManagementApi.saveConfiguration(
        draft,
        revision,
      );
      setDraft(snapshot.hooks as HookConfigDraft);
      setSavedDraft(serializeDraft(snapshot.hooks as HookConfigDraft));
      setRevision(snapshot.revision);
      setDiagnostics(snapshot.diagnostics ?? []);
      message.success("Hook 配置已保存，Default Agent 将异步加载新配置");
    } catch (cause) {
      if ((cause as { status?: number }).status === 409) {
        setConflictOpen(true);
      } else if ((cause as { status?: number }).status === 422) {
        setFormError(
          cause instanceof Error ? cause.message : "Hook 配置未通过服务器验证",
        );
      } else {
        message.error(
          cause instanceof Error ? cause.message : "保存 Hook 配置失败",
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const upload = async (files: File[], overwrite: string[]) => {
    if (!files.length) return;
    setUploading(true);
    try {
      const result = await hookManagementApi.uploadScripts(files, overwrite);
      setUploadResult(result);
      setScripts(await hookManagementApi.listScripts());
      setPendingFiles([]);
      setOverwriteOpen(false);
    } catch (cause) {
      message.error(cause instanceof Error ? cause.message : "上传脚本失败");
    } finally {
      setUploading(false);
    }
  };

  const onFiles = (files: FileList | null) => {
    const next = Array.from(files ?? []);
    setPendingFiles(next);
    const duplicate = next.some((file) =>
      scripts.some((script) => script.filename === file.name),
    );
    if (duplicate) setOverwriteOpen(true);
    else void upload(next, []);
  };

  const runTest = async () => {
    if (!handler || selected.kind !== "handler") return;
    let context: Record<string, unknown>;
    try {
      context = JSON.parse(testContext) as Record<string, unknown>;
    } catch {
      setTestError("Hook Context 必须是有效 JSON");
      return;
    }
    const requiredContextFields = [
      "session_id",
      "transcript_path",
      "cwd",
      "tenant_id",
      "effective_tenant_id",
      "user_id",
      "agent_id",
      "channel",
    ];
    if (
      requiredContextFields.some(
        (field) => typeof context[field] !== "string",
      ) ||
      context.hook_event_name !== selected.event
    ) {
      setTestError("Hook Context 必须保留当前事件及所有必填 Envelope 字段");
      return;
    }
    setTestError(null);
    setTesting(true);
    try {
      const result = await hookManagementApi.manualTest(handler, context);
      setTestResult(result.redacted_summary);
    } catch (cause) {
      const errorMessage =
        cause instanceof Error ? cause.message : "人工测试失败";
      if ((cause as { status?: number }).status === 422) {
        setTestError(errorMessage);
      } else {
        message.error(errorMessage);
      }
    } finally {
      setTesting(false);
    }
  };

  const openManualTest = () => {
    if (!handler || selected.kind !== "handler") return;
    setFormError(null);
    setTestError(null);
    setTestResult(null);
    setTestContext(JSON.stringify(defaultContext(selected.event), null, 2));
    setTestOpen(true);
  };

  const onHandlerChange = (changes: Partial<HookHandlerDraft>) => {
    if (!draft || selected.kind !== "handler") return;
    setDraft(updateHandler(draft, selected, changes));
    if (typeof changes.id === "string" && changes.id !== selected.handlerId) {
      setSelected({ ...selected, handlerId: changes.id });
    }
  };

  const onGroupChange = (changes: Partial<HookMatcherGroupDraft>) => {
    if (!draft || selected.kind === "root") return;
    setDraft(updateGroup(draft, selected, changes));
    if (typeof changes.id === "string" && changes.id !== selected.groupId) {
      setSelected({ ...selected, groupId: changes.id });
    }
  };

  if (loading) {
    return (
      <div className={styles.page}>
        <Skeleton active paragraph={{ rows: 12 }} />
      </div>
    );
  }
  if (error || !draft) {
    return (
      <div className={styles.page}>
        <Empty description={error ?? "未找到 Default Agent Profile"}>
          <Button onClick={() => void load()}>重试</Button>
        </Empty>
      </div>
    );
  }

  const renderHandlerEditor = () => {
    if (!handler || selected.kind !== "handler") return null;
    const argv = Array.isArray(handler.argv) ? handler.argv.map(String) : [];
    return (
      <div className={styles.editor}>
        <div className={styles.editorHeader}>
          <div>
            <h2>{handler.id || "未命名 Handler"}</h2>
            <span>{handler.type} Handler</span>
          </div>
          <Button
            onClick={openManualTest}
          >
            执行人工测试
          </Button>
        </div>
        <label>
          Handler ID
          <Input
            value={handler.id}
            onChange={(event) => onHandlerChange({ id: event.target.value })}
          />
        </label>
        {handler.type === "command" && (
          <>
            <div className={styles.sectionTitle}>命令参数（按顺序传递）</div>
            {argv.map((value, index) => (
              <label key={index}>
                {`命令参数 ${index + 1}`}
                <Input
                  value={value}
                  onChange={(event) => {
                    const next = [...argv];
                    next[index] = event.target.value;
                    onHandlerChange({ argv: next });
                  }}
                />
              </label>
            ))}
            <Button
              type="dashed"
              onClick={() => onHandlerChange({ argv: [...argv, ""] })}
            >
              添加参数
            </Button>
            <label>
              工作目录
              <Input
                value={String(handler.cwd ?? "")}
                onChange={(event) =>
                  onHandlerChange({ cwd: event.target.value })
                }
              />
            </label>
            <label>
              Shell
              <Select
                allowClear
                value={handler.shell as string | undefined}
                options={["sh", "bash", "zsh", "cmd", "powershell"].map(
                  (value) => ({ value, label: value }),
                )}
                onChange={(value) => onHandlerChange({ shell: value })}
              />
            </label>
            <label>
              环境变量（JSON）
              <Input.TextArea
                key={`${handler.id}-env`}
                defaultValue={JSON.stringify(handler.env ?? {}, null, 2)}
                onBlur={(event) => {
                  const env = parseJsonRecord(event.target.value);
                  if (!env) {
                    setJsonError("环境变量必须是 string → string 的 JSON 对象");
                    return;
                  }
                  setJsonError(null);
                  onHandlerChange({ env });
                }}
                rows={4}
              />
            </label>
          </>
        )}
        {handler.type === "http" && (
          <>
            <label>
              请求地址
              <Input
                value={String(handler.url ?? "")}
                onChange={(event) =>
                  onHandlerChange({ url: event.target.value })
                }
              />
            </label>
            <label>
              请求头（JSON）
              <Input.TextArea
                key={`${handler.id}-headers`}
                defaultValue={JSON.stringify(handler.headers ?? {}, null, 2)}
                onBlur={(event) => {
                  const headers = parseJsonRecord(event.target.value);
                  if (!headers) {
                    setJsonError("请求头必须是 string → string 的 JSON 对象");
                    return;
                  }
                  setJsonError(null);
                  onHandlerChange({ headers });
                }}
                rows={4}
              />
            </label>
            <label>
              请求头密钥引用（JSON）
              <Input.TextArea
                key={`${handler.id}-header-secret-refs`}
                defaultValue={JSON.stringify(
                  handler.headerSecretRefs ?? {},
                  null,
                  2,
                )}
                onBlur={(event) => {
                  const headerSecretRefs = parseJsonRecord(event.target.value);
                  if (!headerSecretRefs) {
                    setJsonError(
                      "请求头密钥引用必须是 string → string 的 JSON 对象",
                    );
                    return;
                  }
                  setJsonError(null);
                  onHandlerChange({ headerSecretRefs });
                }}
                rows={4}
              />
            </label>
            <label>
              可用环境变量（每行一个）
              <Input.TextArea
                value={
                  Array.isArray(handler.allowedEnvVars)
                    ? handler.allowedEnvVars.join("\n")
                    : ""
                }
                onChange={(event) =>
                  onHandlerChange({
                    allowedEnvVars: event.target.value
                      .split("\n")
                      .map((value) => value.trim())
                      .filter(Boolean),
                  })
                }
                rows={3}
              />
            </label>
          </>
        )}
        {handler.type === "prompt" && (
          <label>
            Prompt
            <Input.TextArea
              value={String(handler.prompt ?? "")}
              onChange={(event) =>
                onHandlerChange({ prompt: event.target.value })
              }
              rows={5}
            />
          </label>
        )}
        <Collapse
          items={[
            {
              key: "advanced",
              label: "高级设置",
              children: (
                <div className={styles.advanced}>
                  <label>
                    条件表达式
                    <Input
                      value={String(handler.if ?? "")}
                      onChange={(event) =>
                        onHandlerChange({ if: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    超时（秒）
                    <Input
                      type="number"
                      value={String(handler.timeout ?? 10)}
                      onChange={(event) =>
                        onHandlerChange({ timeout: Number(event.target.value) })
                      }
                    />
                  </label>
                  <label>
                    失败策略
                    <Select
                      value={String(handler.failPolicy ?? "allow")}
                      options={[
                        { value: "allow", label: "允许" },
                        { value: "block", label: "阻断" },
                      ]}
                      onChange={(value) =>
                        onHandlerChange({ failPolicy: value })
                      }
                    />
                  </label>
                  <label>
                    状态消息
                    <Input
                      value={String(handler.statusMessage ?? "")}
                      onChange={(event) =>
                        onHandlerChange({ statusMessage: event.target.value })
                      }
                    />
                  </label>
                  <label className={styles.switchLine}>
                    仅执行一次
                    <Switch
                      checked={Boolean(handler.once)}
                      onChange={(once) => onHandlerChange({ once })}
                    />
                  </label>
                  <label className={styles.switchLine}>
                    附带会话快照
                    <Switch
                      checked={Boolean(handler.includeConversationSnapshot)}
                      onChange={(includeConversationSnapshot) =>
                        onHandlerChange({ includeConversationSnapshot })
                      }
                    />
                  </label>
                  <label>
                    会话快照消息数
                    <Input
                      type="number"
                      min={1}
                      max={200}
                      value={String(handler.conversationSnapshotLimit ?? 50)}
                      onChange={(event) =>
                        onHandlerChange({
                          conversationSnapshotLimit: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  {jsonError && (
                    <p className={styles.fieldError}>{jsonError}</p>
                  )}
                </div>
              ),
            },
          ]}
        />
      </div>
    );
  };

  const renderGroupEditor = () => {
    if (!group || selected.kind === "root") return null;
    return (
      <div className={styles.editor}>
        <h2>Matcher Group</h2>
        <p>该组内的 Handler 会按顺序执行。</p>
        <label>
          Matcher Group ID
          <Input
            value={group.id}
            onChange={(event) => onGroupChange({ id: event.target.value })}
          />
        </label>
        <label>
          匹配工具（每行一个）
          <Input.TextArea
            value={group.matcher.tools.join("\n")}
            onChange={(event) =>
              onGroupChange({
                matcher: {
                  tools: event.target.value
                    .split("\n")
                    .map((tool) => tool.trim())
                    .filter(Boolean),
                },
              })
            }
            rows={5}
          />
        </label>
        <p>留空表示匹配该事件的所有工具调用。</p>
      </div>
    );
  };

  const basicDetails = editingEvent ? (
    <section className={styles.eventBasics}>
      <span className={styles.eventCode}>{editingEvent}</span>
      <h3>{eventMetadata[editingEvent].label}</h3>
      <p>{eventMetadata[editingEvent].description}</p>
      <Tag color={draft.enabled ? "success" : "default"}>
        {draft.enabled ? "该事件将参与 Hook 执行" : "全局 Hook 当前已停用"}
      </Tag>
    </section>
  ) : null;

  const testDetails = (
    <section className={styles.testPublishPanel}>
      <div>
        <h3>人工测试</h3>
        <p>真实执行当前草稿中的一个处理器，不会保存草稿或重载 Agent。</p>
      </div>
      <Button
        disabled={!handler || selected.kind !== "handler"}
        title={handler ? undefined : "请先选择一个处理器"}
        onClick={openManualTest}
      >
        执行人工测试
      </Button>
      {testResult && (
        <pre className={styles.summary}>{JSON.stringify(testResult, null, 2)}</pre>
      )}
    </section>
  );

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1>
            Hook 管理 <Tag>Default Agent · 当前租户</Tag>
          </h1>
          <p>配置草稿保存后才会激活；脚本上传立即保存到受控脚本库。</p>
        </div>
        <Button type="primary" loading={saving} onClick={() => void save()}>
          保存并激活
        </Button>
      </header>
      {diagnostics.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="Hook 脚本引用需要修复"
          description={
            <ul>
              {diagnostics.map((diagnostic) => (
                <li
                  key={`${diagnostic.event}-${diagnostic.group_id}-${diagnostic.handler_id}-${diagnostic.argument}`}
                >
                  {`${diagnostic.event} · ${diagnostic.group_id} · ${diagnostic.handler_id}：${diagnostic.argument}（${diagnostic.reason}）`}
                </li>
              ))}
            </ul>
          }
        />
      )}
      {formError && <p className={styles.formError}>{formError}</p>}
      <Tabs
        items={[
          {
            key: "configuration",
            label: "配置",
            children: (
              <>
                <EventOverview
                  config={draft}
                  dirty={dirty}
                  onEnabledChange={(enabled) =>
                    setDraft({ ...draft, enabled })
                  }
                  onCreate={() => {
                    setTemplatePickerOpen(false);
                    setCreateOpen(true);
                  }}
                  onEdit={(event) => {
                    if (!draft.events[event]) setDraft(addEvent(draft, event));
                    const firstGroup = draft.events[event]?.[0];
                    const firstHandler = firstGroup?.hooks[0];
                    setSelected(
                      firstGroup && firstHandler
                        ? {
                            kind: "handler",
                            event,
                            groupId: firstGroup.id,
                            handlerId: firstHandler.id,
                          }
                        : firstGroup
                        ? { kind: "group", event, groupId: firstGroup.id }
                        : { kind: "root" },
                    );
                    setTemplateLabel(undefined);
                    setEditingEvent(event);
                  }}
                />
              </>
            ),
          },
          {
            key: "scripts",
            label: "脚本库",
            children: (
              <div className={styles.scripts}>
                <div className={styles.scriptActions}>
                  <Button
                    type="primary"
                    onClick={() => uploadInputRef.current?.click()}
                  >
                    上传 Hook 脚本
                  </Button>
                  <input
                    ref={uploadInputRef}
                    className={styles.uploadInput}
                    aria-label="选择 Hook 脚本文件"
                    type="file"
                    multiple
                    accept=".py,.sh,.bash,.zsh"
                    tabIndex={-1}
                    onChange={(event) => {
                      onFiles(event.target.files);
                      event.currentTarget.value = "";
                    }}
                  />
                  <span>
                    仅接受 .py、.sh、.bash、.zsh；单文件最多 1 MB，一批最多 20
                    个。
                  </span>
                </div>
                <Table
                  rowKey="filename"
                  size="small"
                  dataSource={scripts}
                  pagination={false}
                  columns={[
                    { title: "脚本", dataIndex: "filename" },
                    {
                      title: "大小",
                      dataIndex: "size",
                      render: (value) => `${value} B`,
                    },
                    {
                      title: "SHA-256",
                      dataIndex: "sha256",
                      render: (value) => (
                        <code>{String(value).slice(0, 16)}…</code>
                      ),
                    },
                  ]}
                />
                {uploadResult && (
                  <div className={styles.uploadResult}>
                    {uploadResult.accepted.length > 0 && (
                      <p>已接收：{uploadResult.accepted.join("、")}</p>
                    )}
                    {uploadResult.warned.length > 0 && (
                      <p>扫描警告：{uploadResult.warned.join("、")}</p>
                    )}
                    {uploadResult.failed.map((item) => (
                      <p key={item.filename}>
                        失败：{item.filename} — {item.reason}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            ),
          },
        ]}
      />
      <EventEditorDrawer
        key={editingEvent ?? "closed"}
        event={editingEvent}
        groups={editingEvent ? (draft.events[editingEvent] ?? []) : []}
        templateLabel={templateLabel}
        basicDetails={basicDetails}
        details={
          selected.kind !== "root" && selected.event === editingEvent ? (
            renderHandlerEditor() ?? renderGroupEditor()
          ) : (
            <p>选择一个分组或处理器以编辑详细配置。</p>
          )
        }
        dirty={dirty}
        scopeDetails={
          <p className={styles.scopeHint}>
            选择一个分组后，可在处理器编排中维护其高级匹配条件。
          </p>
        }
        saving={saving}
        testDetails={testDetails}
        onAddGroup={() => {
          if (editingEvent) setDraft(addGroup(draft, editingEvent));
        }}
        onAddHandler={(groupId, type) => {
          if (editingEvent) {
            setDraft(addHandler(draft, editingEvent, groupId, type));
          }
        }}
        onClose={() => setEditingEvent(null)}
        onMoveHandler={(groupId, fromIndex, toIndex) => {
          if (editingEvent) {
            setDraft(
              moveHandler(draft, editingEvent, groupId, fromIndex, toIndex),
            );
          }
        }}
        onRemoveEvent={() => {
          if (!editingEvent) return;
          setDraft(removeEvent(draft, editingEvent));
          setSelected({ kind: "root" });
          setTemplateLabel(undefined);
          setEditingEvent(null);
        }}
        onRemoveGroup={(groupId) => {
          if (!editingEvent) return;
          setDraft(removeGroup(draft, editingEvent, groupId));
          setSelected({ kind: "root" });
        }}
        onRemoveHandler={(groupId, handlerId) => {
          if (!editingEvent) return;
          setDraft(removeHandler(draft, editingEvent, groupId, handlerId));
          setSelected({ kind: "root" });
        }}
        onSave={() => void save()}
        onSelectGroup={(groupId) => {
          if (editingEvent) {
            setSelected({ kind: "group", event: editingEvent, groupId });
          }
        }}
        onSelectHandler={(groupId, handlerId) => {
          if (editingEvent) {
            setSelected({
              kind: "handler",
              event: editingEvent,
              groupId,
              handlerId,
            });
          }
        }}
      />
      <Modal
        footer={null}
        open={createOpen}
        title="新建事件"
        onCancel={() => setCreateOpen(false)}
      >
        {templatePickerOpen ? (
          <div className={styles.templateList}>
            {scenarioTemplates.map((template) => (
              <Button
                key={template.id}
                block
                onClick={() => {
                  const scenario = createScenarioEvent(template.id);
                  setDraft(replaceEvent(draft, scenario.event, scenario.groups));
                  setSelected({
                    kind: "group",
                    event: scenario.event,
                    groupId: scenario.groups[0]!.id,
                  });
                  setTemplateLabel(template.label);
                  setEditingEvent(scenario.event);
                  setCreateOpen(false);
                }}
              >
                <strong>{template.label}</strong>
                <span>{template.description}</span>
              </Button>
            ))}
          </div>
        ) : (
          <div className={styles.createActions}>
            <Button block onClick={() => setTemplatePickerOpen(true)}>
              从场景模板开始
            </Button>
            <Select
              placeholder="从空白事件开始"
              options={events.map((event) => ({ value: event, label: event }))}
              onChange={(event: HookEventName) => {
                setDraft(addEvent(draft, event));
                setTemplateLabel(undefined);
                setEditingEvent(event);
                setCreateOpen(false);
              }}
            />
          </div>
        )}
      </Modal>
      <Modal
        title="配置已被更新"
        open={conflictOpen}
        onCancel={() => setConflictOpen(false)}
        footer={
          <>
            <Button onClick={() => setConflictOpen(false)}>保留草稿</Button>
            <Button
              type="primary"
              onClick={() => {
                setConflictOpen(false);
                void load();
              }}
            >
              重新加载最新配置
            </Button>
          </>
        }
      >
        <p>其他用户已保存新的 Hook 配置。重新加载会丢弃当前草稿。</p>
      </Modal>
      <Modal
        title="确认覆盖脚本"
        open={overwriteOpen}
        onCancel={() => setOverwriteOpen(false)}
        onOk={() =>
          void upload(
            pendingFiles,
            pendingFiles
              .filter((file) =>
                scripts.some((script) => script.filename === file.name),
              )
              .map((file) => file.name),
          )
        }
        okText="确认覆盖"
        confirmLoading={uploading}
      >
        <p>
          {pendingFiles
            .filter((file) =>
              scripts.some((script) => script.filename === file.name),
            )
            .map((file) => file.name)
            .join("、")}{" "}
          已存在。覆盖会影响之后的 Hook 事件。
        </p>
      </Modal>
      <Modal
        title="执行人工测试"
        open={testOpen}
        onCancel={() => {
          if (testing) return;
          setTestOpen(false);
          setConfirmed(false);
          setTestResult(null);
          setFormError(null);
          setTestError(null);
        }}
        onOk={() => void runTest()}
        okText="执行测试"
        okButtonProps={{ disabled: !confirmed, loading: testing }}
        closable={!testing}
        maskClosable={!testing}
      >
        <p>这会真实执行当前草稿中的一个 Handler，不会保存草稿或重载 Agent。</p>
        <label>
          Hook Context（JSON）
          <Input.TextArea
            aria-label="Hook Context（JSON）"
            value={testContext}
            onChange={(event) => setTestContext(event.target.value)}
            rows={10}
          />
        </label>
        {testError && <p className={styles.fieldError}>{testError}</p>}
        <Checkbox
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        >
          我确认将执行真实命令、HTTP 请求或模型调用
        </Checkbox>
        {testResult && (
          <pre className={styles.summary}>
            {JSON.stringify(testResult, null, 2)}
          </pre>
        )}
      </Modal>
    </div>
  );
}

export default HookManagementPage;
