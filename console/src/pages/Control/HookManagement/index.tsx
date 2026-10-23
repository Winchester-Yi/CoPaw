import {
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
import { SendOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  hookManagementApi,
  type HookDistributionResponse,
  type HookScript,
} from "@/api/modules/hookManagement";
import { TenantSelector } from "@/components/TenantSelector";
import { useAppMessage } from "@/hooks/useAppMessage";
import { getUserId } from "@/utils/identity";

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

const outputTransformContract = `{
  "decision": "allow",
  "hookSpecificOutput": {
    "replacementText": "可选最终文本"
  }
}`;

const outputAppendContract = `{
  "decision": "allow",
  "reason": "报告已生成",
  "hookSpecificOutput": {
    "appendText": "可选附加文本 / Markdown"
  }
}`;

function ManualTestSummary({
  result,
}: {
  result: Record<string, unknown> | null;
}) {
  if (!result) return null;
  const append = result.output_append === true;
  if (!append && result.output_transform !== true) {
    return (
      <pre className={styles.summary}>{JSON.stringify(result, null, 2)}</pre>
    );
  }
  const fields = append
    ? [
        "output_append",
        "status",
        "append_applied",
        "append_bytes",
        "effective_fail_policy",
        "duration_ms",
        "failed",
        "failure_type",
      ]
    : [
        "status",
        "replacement_applied",
        "replacement_length",
        "failed",
        "failure_type",
      ];
  return (
    <section className={styles.outputTransformSummary}>
      <p>
        {append
          ? "仅执行当前追加处理器，不模拟多处理器排序、累计预算或持久化交付。"
          : "仅执行当前处理器，不模拟转换链路或总时间预算。"}
      </p>
      {fields.map((field) => (
        <p key={field}>
          {field}: {String(result[field] ?? "")}
        </p>
      ))}
    </section>
  );
}

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

function normalizeCommandShells(config: HookConfigDraft): HookConfigDraft {
  return {
    ...config,
    events: Object.fromEntries(
      Object.entries(config.events).map(([event, groups]) => [
        event,
        groups?.map((group) => ({
          ...group,
          hooks: group.hooks.map((handler) =>
            handler.type === "command"
              ? { ...handler, shell: "bash" }
              : handler,
          ),
        })),
      ]),
    ),
  } as HookConfigDraft;
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
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<HookTreeSelection>({ kind: "root" });
  const [editingEvent, setEditingEvent] = useState<HookEventName | null>(null);
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
  const [distributionOpen, setDistributionOpen] = useState(false);
  const [distributing, setDistributing] = useState(false);
  const [distributionGroupIds, setDistributionGroupIds] = useState<string[]>(
    [],
  );
  const [distributionTenantIds, setDistributionTenantIds] = useState<string[]>(
    [],
  );
  const [distributionResult, setDistributionResult] =
    useState<HookDistributionResponse | null>(null);

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
  const distributionGroups = useMemo(
    () =>
      Object.entries(draft?.events ?? {}).flatMap(([event, groups]) =>
        (groups ?? []).map((group) => ({
          event,
          group,
          scriptNames: group.hooks.flatMap((handler) =>
            handler.type === "command" && Array.isArray(handler.argv)
              ? handler.argv
                  .map(String)
                  .filter(isScriptReference)
                  .map((reference) =>
                    reference.slice(reference.lastIndexOf("/") + 1),
                  )
              : [],
          ),
        })),
      ),
    [draft],
  );

  const openDistribution = () => {
    if (dirty || !draft) return;
    setDistributionGroupIds(selected.kind === "root" ? [] : [selected.groupId]);
    setDistributionTenantIds([]);
    setDistributionResult(null);
    setDistributionOpen(true);
  };

  const distribute = async () => {
    if (!distributionGroupIds.length || !distributionTenantIds.length) return;
    setDistributing(true);
    try {
      const result = await hookManagementApi.distributeToDefaultAgents({
        matcherGroupIds: distributionGroupIds,
        targetTenantIds: distributionTenantIds,
      });
      setDistributionResult(result);
      const successful = result.results.filter((item) => item.success).length;
      const failed = result.results.length - successful;
      if (successful) message.success(`已成功分发到 ${successful} 个租户`);
      if (failed) message.warning(`${failed} 个租户分发失败，请查看结果`);
    } catch (cause) {
      message.error(cause instanceof Error ? cause.message : "Hook 分发失败");
    } finally {
      setDistributing(false);
    }
  };

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
        normalizeCommandShells(draft),
        revision,
      );
      setDraft(snapshot.hooks as HookConfigDraft);
      setSavedDraft(serializeDraft(snapshot.hooks as HookConfigDraft));
      setRevision(snapshot.revision);
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
    if (
      (handler.outputTransform || handler.outputAppend) &&
      (typeof context.assistant_response !== "string" ||
        !context.assistant_response.trim())
    ) {
      setTestError(
        handler.outputAppend
          ? "追加消息测试需要非空的 assistant_response"
          : "输出转换测试需要非空的 assistant_response",
      );
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
          <Button onClick={openManualTest}>执行人工测试</Button>
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
            <div>Shell：bash</div>
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
        {selected.event === "Stop" && (
          <section className={styles.outputTransform}>
            <label className={styles.switchLine}>
              转换最终回复
              <Switch
                aria-label="转换最终回复"
                checked={Boolean(handler.outputTransform)}
                onChange={(outputTransform) =>
                  onHandlerChange(
                    outputTransform
                      ? { outputTransform, outputAppend: false, once: false }
                      : { outputTransform },
                  )
                }
              />
            </label>
            <label className={styles.switchLine}>
              追加独立消息
              <Switch
                aria-label="追加独立消息"
                checked={Boolean(handler.outputAppend)}
                onChange={(outputAppend) =>
                  onHandlerChange(
                    outputAppend
                      ? { outputAppend, outputTransform: false, once: false }
                      : { outputAppend },
                  )
                }
              />
            </label>
            {handler.outputAppend && (
              <div className={styles.outputTransformDetails}>
                <p>
                  仅在最终 Stop 校验放行后追加独立文本 / Markdown
                  消息；首期仅支持 Console，消息进入历史和后续模型上下文。
                </p>
                <p>
                  正文保持流式输出；存在其他输出转换器时，仍遵循其暂存规则。
                </p>
                <p>
                  单条最多 16 KiB UTF-8，每轮累计 64 KiB；单处理器默认 10
                  秒，阶段总预算默认 30 秒。超限跳过，不截断。
                </p>
                <p>
                  仅接受 decision、非空
                  reason、hookSpecificOutput.appendText；decision 必须为
                  allow。appendText
                  可省略；提供时必须为非空白字符串，不支持其他字段。
                </p>
                <pre className={styles.transformContract}>
                  {outputAppendContract}
                </pre>
                <p>
                  有效失败策略固定为
                  allow：跳过失败处理器，保留正文，不插入错误气泡。
                </p>
                {handler.type === "prompt" && (
                  <p>Prompt 追加处理器会额外发起一次模型请求。</p>
                )}
              </div>
            )}
            {handler.outputTransform && (
              <div className={styles.outputTransformDetails}>
                <p>
                  候选回复会暂存，只有最终文本会交付；此处不提供返回字段配置。
                </p>
                <p>
                  有效替换格式为 decision: "allow"，可选
                  hookSpecificOutput.replacementText。
                </p>
                <pre className={styles.transformContract}>
                  {outputTransformContract}
                </pre>
                <p>
                  处理器执行失败或输出无效时遵循失败策略：allow
                  保留当前文本继续，block 终止。
                </p>
                <p>
                  Prompt 返回仍仅支持 allow；command/http 的 Stop 返回 block
                  会终止。
                </p>
                <p>
                  总转换预算默认 30
                  秒，耗尽时硬终止，且不属于失败策略的可编辑项。
                </p>
              </div>
            )}
          </section>
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
                      aria-label="失败策略"
                      disabled={Boolean(handler.outputAppend)}
                      value={String(
                        handler.outputAppend
                          ? "allow"
                          : handler.failPolicy ??
                              (handler.type === "prompt" ? "block" : "allow"),
                      )}
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
                      disabled={Boolean(
                        handler.outputTransform || handler.outputAppend,
                      )}
                      title={
                        handler.outputAppend
                          ? "追加独立消息在每个最终放行轮次执行，不支持 once"
                          : handler.outputTransform
                          ? "转换最终回复开启时必须在每次 Stop 时执行"
                          : undefined
                      }
                      aria-describedby={
                        handler.outputAppend
                          ? "once-output-append-reason"
                          : handler.outputTransform
                          ? "once-output-transform-reason"
                          : undefined
                      }
                      onChange={(once) => onHandlerChange({ once })}
                    />
                  </label>
                  {handler.outputAppend && (
                    <p
                      id="once-output-append-reason"
                      className={styles.onceTransformReason}
                    >
                      追加独立消息在每个最终放行轮次执行，不支持仅执行一次。
                    </p>
                  )}
                  {handler.outputTransform && (
                    <p
                      id="once-output-transform-reason"
                      className={styles.onceTransformReason}
                    >
                      转换最终回复开启后，必须在每次 Stop 时执行。
                    </p>
                  )}
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
        <p>Handler 按阶段执行；追加处理器并发生成，按配置顺序展示。</p>
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
      <ManualTestSummary result={testResult} />
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
        <div className={styles.headerActions}>
          <Button
            aria-label="分发 Hook"
            disabled={dirty || distributionGroups.length === 0}
            icon={<SendOutlined />}
            title={dirty ? "请先保存 Hook 配置" : "分发 Hook"}
            onClick={openDistribution}
          />
          <Button type="primary" loading={saving} onClick={() => void save()}>
            保存并激活
          </Button>
        </div>
      </header>
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
                  onEnabledChange={(enabled) => setDraft({ ...draft, enabled })}
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
        groups={editingEvent ? draft.events[editingEvent] ?? [] : []}
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
                  setDraft(
                    replaceEvent(draft, scenario.event, scenario.groups),
                  );
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
        title="分发 Hook"
        open={distributionOpen}
        onCancel={() => {
          if (!distributing) setDistributionOpen(false);
        }}
        onOk={() => void distribute()}
        okText="分发并启用"
        okButtonProps={{
          disabled:
            distributionGroupIds.length === 0 ||
            distributionTenantIds.length === 0,
          loading: distributing,
        }}
        closable={!distributing}
        maskClosable={!distributing}
        width={760}
      >
        <div className={styles.distributionContent}>
          <p>
            分发后将开启目标 Default Agent 的 Hook
            总开关，目标已有分组也会按规则参与执行。
          </p>
          <section className={styles.distributionSection}>
            <h3>选择 Matcher Group</h3>
            <div className={styles.distributionGroupList}>
              {distributionGroups.map(({ event, group, scriptNames }) => (
                <Checkbox
                  key={group.id}
                  aria-label={`选择 ${group.id}`}
                  checked={distributionGroupIds.includes(group.id)}
                  onChange={(change) =>
                    setDistributionGroupIds((current) =>
                      change.target.checked
                        ? [...current, group.id]
                        : current.filter((id) => id !== group.id),
                    )
                  }
                >
                  <span className={styles.distributionGroupName}>
                    {event} · {group.id}
                  </span>
                  <span className={styles.distributionGroupMeta}>
                    {group.hooks.length} 个 Handler
                    {scriptNames.length > 0
                      ? ` · 脚本：${scriptNames.join("、")}`
                      : ""}
                  </span>
                </Checkbox>
              ))}
            </div>
          </section>
          <section className={styles.distributionSection}>
            <h3>选择目标租户</h3>
            <TenantSelector
              selectedTenantIds={distributionTenantIds}
              excludeTenantId={getUserId()}
              onChange={setDistributionTenantIds}
            />
          </section>
          {distributionResult && (
            <section className={styles.distributionResults}>
              <h3>本次结果</h3>
              {distributionResult.results.map((item) => (
                <p key={item.tenant_id}>
                  {item.tenant_id}：{item.success ? "成功" : "失败"}
                  {item.bootstrapped ? "（已初始化）" : ""}
                  {!item.success && item.error ? ` · ${item.error}` : ""}
                </p>
              ))}
            </section>
          )}
        </div>
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
        <ManualTestSummary result={testResult} />
      </Modal>
    </div>
  );
}

export default HookManagementPage;
