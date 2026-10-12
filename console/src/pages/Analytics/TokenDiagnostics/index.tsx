import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Alert,
  Button,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Table,
  Tag,
} from "antd";
import { tokenDiagnosticsApi as api } from "../../../api/modules/tokenDiagnostics";
import type {
  TokenCall,
  TokenCategory,
  TokenDetail,
  TokenFinding,
  TokenItem,
  TokenRun,
} from "../../../api/modules/tokenDiagnostics";
import { useIframeStore } from "../../../stores/iframeStore";
import { getUserId } from "../../../utils/identity";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import {
  categories,
  composition,
  formatTokens as count,
  ruleNames,
  states,
  usageLabel,
} from "./presentation";
import { useResource } from "./useResource";
import styles from "./index.module.less";

function Breakdown({
  rows,
  input,
  output,
  anchor,
}: {
  rows: TokenCategory[];
  input: number | null;
  output: number | null;
  anchor: boolean;
}) {
  const parts = composition(rows, input, output, anchor);
  const total = parts.reduce((sum, x) => sum + x.value, 0);
  let offset = 0;
  if (!total)
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="暂无可展示的分类估算"
      />
    );
  return (
    <div className={styles.chart}>
      <svg
        className={styles.donut}
        viewBox="0 0 160 160"
        role="img"
        aria-label={
          anchor ? "Token 构成，输入分类为估算" : "已捕获输入的估算构成"
        }
      >
        {parts.map((part) => {
          const share = (part.value / total) * 100;
          const start = offset;
          offset += share;
          return (
            <circle
              key={part.category}
              cx="80"
              cy="80"
              r="60"
              pathLength="100"
              fill="none"
              stroke={
                categories[part.category]?.color ??
                categories.unclassified.color
              }
              strokeWidth="16"
              strokeDasharray={`${share} ${100 - share}`}
              strokeDashoffset={-start}
              transform="rotate(-90 80 80)"
            />
          );
        })}
        <text x="80" y="78" textAnchor="middle" fontSize="20" fontWeight="600">
          {count(Math.round(total))}
        </text>
        <text x="80" y="100" textAnchor="middle" fontSize="11" fill="#4b5563">
          {anchor ? "总量" : "输入估算"}
        </text>
      </svg>
      <div className={styles.legend}>
        {parts.map((part) => (
          <div className={styles.legendRow} key={part.category}>
            <span>
              <i
                className={styles.swatch}
                style={{
                  background:
                    categories[part.category]?.color ??
                    categories.unclassified.color,
                }}
              />
              {categories[part.category]?.label ?? part.category}
            </span>
            <span>
              {part.approximate ? "≈" : ""}
              {count(Math.round(part.value))} ·{" "}
              {((part.value / total) * 100).toFixed(1)}%
            </span>
          </div>
        ))}
      </div>
      <p className={styles.muted}>
        {anchor
          ? "输入分类按估算比例对应实际输入总量；输出为 Provider 报告值。"
          : "仅展示已捕获输入的估算；缺失部分未计入。"}
      </p>
    </div>
  );
}

function Issues({
  issues,
  select,
}: {
  issues: TokenFinding[];
  select: (id: string) => void;
}) {
  if (!issues.length)
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="当前可用证据未触发规则"
      />
    );
  return (
    <>
      {issues.map((issue) => (
        <article key={issue.finding_id} className={styles.issue}>
          <Space wrap>
            <strong>{issue.title}</strong>
            <Tag>
              {issue.evidence_quality === "observed" ? "已观测" : "待复核"}
            </Tag>
            <span className={styles.muted}>规则 v{issue.rule_version}</span>
          </Space>
          <p>{issue.action}</p>
          {issue.affected_tokens != null && (
            <p>
              涉及估算 Token：≈{count(issue.affected_tokens)}（不等于可节省量）
            </p>
          )}
          <Space wrap>
            {issue.evidence.call_ids?.map((id) => (
              <Button key={id} size="small" onClick={() => select(id)}>
                查看调用 {id.slice(0, 8)}
              </Button>
            ))}
          </Space>
          <details>
            <summary>查看判断证据</summary>
            <pre className={styles.evidence}>
              {JSON.stringify(issue.evidence, null, 2)}
            </pre>
          </details>
        </article>
      ))}
    </>
  );
}

function CallInspector({
  call,
  scope,
  issues,
  select,
}: {
  call: TokenCall;
  scope: string;
  issues: TokenFinding[];
  select: (id: string) => void;
}) {
  const [offset, setOffset] = useState(0);
  const resource = useResource(`${scope}:${call.call_id}:${offset}`, (signal) =>
    api.inputs(call.call_id, signal, offset),
  );
  const rows = resource.data?.items ?? [];
  const complete =
    offset === 0 &&
    !resource.data?.has_more &&
    call.capture_state === "complete";
  const amounts = new Map<string, number>();
  rows.forEach((item) =>
    amounts.set(
      item.category,
      (amounts.get(item.category) ?? 0) + (item.estimated_tokens ?? 0),
    ),
  );
  const categoriesForCall = [...amounts].map(
    ([category, estimated_tokens]) => ({ category, estimated_tokens }),
  );
  return (
    <section className={styles.section}>
      <div className={styles.callHeader}>
        <h2>调用详情</h2>
        <Tag>{states[call.usage_status] ?? call.usage_status}</Tag>
        <span className={styles.id}>{call.call_id}</span>
      </div>
      <Space wrap>
        <span>实际输入：{count(call.input_tokens)}</span>
        <span>实际输出：{count(call.output_tokens)}</span>
        <span>输入预算：{count(call.max_input_tokens)}</span>
        <span>输出预算：{count(call.max_output_tokens)}</span>
        <span>压缩周期：{call.compaction_epoch}</span>
      </Space>
      <p className={styles.muted}>
        缓存读取：{count(call.usage_details?.cache_read_tokens)} · 缓存写入：
        {count(call.usage_details?.cache_write_tokens)} · 推理：
        {count(call.usage_details?.reasoning_tokens)}（均为总量子项）
      </p>
      {resource.error && <Alert type="error" message={resource.error} />}
      <Spin spinning={resource.loading}>
        <Breakdown
          rows={categoriesForCall}
          input={call.input_tokens}
          output={call.output_tokens}
          anchor={complete && call.usage_status === "reported"}
        />
        <Table<TokenItem>
          size="small"
          rowKey="occurrence_index"
          dataSource={rows}
          pagination={false}
          scroll={{ x: 760 }}
          columns={[
            {
              title: "输入类型",
              dataIndex: "category",
              render: (v: string) => categories[v]?.label ?? v,
            },
            {
              title: "来源引用",
              dataIndex: "source_ref",
              render: (v: string) => <span className={styles.id}>{v}</span>,
            },
            {
              title: "估算 Token",
              dataIndex: "estimated_tokens",
              render: (v: number | null) =>
                v == null ? "未知" : `≈${count(v)}`,
            },
            {
              title: "归因依据",
              dataIndex: "attribution_quality",
              render: (v: string) =>
                v === "explicit" ? "显式来源" : "消息角色",
            },
            {
              title: "内容指纹",
              dataIndex: "fingerprint",
              render: (v: string | null) =>
                v ? (
                  <span title={v} className={styles.id}>
                    {v.slice(0, 12)}…
                  </span>
                ) : (
                  "不可用"
                ),
            },
          ]}
        />
      </Spin>
      <Space style={{ marginTop: 12 }}>
        <Button
          disabled={!offset || resource.loading}
          onClick={() => setOffset(Math.max(0, offset - 100))}
        >
          上一页输入
        </Button>
        <Button
          disabled={!resource.data?.has_more || resource.loading}
          onClick={() => setOffset(offset + 100)}
        >
          下一页输入
        </Button>
      </Space>
      {!!issues.length && (
        <>
          <h3 style={{ marginTop: 20 }}>该调用可能存在的问题</h3>
          <Issues issues={issues} select={select} />
        </>
      )}
    </section>
  );
}

function Detail({ detail, scope }: { detail: TokenDetail; scope: string }) {
  const [branch, setBranch] = useState("");
  const [selectedCall, setSelectedCall] = useState("");
  const visibleCalls = detail.calls.filter(
    (call) => !branch || call.branch_run_id === branch,
  );
  const call = detail.calls.find((item) => item.call_id === selectedCall);
  const maxInput = Math.max(
    1,
    ...visibleCalls.map(
      (item) => item.input_tokens ?? item.estimated_input_tokens ?? 0,
    ),
  );
  const summary = detail.summary;
  const select = (id: string) => {
    setBranch("");
    setSelectedCall(id);
  };
  const ruleRows = detail.branches.flatMap((run) => {
    const rows =
      typeof run.analysis_summary === "string"
        ? JSON.parse(run.analysis_summary)
        : run.analysis_summary;
    return (rows ?? []).map(
      (rule: { rule_id: string; state: string; missing_fields: string[] }) => ({
        ...rule,
        branch: run.branch_run_id,
      }),
    );
  });
  return (
    <section className={styles.section}>
      <div className={styles.sectionHeader}>
        <h2>任务用量与诊断</h2>
        <Space wrap>
          <Tag>{states[detail.analysis_state] ?? detail.analysis_state}</Tag>
          <Tag>{states[summary.capture_state] ?? summary.capture_state}</Tag>
        </Space>
      </div>
      <p className={styles.id}>
        Task {detail.task_run_id} · Trace {detail.run.trace_id ?? "未关联"}
      </p>
      {!summary.usage_complete && (
        <Alert
          type="warning"
          showIcon
          message="用量不完整：下面的已知用量不能视为完整账单。"
          description={`部分报告 ${summary.partial_call_count} 次，未知 ${summary.unknown_call_count} 次。记录与分析可能仍在更新。`}
        />
      )}
      {detail.data_limited && (
        <Alert
          type="info"
          message="当前明细窗口有限，总量单独汇总；分类与规则证据请按分支复核。"
        />
      )}
      <div className={styles.metrics}>
        <div className={styles.metric}>
          <span>
            {usageLabel(summary.usage_complete, summary.known_total_tokens)}
          </span>
          <strong>
            {summary.usage_complete || summary.known_total_tokens > 0
              ? count(summary.known_total_tokens)
              : "未知"}
          </strong>
          <span className={styles.muted}>输入 + 输出</span>
        </div>
        <div className={styles.metric}>
          <span>已知输入</span>
          <strong>{count(summary.known_input_tokens)}</strong>
          <span className={styles.muted}>接口报告值</span>
        </div>
        <div className={styles.metric}>
          <span>已知输出</span>
          <strong>{count(summary.known_output_tokens)}</strong>
          <span className={styles.muted}>接口报告值</span>
        </div>
        <div className={styles.metric}>
          <span>已记录调用</span>
          <strong>{count(summary.observed_call_count)}</strong>
          <span className={styles.muted}>
            {detail.branches.length} 个执行分支
          </span>
        </div>
      </div>
      <div className={styles.overview}>
        <div>
          <h3>Token 类型构成</h3>
          <Breakdown
            rows={detail.categories}
            input={summary.known_input_tokens}
            output={summary.known_output_tokens}
            anchor={summary.usage_complete && !!summary.composition_complete}
          />
        </div>
        <div>
          <h3>潜在问题 · {detail.findings.length}</h3>
          <Issues issues={detail.findings} select={select} />
        </div>
      </div>
      <h3>规则分析状态</h3>
      <Space wrap>
        {ruleRows.map((rule) => (
          <Tag
            key={`${rule.branch}:${rule.rule_id}`}
            title={rule.missing_fields.join(", ")}
          >
            {ruleNames[rule.rule_id] ?? rule.rule_id} ·{" "}
            {rule.state === "evaluated" ? "已评估" : "证据不足"} ·{" "}
            {rule.branch.slice(0, 6)}
          </Tag>
        ))}
      </Space>
      <p className={styles.muted}>
        数据版本 {summary.data_revision} / 已分析 {summary.analyzed_revision}
        ；证据不足时不能据此判断没有问题。
      </p>
      <div className={styles.sectionHeader} style={{ marginTop: 24 }}>
        <h3>调用趋势与明细</h3>
        <Select
          aria-label="执行分支"
          value={branch}
          onChange={(value) => {
            setBranch(value);
            setSelectedCall("");
          }}
          style={{ minWidth: 240 }}
          options={[
            { value: "", label: "全部执行分支" },
            ...detail.branches.map((run) => ({
              value: run.branch_run_id,
              label: `${run.role} · ${run.purpose} · ${run.branch_run_id.slice(
                0,
                8,
              )}`,
            })),
          ]}
        />
      </div>
      <p className={styles.muted}>
        柱高为每次调用的输入用量（未知时显示估算），点击定位调用。每个重试为独立请求次数。
      </p>
      <div className={styles.trend}>
        {visibleCalls.slice(0, 100).map((item) => (
          <button
            key={item.call_id}
            className={`${styles.bar} ${
              selectedCall === item.call_id ? styles.activeBar : ""
            }`}
            style={{
              height: `${Math.max(
                4,
                ((item.input_tokens ?? item.estimated_input_tokens ?? 0) /
                  maxInput) *
                  100,
              )}%`,
            }}
            aria-label={`选择调用 ${item.logical_seq} 重试 ${
              item.attempt_no
            }，输入 ${
              item.input_tokens ?? "估算 " + count(item.estimated_input_tokens)
            }`}
            title={`${item.model_name} · ${count(item.input_tokens)} 输入`}
            onClick={() => setSelectedCall(item.call_id)}
          />
        ))}
      </div>
      <Table<TokenCall>
        rowKey="call_id"
        size="small"
        dataSource={visibleCalls}
        scroll={{ x: 1000 }}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        rowClassName={(item) =>
          item.call_id === selectedCall ? styles.selectedRow : ""
        }
        columns={[
          {
            title: "调用 / 尝试",
            render: (_: unknown, item: TokenCall) =>
              `${item.logical_seq} / ${item.attempt_no}`,
          },
          {
            title: "模型 / 用途",
            render: (_: unknown, item: TokenCall) => (
              <>
                <div>{item.model_name}</div>
                <span className={styles.muted}>
                  {item.provider_id} · {item.purpose}
                </span>
              </>
            ),
          },
          { title: "输入", dataIndex: "input_tokens", render: count },
          { title: "输出", dataIndex: "output_tokens", render: count },
          {
            title: "总量",
            render: (_: unknown, item: TokenCall) =>
              item.input_tokens != null && item.output_tokens != null
                ? `${item.usage_status === "reported" ? "" : "已知 "}${count(
                    item.input_tokens + item.output_tokens,
                  )}`
                : "不完整",
          },
          {
            title: "用量状态",
            dataIndex: "usage_status",
            render: (value: string) => states[value] ?? value,
          },
          {
            title: "问题",
            render: (_: unknown, item: TokenCall) =>
              detail.findings.filter(
                (issue) => issue.evidence.call_ids?.includes(item.call_id),
              ).length,
          },
          {
            title: "操作",
            render: (_: unknown, item: TokenCall) => (
              <Button
                size="small"
                onClick={() => setSelectedCall(item.call_id)}
              >
                查看详情
              </Button>
            ),
          },
        ]}
      />
      {call && (
        <CallInspector
          key={call.call_id}
          call={call}
          scope={scope}
          select={select}
          issues={detail.findings.filter(
            (issue) => issue.evidence.call_ids?.includes(call.call_id),
          )}
        />
      )}
      {!!selectedCall && !call && (
        <Alert
          type="info"
          message="该调用超出当前明细窗口，可根据证据中的调用 ID 进一步查询。"
        />
      )}
    </section>
  );
}

export default function TokenDiagnosticsPage() {
  const source = useIframeStore((store) => store.source) || DEFAULT_SOURCE_ID;
  const user = useIframeStore((store) => store.userId) || getUserId();
  const scope = JSON.stringify([source, user]);
  const [params, setParams] = useSearchParams();
  const task = params.get("task") ?? "";
  const [cursor, setCursor] = useState("");
  const [model, setModel] = useState("");
  const [trace, setTrace] = useState(params.get("trace") ?? "");
  const [lookupTrace, setLookupTrace] = useState(params.get("trace") ?? "");
  const [revision, setRevision] = useState(0);
  const list = useResource(
    `${scope}:${cursor}:${model}:${revision}`,
    (signal) => api.list(signal, cursor, model),
  );
  const detail = useResource(
    task ? `${scope}:${task}:${revision}` : "",
    (signal) => api.detail(task, signal),
  );
  const lookup = useResource(
    lookupTrace ? `${scope}:${lookupTrace}:${revision}` : "",
    (signal) => api.byTrace(lookupTrace, signal),
  );
  const choose = (id: string) => setParams({ task: id });
  return (
    <main className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1>Token 记录与诊断</h1>
          <p className={styles.muted}>
            按任务查看每次请求的用量、输入构成与潜在问题
          </p>
        </div>
        <Button onClick={() => setRevision((value) => value + 1)}>
          刷新记录
        </Button>
      </div>
      <p className={styles.muted}>
        仅统计已观测请求；适配器未透传的用量、Provider 内部重试可能未覆盖。
      </p>
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search
          aria-label="筛选模型"
          placeholder="模型名称"
          allowClear
          onSearch={(value) => {
            setModel(value);
            setCursor("");
          }}
        />
        <Input.Search
          aria-label="按 Trace ID 查找"
          placeholder="Trace ID"
          value={trace}
          onChange={(event) => setTrace(event.target.value)}
          onSearch={(value) => setLookupTrace(value)}
          loading={lookup.loading}
        />
      </Space>
      {list.error && (
        <Alert
          type="error"
          showIcon
          message={list.error}
          action={
            <Button onClick={() => setRevision((value) => value + 1)}>
              重试
            </Button>
          }
        />
      )}
      {lookup.error && <Alert type="error" message={lookup.error} />}
      {lookup.data && (
        <Space wrap style={{ marginBottom: 16 }}>
          {lookup.data.items.length ? (
            lookup.data.items.map((item) => (
              <Button
                key={item.task_run_id}
                onClick={() => choose(item.task_run_id)}
              >
                查看任务 {item.task_run_id.slice(0, 8)}
              </Button>
            ))
          ) : (
            <span>当前范围没有关联的 Token 记录，历史 Trace 不自动回填。</span>
          )}
        </Space>
      )}
      <Table<TokenRun>
        loading={list.loading}
        rowKey="task_run_id"
        size="small"
        dataSource={list.data?.items ?? []}
        pagination={false}
        scroll={{ x: 900 }}
        locale={{
          emptyText: list.error
            ? "数据暂不可用"
            : "暂无记录；启用采集后新请求会在此显示",
        }}
        rowClassName={(run) =>
          run.task_run_id === task ? styles.selectedRow : ""
        }
        columns={[
          {
            title: "开始时间",
            dataIndex: "started_at",
            render: (value: string) =>
              new Date(value).toLocaleString("zh-CN", { hour12: false }),
          },
          {
            title: "Trace / 任务",
            render: (_: unknown, run: TokenRun) => (
              <div className={styles.id}>
                {run.trace_id ?? "未关联 Trace"}
                <br />
                {run.task_run_id.slice(0, 12)}
              </div>
            ),
          },
          {
            title: "已知 Token",
            render: (_: unknown, run: TokenRun) => (
              <>
                {run.usage_complete || (run.known_total_tokens ?? 0) > 0
                  ? count(run.known_total_tokens)
                  : "未知"}
                <div className={styles.muted}>
                  {run.usage_complete ? "总量" : "完整性待确认"}
                </div>
              </>
            ),
          },
          {
            title: "已记录调用",
            dataIndex: "observed_call_count",
            render: count,
          },
          {
            title: "执行 / 分析",
            render: (_: unknown, run: TokenRun) => (
              <>
                {states[run.execution_state] ?? run.execution_state}
                <br />
                <span className={styles.muted}>
                  {states[run.analysis_state] ?? run.analysis_state}
                </span>
              </>
            ),
          },
          {
            title: "操作",
            render: (_: unknown, run: TokenRun) => (
              <Button size="small" onClick={() => choose(run.task_run_id)}>
                用量与诊断
              </Button>
            ),
          },
        ]}
      />
      <Space style={{ marginTop: 12 }}>
        <Button
          disabled={!cursor || list.loading}
          onClick={() => setCursor("")}
        >
          最新记录
        </Button>
        <Button
          disabled={!list.data?.next_cursor || list.loading}
          onClick={() => setCursor(list.data?.next_cursor ?? "")}
        >
          下一页
        </Button>
      </Space>
      {detail.error && (
        <Alert style={{ marginTop: 24 }} type="error" message={detail.error} />
      )}
      {detail.loading && <Spin style={{ display: "block", marginTop: 32 }} />}
      {detail.data && (
        <Detail key={`${scope}:${task}`} detail={detail.data} scope={scope} />
      )}
    </main>
  );
}
