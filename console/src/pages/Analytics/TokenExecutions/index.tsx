import { Alert, Button, Empty } from "antd";
import { useEffect, useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  tokenDiagnosisV2Api,
  type DiagnosisFilters,
  type EvaluationLocatorDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import { FilterBar } from "../TokenDiagnosisShared/FilterBar";
import { EvidenceState } from "../TokenDiagnosisShared/EvidenceState";
import {
  formatCount,
  parseDiagnosisFilters,
  RULE_LABELS,
  stateLabel,
  updateDiagnosisSearch,
  validateReturnTo,
} from "../TokenDiagnosisShared/presentation";
import { useDiagnosisResource } from "../TokenDiagnosisShared/useDiagnosisResource";
import {
  CallUsage,
  ContextPanel,
  EvaluationPanel,
  OperationPanel,
  Pager,
  PrefixPanel,
  ResourceNotice,
} from "./panels";
import styles from "./index.module.less";

const CURSORS = [
  "branches_cursor",
  "calls_cursor",
  "occurrences_cursor",
  "candidates_cursor",
  "operations_cursor",
  "relation_cursor",
];
const CALL_LOCATORS = ["call_id", "context_id", "evaluation_id"];
function scopedFilters(filters: DiagnosisFilters): DiagnosisFilters {
  return { ...filters, cursor: undefined };
}
function resourceKey(
  kind: string,
  id: string,
  filters: DiagnosisFilters,
  extras?: unknown,
) {
  return JSON.stringify([kind, id, filters, extras]);
}
function locator(search: URLSearchParams, key: string) {
  const value = search.get(key);
  return value &&
    value.length <= 128 &&
    !Array.from(value).some((char) => char.charCodeAt(0) < 32)
    ? value
    : undefined;
}
function RuleButtons({
  items,
  limited,
  selected,
  onSelect,
}: {
  items: EvaluationLocatorDTO[];
  limited: boolean;
  selected?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <>
      <div className={styles.rules}>
        {items.map((evaluation) => (
          <Button
            key={evaluation.evaluation_id}
            aria-pressed={evaluation.evaluation_id === selected}
            onClick={() => onSelect(evaluation.evaluation_id)}
          >
            {RULE_LABELS[evaluation.rule_id]} · {stateLabel(evaluation.state)} ·{" "}
            {stateLabel(evaluation.freshness)}
          </Button>
        ))}
      </div>
      {limited && <p>规则实例列表受限</p>}
      {items.length === 0 && (
        <p>当前对象无已保存规则实例；分析可能待评估或证据未保存</p>
      )}
    </>
  );
}
export default function TokenExecutionsPage() {
  const { taskId = "" } = useParams();
  const [search, setSearch] = useSearchParams();
  const filters = useMemo(
    () => scopedFilters(parseDiagnosisFilters(search)),
    [search],
  );
  const taskResource = useDiagnosisResource(
    resourceKey("task", taskId, filters),
    (signal) => tokenDiagnosisV2Api.task(taskId, signal, filters),
    !!taskId,
  );
  const taskMatches =
    taskResource.data?.task_run_id === taskId &&
    taskResource.data.root.task_run_id === taskId;
  const task = taskMatches ? taskResource.data : undefined;
  const window = task?.window;
  const pinned =
    !!window && filters.start === window.start && filters.end === window.end;
  useEffect(() => {
    if (!window || pinned) return;
    const next = new URLSearchParams(search);
    next.set("start", window.start);
    next.set("end", window.end);
    setSearch(next, { replace: true });
  }, [window, pinned, search, setSearch]);
  const branchId = filters.branch_run_id || task?.root.branch_run_id || "";
  const branchFilters = { ...filters, branch_run_id: branchId };
  const branchesFilters = {
    ...filters,
    branch_run_id: undefined,
    cursor: search.get("branches_cursor") || undefined,
  };
  const branches = useDiagnosisResource(
    resourceKey("branches", taskId, branchesFilters),
    (signal) => tokenDiagnosisV2Api.branches(taskId, signal, branchesFilters),
    !!task && pinned,
  );
  const callsFilters = {
    ...branchFilters,
    cursor: search.get("calls_cursor") || undefined,
  };
  const calls = useDiagnosisResource(
    resourceKey("calls", taskId, callsFilters),
    (signal) => tokenDiagnosisV2Api.calls(taskId, signal, callsFilters),
    !!task && pinned && !!branchId,
  );
  const pageBranches =
    branches.data?.items.filter((item) => item.task_run_id === taskId) || [];
  const branchesMismatch = branches.data?.items.some(
    (item) => item.task_run_id !== taskId,
  );
  const branch = pageBranches.find((item) => item.branch_run_id === branchId);
  const requestedCall = locator(search, "call_id");
  const explicitContext = locator(search, "context_id");
  const explicitEvaluation = locator(search, "evaluation_id");
  const pageCalls =
    calls.data?.items.filter(
      (item) => item.task_run_id === taskId && item.branch_run_id === branchId,
    ) || [];
  // Never bind an explicit deep locator to an arbitrary call from a limited page.
  const selectedCall =
    pageCalls.find((call) => call.call_id === requestedCall) ||
    (!requestedCall && explicitContext
      ? pageCalls.find((call) => call.context_id === explicitContext)
      : undefined) ||
    (!requestedCall && !explicitContext && explicitEvaluation
      ? pageCalls.find((call) =>
          call.evaluations.items.some(
            (item) => item.evaluation_id === explicitEvaluation,
          ),
        )
      : undefined) ||
    (!requestedCall && !explicitContext && !explicitEvaluation
      ? pageCalls[0]
      : undefined);
  const contextId = explicitContext || selectedCall?.context_id || "";
  const contextFilters = {
    ...branchFilters,
    cursor: search.get("occurrences_cursor") || undefined,
  };
  const context = useDiagnosisResource(
    resourceKey("context", contextId, contextFilters, {
      taskId,
      requestedCall,
      selectedCall: selectedCall?.call_id,
    }),
    (signal) => tokenDiagnosisV2Api.context(contextId, signal, contextFilters),
    !!contextId && pinned && !!branchId,
  );
  const contextRecord = context.data?.context;
  const contextMatches =
    contextRecord &&
    contextRecord.context_id === contextId &&
    contextRecord.task_run_id === taskId &&
    contextRecord.branch_run_id === branchId &&
    (!requestedCall || contextRecord.call_id === requestedCall) &&
    (!selectedCall || contextRecord.call_id === selectedCall.call_id);
  const evaluationId =
    explicitEvaluation ||
    selectedCall?.evaluations.items[0]?.evaluation_id ||
    "";
  const evaluation = useDiagnosisResource(
    resourceKey("evaluation", evaluationId, branchFilters, {
      taskId,
      requestedCall,
      selectedCall: selectedCall?.call_id,
    }),
    (signal) =>
      tokenDiagnosisV2Api.evaluation(evaluationId, signal, branchFilters),
    !!evaluationId && pinned && !!branchId,
  );
  const evaluationRecord = evaluation.data?.evaluation;
  const evaluationMatches =
    evaluationRecord &&
    evaluationRecord.evaluation_id === evaluationId &&
    evaluationRecord.task_run_id === taskId &&
    evaluationRecord.branch_run_id === branchId &&
    (evaluationRecord.object_type !== "call" ||
      ((!requestedCall || evaluationRecord.object_id === requestedCall) &&
        (!selectedCall ||
          evaluationRecord.object_id === selectedCall.call_id)));
  const prefixCallId =
    selectedCall?.call_id ||
    (requestedCall && pageCalls.some((call) => call.call_id === requestedCall)
      ? requestedCall
      : "");
  const prefixFilters = {
    ...branchFilters,
    cursor: search.get("candidates_cursor") || undefined,
  };
  const prefix = useDiagnosisResource(
    resourceKey("prefix", prefixCallId, prefixFilters, { taskId, contextId }),
    (signal) => tokenDiagnosisV2Api.prefix(prefixCallId, signal, prefixFilters),
    !!prefixCallId && pinned,
  );
  const prefixMatches =
    prefix.data &&
    prefix.data.call_id === prefixCallId &&
    (!prefix.data.comparison ||
      (prefix.data.comparison.call_id === prefixCallId &&
        prefix.data.comparison.current_context_id === contextId));
  const prefixRevisionMismatch =
    contextMatches &&
    prefix.data?.comparison &&
    prefix.data.comparison.current_context_revision !==
      contextRecord.context_revision;
  const prefixOccurrenceBinding =
    contextMatches &&
    prefix.data?.comparison &&
    prefix.data.comparison.current_context_id === contextRecord.context_id &&
    prefix.data.comparison.current_context_revision ===
      contextRecord.context_revision;
  const operationId = locator(search, "operation_id");
  const operationFilters = {
    ...branchFilters,
    cursor: search.get("operations_cursor") || undefined,
  };
  const operations = useDiagnosisResource(
    resourceKey("operations", taskId, operationFilters),
    (signal) =>
      tokenDiagnosisV2Api.operations(taskId, signal, operationFilters),
    !!task && pinned && !!branchId,
  );
  const relationCursor = search.get("relation_cursor") || undefined;
  const relationOptions = {
    operation_id: operationId,
    relation_cursor: relationCursor,
  };
  const relation = useDiagnosisResource(
    resourceKey("relations", taskId, branchFilters, relationOptions),
    (signal) =>
      tokenDiagnosisV2Api.operations(
        taskId,
        signal,
        branchFilters,
        relationOptions,
      ),
    !!operationId && pinned && !!branchId,
  );
  function update(
    updates: Record<string, string | undefined>,
    clear: string[] = [],
  ) {
    const next = new URLSearchParams(search);
    for (const key of clear) next.delete(key);
    for (const [key, value] of Object.entries(updates)) {
      if (value === undefined) next.delete(key);
      else next.set(key, value);
    }
    setSearch(next);
  }
  function selectCall(callId: string) {
    update({ call_id: callId }, [
      "context_id",
      "evaluation_id",
      "occurrences_cursor",
      "candidates_cursor",
    ]);
  }
  function callHref(callId: string) {
    const next = new URLSearchParams(search);
    for (const key of [
      ...CALL_LOCATORS,
      "calls_cursor",
      "occurrences_cursor",
      "candidates_cursor",
    ])
      next.delete(key);
    next.set("call_id", callId);
    return `?${next}`;
  }
  function contextHref(id: string) {
    const next = new URLSearchParams(search);
    next.set("context_id", id);
    for (const key of [
      "call_id",
      "evaluation_id",
      "occurrences_cursor",
      "candidates_cursor",
    ])
      next.delete(key);
    return `?${next}`;
  }
  function page(name: string, key: string, next?: string | null) {
    return (
      <Pager
        name={name}
        cursor={search.get(key) || undefined}
        next={next}
        pinned={pinned}
        onChange={(cursor) => update({ [key]: cursor })}
      />
    );
  }
  const operationRows =
    operations.data?.items.filter(
      (item) => item.task_run_id === taskId && item.branch_run_id === branchId,
    ) || [];
  const relationOperation = relation.data?.items.find(
    (item) =>
      item.operation_id === operationId &&
      item.task_run_id === taskId &&
      item.branch_run_id === branchId,
  );
  const validReturn = validateReturnTo(search.get("returnTo"));
  const fallbackSearch = new URLSearchParams(search);
  for (const key of [
    ...CALL_LOCATORS,
    ...CURSORS,
    "operation_id",
    "returnTo",
    "branch_run_id",
  ])
    fallbackSearch.delete(key);
  const returnTo =
    validReturn || `/analytics/token-overview?${fallbackSearch}#task-list`;
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link to={returnTo}>返回问题或任务列表</Link>
        <h1>执行详情</h1>
        <code>{taskId}</code>
      </header>
      <FilterBar
        filters={filters}
        window={window}
        showRule={false}
        showState={false}
        onChange={(nextFilters) => {
          const next = updateDiagnosisSearch(search, nextFilters);
          for (const key of [...CURSORS, ...CALL_LOCATORS, "operation_id"])
            next.delete(key);
          setSearch(next);
        }}
      />
      <ResourceNotice resource={taskResource} name="任务" />
      {taskResource.data && !taskMatches && (
        <Alert
          type="warning"
          message="任务证据与请求的 Task 不匹配；未绑定摘要。"
        />
      )}
      {task && (
        <>
          <section className={styles.section} aria-label="任务用量范围">
            <h2>任务用量范围</h2>
            <div className={styles.rows}>
              <div className={styles.row}>
                <strong>任务完整生命周期 · 含所有已记录分支</strong>
                <span>
                  已知总量{" "}
                  {formatCount(task.lifecycle_usage.known_total_tokens)} ·
                  真实总量{" "}
                  {formatCount(task.lifecycle_usage.actual_total_tokens)}
                </span>
              </div>
              <div className={styles.row}>
                <strong>筛选选中贡献 · 不等同于任务总量</strong>
                <span>
                  已知总量{" "}
                  {formatCount(task.selected_contribution.known_total_tokens)} ·
                  真实总量{" "}
                  {formatCount(task.selected_contribution.actual_total_tokens)}
                </span>
              </div>
              <div className={styles.row}>
                <strong>窗口物理 attempt · 按开始时间纳入</strong>
                <span>
                  已知总量{" "}
                  {formatCount(task.window_attempt_usage.known_total_tokens)} ·
                  真实总量{" "}
                  {formatCount(task.window_attempt_usage.actual_total_tokens)}
                </span>
              </div>
            </div>
            <p>
              范围：{task.scope.source_id} / {task.scope.tenant_id} ·{" "}
              {window?.start} → {window?.end} · UTC [start,end)
            </p>
            <EvidenceState
              state={task.completeness.capture_state}
              limited={
                task.branches_limited || task.completeness.gap_tracking_limited
              }
            />
            <p>
              任务分支 {formatCount(task.completeness.branch_count)} · 已观察
              Call {formatCount(task.completeness.observed_call_count)} / 预期
              attempt {formatCount(task.completeness.expected_attempt_count)} ·
              缺少子分支 {formatCount(task.completeness.missing_child_count)} ·
              gap {formatCount(task.completeness.gap_count)} · Usage{" "}
              {task.completeness.usage_complete ? "完整" : "不完整"}
            </p>
          </section>
          <div className={styles.workspace}>
            <aside className={styles.navigator}>
              <section aria-label="分支列表">
                <h2>分支</h2>
                <p className={styles.caption}>
                  分支分别展示；父子关系不表示串行顺序。
                </p>
                <ResourceNotice resource={branches} name="分支" />
                {branchesMismatch && (
                  <Alert
                    type="warning"
                    message="分支证据与请求的 Task 不匹配；已排除外部任务分支。"
                  />
                )}
                {branches.data?.items.length === 0 && (
                  <Empty
                    description="当前页无分支"
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                  />
                )}
                {pageBranches.map((item) => (
                  <div key={item.branch_run_id} className={styles.listItem}>
                    <Button
                      className={styles.selection}
                      aria-label={`选择分支 ${item.branch_run_id}`}
                      aria-pressed={item.branch_run_id === branchId}
                      onClick={() =>
                        update({ branch_run_id: item.branch_run_id }, [
                          ...CALL_LOCATORS,
                          ...CURSORS.filter((key) => key !== "branches_cursor"),
                          "operation_id",
                        ])
                      }
                    >
                      <strong>{item.agent_id || "Agent 未知"}</strong>
                      <code>{item.branch_run_id}</code>
                    </Button>
                    <span>
                      父分支：{item.parent_branch_run_id || "无已记录父分支"}
                    </span>
                    <span>
                      Phase：{item.phase_id || "未知"} ·{" "}
                      {stateLabel(item.execution_state)} ·{" "}
                      {stateLabel(item.analysis_state)}
                    </span>
                    <EvidenceState state={item.freshness} />
                  </div>
                ))}
                {page("分支", "branches_cursor", branches.data?.next_cursor)}
              </section>
              <section aria-label="Call 列表">
                <h2>Calls / attempts</h2>
                <ResourceNotice resource={calls} name="Call" />
                {calls.data?.items.length === 0 && (
                  <Empty
                    description="当前页无 Call"
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                  />
                )}
                {pageCalls.map((call, index) => (
                  <div className={styles.listItem} key={call.call_id}>
                    {(index === 0 ||
                      pageCalls[index - 1].model_name !== call.model_name ||
                      pageCalls[index - 1].purpose !== call.purpose) && (
                      <h3>
                        {call.model_name || "模型未知"} /{" "}
                        {call.purpose || "用途未知"}
                      </h3>
                    )}
                    <Button
                      className={styles.selection}
                      aria-label={`选择 Call ${call.call_id}`}
                      aria-pressed={selectedCall?.call_id === call.call_id}
                      onClick={() => selectCall(call.call_id)}
                    >
                      <strong>
                        Call {formatCount(call.logical_seq)} · attempt{" "}
                        {formatCount(call.attempt_no)}
                      </strong>
                      <code>{call.call_id}</code>
                    </Button>
                    <span>Logical Call：{call.logical_call_id || "未知"}</span>
                    <span>
                      {call.model_name || "模型未知"} ·{" "}
                      {call.purpose || "Phase 未知"}
                    </span>
                    <span>
                      业务 Step：
                      {call.identity_source === "business" && call.step_id
                        ? call.step_id
                        : "业务 Step 身份未记录"}
                    </span>
                  </div>
                ))}
                {page("Call", "calls_cursor", calls.data?.next_cursor)}
              </section>
            </aside>
            <main className={styles.inspector}>
              <section className={styles.section} aria-label="当前分支范围">
                <h2>当前分支：{branchId}</h2>
                <p className={styles.caption}>
                  分支与 Call
                  明细按当前筛选读取；上方任务生命周期汇总包含全部分支。
                </p>
                {!branch && branches.state === "ready" && (
                  <p>请求分支尚未在当前页解析；可翻页继续查找</p>
                )}
                {branch && (
                  <>
                    <p>
                      Phase：{branch.phase_id || "未知"} · 分支分析{" "}
                      {stateLabel(branch.analysis_state)}
                    </p>
                    <h3>业务 Steps</h3>
                    {!branch.steps?.order_known && (
                      <p>业务 Step 顺序未知；不推断先后关系。</p>
                    )}
                    <EvidenceState
                      state={branch.steps?.order_known ? "ready" : "unknown"}
                      reason={branch.steps?.reason_code}
                      limited={branch.steps?.limited}
                    />
                    {!branch.steps?.items.length && (
                      <p>未保存真实业务 Step；Call 序号与重试不作为 Step。</p>
                    )}
                    {branch.steps?.items.map((step) => (
                      <div key={step.step_id} className={styles.row}>
                        <code>{step.step_id}</code>
                        <span>
                          事件 {formatCount(step.event_count)} ·{" "}
                          {step.started_at || "开始时间未知"} →{" "}
                          {step.ended_at || "结束时间未知"}
                        </span>
                        <RuleButtons
                          items={step.evaluations.items}
                          limited={step.evaluations.limited}
                          selected={evaluationId}
                          onSelect={(id) => update({ evaluation_id: id })}
                        />
                      </div>
                    ))}
                    <h3>分支规则实例</h3>
                    <RuleButtons
                      items={branch.evaluations.items}
                      limited={branch.evaluations.limited}
                      selected={evaluationId}
                      onSelect={(id) => update({ evaluation_id: id })}
                    />
                  </>
                )}
              </section>
              {requestedCall && !selectedCall && calls.state === "ready" && (
                <Alert
                  type="warning"
                  message={`请求的 Call 尚未在当前页解析：${requestedCall}；可继续翻页，或证据已过期。`}
                />
              )}
              {selectedCall && (
                <>
                  <CallUsage call={selectedCall} />
                  <section
                    className={styles.section}
                    aria-label="Call 规则实例"
                  >
                    <h2>当前 Call 规则实例</h2>
                    <RuleButtons
                      items={selectedCall.evaluations.items}
                      limited={selectedCall.evaluations.limited}
                      selected={evaluationId}
                      onSelect={(id) => update({ evaluation_id: id })}
                    />
                  </section>
                </>
              )}
              <ResourceNotice resource={context} name="Context" />
              {context.data && !contextMatches && (
                <Alert
                  type="warning"
                  message="Context 与请求的 Call 不匹配，或 task / branch 归属不匹配；未绑定该证据。"
                />
              )}
              {context.data && contextMatches && (
                <ContextPanel
                  data={context.data}
                  page={page(
                    "occurrences",
                    "occurrences_cursor",
                    context.data.occurrences.next_cursor,
                  )}
                />
              )}
              {explicitContext && contextMatches && !selectedCall && (
                <p>
                  Context 已定位；其 Call {contextRecord.call_id || "未知"}{" "}
                  尚未在当前页解析，Usage 保持未知。
                </p>
              )}
              {explicitEvaluation &&
                evaluationMatches &&
                !selectedCall &&
                evaluationRecord.object_type === "call" && (
                  <p>
                    规则实例已定位；其 Call {evaluationRecord.object_id}{" "}
                    尚未在当前页解析，Usage 保持未知。
                  </p>
                )}
              {selectedCall && !contextId && (
                <p>当前 Call 的 Context 证据缺失</p>
              )}
              <ResourceNotice resource={prefix} name="前缀" />
              {prefix.data && !prefixMatches && (
                <Alert
                  type="warning"
                  message="前缀证据与当前 Call / Context 不匹配"
                />
              )}
              {prefixRevisionMismatch && prefix.data?.is_current && (
                <Alert
                  type="warning"
                  message="当前前缀保存修订与 Context 修订不一致；此比较不能作为当前证据。"
                />
              )}
              {prefixRevisionMismatch && !prefix.data?.is_current && (
                <Alert
                  type="warning"
                  message="历史前缀修订与当前 Context 不一致；首差字节区间未知，保留已保存历史 bounds。"
                />
              )}
              {prefix.data &&
                prefixMatches &&
                !(prefixRevisionMismatch && prefix.data.is_current) && (
                  <PrefixPanel
                    data={prefix.data}
                    occurrences={
                      prefixOccurrenceBinding
                        ? context.data?.occurrences.items
                        : undefined
                    }
                    page={page(
                      "前缀候选",
                      "candidates_cursor",
                      prefix.data.candidates.next_cursor,
                    )}
                  />
                )}
              <ResourceNotice resource={evaluation} name="规则实例" />
              {evaluation.data && !evaluationMatches && (
                <Alert
                  type="warning"
                  message="规则实例与当前 task / branch / Call 不匹配；未绑定该证据。"
                />
              )}
              {evaluation.data && evaluationMatches && (
                <EvaluationPanel data={evaluation.data} href={contextHref} />
              )}
              <section className={styles.section} aria-label="分支操作">
                <h2>分支操作</h2>
                <ResourceNotice resource={operations} name="操作" />
                <ResourceNotice resource={relation} name="操作关系" />
                {operations.data?.items.length === 0 && (
                  <Empty
                    description="当前页无操作"
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                  />
                )}
                {operationId &&
                  relation.state === "ready" &&
                  !relationOperation && (
                    <p>操作定位证据缺失或已过期：{operationId}</p>
                  )}
                {operationRows.map((operation) => {
                  if (
                    operation.operation_id === operationId &&
                    !relationOperation
                  )
                    return null;
                  const shown =
                    operation.operation_id === operationId && relationOperation
                      ? relationOperation
                      : operation;
                  return (
                    <OperationPanel
                      key={operation.operation_id}
                      operation={shown}
                      callHref={callHref}
                      page={
                        <Pager
                          name={`关系 ${operation.operation_id}`}
                          cursor={
                            operationId === operation.operation_id
                              ? relationCursor
                              : undefined
                          }
                          next={shown.segments.next_cursor}
                          pinned={pinned}
                          onChange={(cursor) =>
                            update({
                              operation_id: operation.operation_id,
                              relation_cursor: cursor,
                            })
                          }
                        />
                      }
                    />
                  );
                })}
                {operationId &&
                  relationOperation &&
                  !operationRows.some(
                    (operation) => operation.operation_id === operationId,
                  ) && (
                    <OperationPanel
                      operation={relationOperation}
                      callHref={callHref}
                      page={
                        <Pager
                          name={`关系 ${operationId}`}
                          cursor={relationCursor}
                          next={relationOperation.segments.next_cursor}
                          pinned={pinned}
                          onChange={(cursor) =>
                            update({ relation_cursor: cursor })
                          }
                        />
                      }
                    />
                  )}
                {page(
                  "操作",
                  "operations_cursor",
                  operations.data?.next_cursor,
                )}
              </section>
            </main>
          </div>
        </>
      )}
    </div>
  );
}
