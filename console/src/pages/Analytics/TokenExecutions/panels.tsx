import { Alert, Button, Empty, Skeleton } from "antd";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import {
  type CallRecordDTO,
  type ContextDTO,
  type EvaluationDTO,
  type OperationDTO,
  type OccurrenceDTO,
  type PrefixDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import { EvidenceState } from "../TokenDiagnosisShared/EvidenceState";
import {
  formatCount,
  formatEstimate,
  reasonLabel,
  RULE_LABELS,
  stateLabel,
} from "../TokenDiagnosisShared/presentation";
import styles from "./index.module.less";

export function ResourceNotice({
  resource,
  name,
}: {
  resource: { state: string; error?: string; reload: () => void };
  name: string;
}) {
  if (resource.state === "loading")
    return (
      <div role="status" aria-label={`加载${name}`}>
        <Skeleton active paragraph={{ rows: 2 }} />
      </div>
    );
  if (["error", "not_found", "unavailable"].includes(resource.state))
    return (
      <Alert
        type="warning"
        showIcon
        message={resource.error}
        action={<Button onClick={resource.reload}>重试{name}</Button>}
      />
    );
  return null;
}
export function Pager({
  name,
  cursor,
  next,
  pinned,
  onChange,
}: {
  name: string;
  cursor?: string;
  next: string | null | undefined;
  pinned: boolean;
  onChange: (cursor?: string) => void;
}) {
  return (
    <div className={styles.pager}>
      <span>仅当前页证据；每页受 limit 限制</span>
      {cursor && <Button onClick={() => onChange()}>首页{name}</Button>}
      <Button
        aria-label={`下一页 ${name}`}
        disabled={!next || !pinned}
        onClick={() => next && onChange(next)}
      >
        下一页{name}
      </Button>
    </div>
  );
}
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <dt>{label}</dt>
      <dd>{children ?? "未知"}</dd>
    </div>
  );
}
export function CallUsage({
  call,
  title = "当前 Call Usage",
}: {
  call: CallRecordDTO;
  title?: string;
}) {
  return (
    <section className={styles.section} aria-label={title}>
      <h2>{title}</h2>
      <code>{call.call_id}</code>
      <dl className={styles.fields}>
        <Field label="输入 Token">{formatCount(call.input_tokens)}</Field>
        <Field label="输出">
          输出 Token：{formatCount(call.output_tokens)}
        </Field>
        <Field label="实际总量">
          {formatCount(
            call.input_tokens == null || call.output_tokens == null
              ? null
              : call.input_tokens + call.output_tokens,
          )}
        </Field>
        <Field label="估算输入">
          {formatEstimate(call.estimated_input_tokens)}
        </Field>
        <Field label="来源 / 精度">
          {call.usage_origin || "未知"} / {call.usage_accuracy || "未知"}
        </Field>
        <Field label="模型 / Provider">
          {call.model_name || "未知"} / {call.provider_id || "未知"}
        </Field>
        <Field label="输入 / 输出预算">
          {formatCount(call.max_input_tokens)} /{" "}
          {formatCount(call.max_output_tokens)}
        </Field>
        <Field label="assembly stage / 格式可信度">
          {call.assembly_stage || "未知"} /{" "}
          {call.prompt_format_confidence || "未知"}
        </Field>
        <Field label="Context builder / estimator">
          {call.context_builder_version || "未知"} /{" "}
          {call.estimator_version || "未知"}
        </Field>
        <Field label="缓存读取 / 写入 / reasoning">
          {formatCount(call.usage_details.cache_read_tokens)} /{" "}
          {formatCount(call.usage_details.cache_write_tokens)} /{" "}
          {formatCount(call.usage_details.reasoning_tokens)}
        </Field>
        <Field label="finish reason">{call.finish_reason || "未知"}</Field>
      </dl>
      <EvidenceState state={call.usage_status} />
      <EvidenceState state={call.execution_state} />
      <EvidenceState state={call.capture_state} />
    </section>
  );
}
export function ContextPanel({
  data,
  page,
}: {
  data: ContextDTO;
  page: ReactNode;
}) {
  const { context, composition, occurrences } = data;
  return (
    <section className={styles.section} aria-label="Context 构成">
      <h2>Context 构成</h2>
      <code>{context.context_id}</code>
      <EvidenceState
        state={context.capture_state}
        reason={context.capture_reason}
      />
      <dl className={styles.fields}>
        <Field label="构成总估算">
          {formatEstimate(composition.estimated_tokens)}
        </Field>
        <Field label="已知部分估算">
          {formatEstimate(composition.known_estimated_tokens)}
        </Field>
        <Field label="范围">{composition.population}</Field>
        <Field label="表示 / 密钥 / 估算器版本">
          {context.representation_version || "未知"} /{" "}
          {context.key_version || "未知"} /{" "}
          {context.estimator_version || "未知"}
        </Field>
        <Field label="文本 / 编码字节">
          {formatCount(context.text_bytes)} /{" "}
          {formatCount(context.encoded_bytes)}
        </Field>
      </dl>
      {composition.complete ? (
        <p>构成归因 / 估算完整</p>
      ) : (
        <Alert
          type="info"
          showIcon
          message="构成归因 / 估算不完整"
          description={`${reasonLabel(
            composition.reason_code,
          )}；类别未知 ${formatCount(
            composition.unknown_category_count,
          )}；归因未知 ${formatCount(composition.unknown_attribution_count)}`}
        />
      )}
      <div className={styles.rows}>
        {composition.categories.map((category) => (
          <div className={styles.row} key={category.category || "unknown"}>
            <strong>{category.category || "类别未知"}</strong>
            <span>{formatEstimate(category.estimated_tokens)}</span>
            <span>
              已知部分 {formatCount(category.known_estimated_tokens)} ·
              occurrence {formatCount(category.occurrence_count)}
            </span>
          </div>
        ))}
      </div>
      <h3>Context occurrences</h3>
      {occurrences.items.length === 0 && (
        <Empty
          description="无已保存 occurrence"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      )}
      <div className={styles.rows}>
        {occurrences.items.map((item) => (
          <div className={styles.row} key={item.occurrence_index}>
            <strong>
              Occurrence {item.occurrence_index} · {item.category || "类别未知"}
            </strong>
            <span>
              来源：{item.source_ref || item.source_object_id || "未知"} / 版本{" "}
              {item.source_version || "未知"}
            </span>
            <span>
              归因：{item.attribution || "未知"}；可靠性：
              {["explicit", "structure"].includes(item.attribution || "")
                ? "已记录可靠来源"
                : "未知"}
              ；生命周期：
              {stateLabel(item.lifecycle_state)}
            </span>
            <span>
              {formatEstimate(item.estimated_tokens)} · 文本字节 [
              {formatCount(item.text_start)}, {formatCount(item.text_end)})
            </span>
            <span>
              producer operation：{item.producer_operation_id || "未知"} · 业务
              Step：{item.business_step_id || "未知"}
            </span>
          </div>
        ))}
      </div>
      {page}
    </section>
  );
}
export function PrefixPanel({
  data,
  page,
  occurrences = [],
}: {
  data: PrefixDTO;
  page: ReactNode;
  occurrences?: OccurrenceDTO[];
}) {
  const { comparison, candidates } = data;
  function differenceRange(index: number | null) {
    const occurrence =
      index == null
        ? undefined
        : occurrences.find((item) => item.occurrence_index === index);
    return occurrence?.text_start != null && occurrence.text_end != null
      ? `[${formatCount(occurrence.text_start)}, ${formatCount(
          occurrence.text_end,
        )})`
      : "未知（当前页无首差 occurrence 字节边界）";
  }
  return (
    <section className={styles.section} aria-label="文本前缀证据">
      <h2>文本前缀证据</h2>
      <p className={styles.caption}>
        本地已保存的文本字节前缀比较；不代表 Provider 缓存命中或节省费用。
      </p>
      <EvidenceState
        state={comparison?.analysis_state || "missing"}
        reason={data.reason_code || comparison?.reason_code}
        expired={data.evidence_expired}
        limited={comparison?.candidate_limited}
      />
      {comparison && (
        <>
          <p>
            {data.is_current ? "当前已保存比较" : "已保存历史比较（已落后）"} ·{" "}
            {comparison.eligible ? "符合当前比较条件" : "不纳入当前统计"}
          </p>
          <dl className={styles.fields}>
            <Field label="当前 Context">
              <code>{comparison.current_context_id}</code>
            </Field>
            <Field label="下界历史 Context">
              {comparison.best_lower_context_id || "未知"}
            </Field>
            <Field label="上界历史 Context">
              {comparison.max_upper_context_id || "未知"}
            </Field>
            <Field label="实际候选数 / 检索上限">
              {formatCount(comparison.candidate_count)} /{" "}
              {formatCount(comparison.candidate_limit)}
            </Field>
            <Field label="下界 / 上界 / 总文本字节">
              {formatCount(comparison.lower_text_bytes)} /{" "}
              {formatCount(comparison.upper_text_bytes)} /{" "}
              {formatCount(comparison.total_text_bytes)}
            </Field>
            <Field label="首个差异 occurrence 索引">
              {formatCount(comparison.first_difference_index)}
            </Field>
            <Field label="首差 occurrence 文本字节区间">
              {differenceRange(comparison.first_difference_index)}
            </Field>
            <Field label="表示 / 密钥 / selection 版本">
              {comparison.representation_version || "未知"} /{" "}
              {comparison.key_version || "未知"} /{" "}
              {comparison.selection_version || "未知"}
            </Field>
            <Field label="当前修订 / input revision">
              {comparison.current_context_revision} /{" "}
              {comparison.input_revision}
            </Field>
            <Field label="历史检索时间窗">{comparison.window_hours} 小时</Field>
          </dl>
        </>
      )}
      <h3>历史候选 Context</h3>
      {candidates.items.length === 0 && <p>当前页无历史候选</p>}
      <div className={styles.rows}>
        {candidates.items.map((candidate) => (
          <div className={styles.row} key={candidate.candidate_index}>
            <code>{candidate.historical_context_id}</code>
            <span>
              历史 Call：{candidate.historical_call_id || "未知"} · 修订{" "}
              {candidate.historical_context_revision}
            </span>
            <span>
              文本下界 / 上界：{formatCount(candidate.lower_text_bytes)} /{" "}
              {formatCount(candidate.upper_text_bytes)} bytes
            </span>
            <span>
              首差 occurrence：{formatCount(candidate.first_difference_index)} ·
              首差 occurrence 文本字节区间{" "}
              {differenceRange(candidate.first_difference_index)}
            </span>
            <EvidenceState
              state={candidate.evidence_expired ? "expired" : "ready"}
            />
          </div>
        ))}
      </div>
      {page}
    </section>
  );
}
export function EvaluationPanel({
  data,
  href,
}: {
  data: EvaluationDTO;
  href?: (contextId: string) => string;
}) {
  const { evaluation, recommendation } = data;
  return (
    <section className={styles.section} aria-label="规则实例详情">
      <h2>{RULE_LABELS[evaluation.rule_id]}</h2>
      <code>{evaluation.evaluation_id}</code>
      <EvidenceState state={evaluation.state} />
      <p>
        {data.is_current ? "当前规则实例" : "已保存历史规则实例（已落后）"} ·
        input revision {evaluation.input_revision} · rule{" "}
        {evaluation.rule_version} · threshold {evaluation.threshold_version}
      </p>
      <p>
        Snapshot：{evaluation.snapshot_id} · {evaluation.object_type}：
        {evaluation.object_id}
      </p>
      <div className={styles.rows}>
        {data.values.map((value) => (
          <div className={styles.row} key={value.value_key}>
            <strong>{value.value_key}</strong>
            <span>
              {value.enum_value ??
                formatCount(value.integer_value ?? value.decimal_value)}{" "}
              {value.unit}
            </span>
            <span>
              下界 {formatCount(value.lower_bound)} · 上界{" "}
              {formatCount(value.upper_bound)} · 阈值{" "}
              {formatCount(value.threshold)}
            </span>
          </div>
        ))}
      </div>
      {data.gaps.map((gap) => (
        <p key={gap.reason_code}>{reasonLabel(gap.reason_code)}</p>
      ))}
      {data.refs.map((ref) => (
        <div className={styles.row} key={ref.reference_index}>
          <span>
            {ref.reference_type}：{ref.object_id}
          </span>
          {href && (ref.context_id || ref.reference_type === "context") && (
            <Link to={href(ref.context_id || ref.object_id)}>
              查看引用 Context
            </Link>
          )}
          <EvidenceState state={ref.evidence_expired ? "expired" : "ready"} />
        </div>
      ))}
      {data.findings.length === 0 && <p>此实例没有候选 Finding</p>}
      {data.findings.map((finding) => (
        <p key={finding.finding_id}>
          {finding.instance_key} · {finding.recommendation_code} ·{" "}
          {finding.reliable_grouping ? "可靠分组" : "分组身份不可靠"}
        </p>
      ))}
      <EvidenceState
        state={null}
        limited={
          data.values_limited ||
          data.gaps_limited ||
          data.refs_limited ||
          data.findings_limited
        }
      />
      {recommendation && (
        <div>
          <h3>建议：{recommendation.action_code}</h3>
          <p>位置：{recommendation.location}</p>
          {(
            [
              ["前置条件", recommendation.prerequisites],
              ["例外", recommendation.exceptions],
              ["风险", recommendation.risks],
              ["验证", recommendation.verification],
            ] as const
          ).map(([name, items]) => (
            <div key={name}>
              <strong>{name}</strong>
              <ul>
                {items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
function comparableOperation(operation: OperationDTO) {
  const before = operation.before_context,
    after = operation.after_context;
  return (
    ["completed", "success"].includes(operation.operation_state || "") &&
    operation.capture_state === "complete" &&
    operation.missing_before === false &&
    operation.missing_after === false &&
    before &&
    after &&
    !operation.evidence_expired.before_context &&
    !operation.evidence_expired.after_context &&
    [before, after].every(
      (context) =>
        context.task_run_id === operation.task_run_id &&
        context.branch_run_id === operation.branch_run_id &&
        context.operation_id === operation.operation_id &&
        context.capture_state === "complete" &&
        context.estimated_tokens != null,
    ) &&
    before.context_id === operation.before_context_id &&
    after.context_id === operation.after_context_id &&
    before.context_role === "before" &&
    after.context_role === "after" &&
    before.representation_version &&
    before.representation_version === after.representation_version &&
    before.estimator_version &&
    before.estimator_version === after.estimator_version &&
    operation.comparison.unit === "estimated_tokens" &&
    !operation.comparison.reason_code
  );
}
export function OperationPanel({
  operation,
  page,
  callHref,
}: {
  operation: OperationDTO;
  page: ReactNode;
  callHref: (callId: string) => string;
}) {
  const saved = comparableOperation(operation)
    ? operation.comparison.saved_estimated_tokens
    : null;
  const summary =
    operation.summary_call?.call_id === operation.summary_call_id &&
    operation.summary_call.task_run_id === operation.task_run_id &&
    operation.summary_call.branch_run_id === operation.branch_run_id
      ? operation.summary_call
      : null;
  return (
    <section
      className={styles.section}
      aria-label={`操作 ${operation.operation_id}`}
    >
      <h3>
        {operation.operation_type || "操作类型未知"} · {operation.operation_id}
      </h3>
      <EvidenceState
        state={operation.operation_state}
        reason={operation.comparison.reason_code}
      />
      <p>
        触发原因：{operation.trigger_reason || "未知"} · 采集：
        {stateLabel(operation.capture_state)}
      </p>
      <dl className={styles.fields}>
        <Field label="操作 Before Context">
          <code>{operation.before_context_id || "未知"}</code>
          <span>
            {" "}
            ·{" "}
            {operation.evidence_expired.before_context
              ? "证据已过期"
              : formatEstimate(operation.before_context?.estimated_tokens)}
          </span>
        </Field>
        <Field label="操作 After Context">
          <code>{operation.after_context_id || "未知"}</code>
          <span>
            {" "}
            ·{" "}
            {operation.evidence_expired.after_context
              ? "证据已过期"
              : formatEstimate(operation.after_context?.estimated_tokens)}
          </span>
        </Field>
      </dl>
      <p>
        服务器估算减少：{saved == null ? "未知" : `${formatCount(saved)} Token`}
      </p>
      <p>业务质量：未知</p>
      <p>摘要 Call Usage 独立于 Context 规模差；不据此推算真实净节省。</p>
      {operation.summary_call_id && (
        <Link to={callHref(operation.summary_call_id)}>
          查看摘要 Call {operation.summary_call_id}
        </Link>
      )}
      {operation.evidence_expired.summary_call ? (
        <p>摘要 Call 证据已过期 · Usage 未知</p>
      ) : summary ? (
        <CallUsage call={summary} title="摘要 Call Usage" />
      ) : (
        <p>摘要 Call Usage 未知 / 证据缺失</p>
      )}
      <h3>操作 occurrence 关系</h3>
      {operation.segments.items.length === 0 && <p>当前页无关系证据</p>}
      <div className={styles.rows}>
        {operation.segments.items.map((segment) => (
          <div className={styles.row} key={segment.relation_index}>
            <strong>{segment.relation_type}</strong>
            <span>
              Before：{segment.before_context_id || "未知"} /{" "}
              {formatCount(segment.before_occurrence_index)}
            </span>
            <span>
              After：{segment.after_context_id || "未知"} /{" "}
              {formatCount(segment.after_occurrence_index)}
            </span>
            <span>
              来源：{segment.source_object_id || "未知"} /{" "}
              {segment.source_version || "未知"}
            </span>
          </div>
        ))}
      </div>
      {page}
    </section>
  );
}
