import { useId, useState } from "react";
import { Button, Input } from "antd";
import {
  DIAGNOSIS_RULE_IDS,
  type DiagnosisFilters,
  type DiagnosisWindow,
} from "../../../api/modules/tokenDiagnosisV2";
import { QUERY_STATES, RULE_LABELS, stateLabel } from "./presentation";
import styles from "./index.module.less";
interface FilterBarProps {
  filters: DiagnosisFilters;
  onChange: (filters: DiagnosisFilters) => void;
  window?: DiagnosisWindow;
  showRule?: boolean;
  showState?: boolean;
}
function utcInput(value?: string) {
  return value && Number.isFinite(Date.parse(value))
    ? new Date(value).toISOString().slice(0, 16)
    : "";
}
function FilterFields({
  filters,
  onChange,
  window,
  showRule,
  showState,
}: FilterBarProps) {
  const id = useId();
  const originalStart = filters.start || window?.start;
  const originalEnd = filters.end || window?.end;
  const [draft, setDraft] = useState({
    ...filters,
    start: utcInput(originalStart),
    end: utcInput(originalEnd),
  });
  const [error, setError] = useState("");
  const change = (key: keyof DiagnosisFilters, value: string) =>
    setDraft((previous) => ({ ...previous, [key]: value }));
  const field = (
    key: "start" | "end" | "agent_id" | "root_agent_id" | "model_name",
    label: string,
    type = "text",
  ) => (
    <label className={styles.field} htmlFor={`${id}-${key}`}>
      <span>{label}</span>
      <Input
        id={`${id}-${key}`}
        type={type}
        value={draft[key] || ""}
        onChange={(event) => change(key, event.target.value)}
        maxLength={key === "model_name" ? 256 : 64}
      />
    </label>
  );
  function apply(event: React.FormEvent) {
    event.preventDefault();
    // Preserve saved microseconds and cursor-bound window strings when only
    // another filter changes; datetime controls display minute precision.
    const selectedEnd =
      originalEnd && draft.end === utcInput(originalEnd)
        ? originalEnd
        : draft.end
        ? `${draft.end}Z`
        : undefined;
    const selectedStart =
      originalStart && draft.start === utcInput(originalStart)
        ? originalStart
        : draft.start
        ? `${draft.start}Z`
        : undefined;
    const end = selectedEnd ? new Date(selectedEnd) : new Date();
    const start = selectedStart
      ? new Date(selectedStart)
      : new Date(end.getTime() - 24 * 60 * 60 * 1000);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) {
      setError("请输入有效的 UTC 时间");
      return;
    }
    if (start >= end) {
      setError("开始时间须早于结束时间");
      return;
    }
    if (end.getTime() - start.getTime() > 30 * 24 * 60 * 60 * 1000) {
      setError("时间范围最多 30 天");
      return;
    }
    setError("");
    onChange({
      ...filters,
      ...draft,
      start:
        originalStart && selectedStart === originalStart
          ? originalStart
          : start.toISOString(),
      end:
        originalEnd && selectedEnd === originalEnd
          ? originalEnd
          : end.toISOString(),
      cursor: undefined,
      agent_id: draft.agent_id || undefined,
      root_agent_id: draft.root_agent_id || undefined,
      model_name: draft.model_name || undefined,
      rule_id: draft.rule_id || undefined,
      state: draft.state || undefined,
    });
  }
  return (
    <form className={styles.filterBar} onSubmit={apply}>
      <div className={styles.fields}>
        {field("start", "开始时间（UTC）", "datetime-local")}
        {field("end", "结束时间（UTC）", "datetime-local")}
        {field("agent_id", "执行 Agent")}
        {field("root_agent_id", "根 Agent 归属")}
        {field("model_name", "模型")}
        {showRule && (
          <label className={styles.field} htmlFor={`${id}-rule`}>
            <span>规则</span>
            <select
              id={`${id}-rule`}
              value={draft.rule_id || ""}
              onChange={(event) => change("rule_id", event.target.value)}
            >
              <option value="">全部规则</option>
              {DIAGNOSIS_RULE_IDS.map((rule) => (
                <option key={rule} value={rule}>
                  {RULE_LABELS[rule]}
                </option>
              ))}
            </select>
          </label>
        )}
        {showState && (
          <label className={styles.field} htmlFor={`${id}-state`}>
            <span>状态</span>
            <select
              id={`${id}-state`}
              value={draft.state || ""}
              onChange={(event) => change("state", event.target.value)}
            >
              <option value="">全部状态</option>
              {QUERY_STATES.map((state) => (
                <option key={state} value={state}>
                  {stateLabel(state)}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className={styles.actions}>
          <Button htmlType="submit" type="primary">
            应用筛选
          </Button>
          <Button
            onClick={() =>
              onChange({
                start: originalStart,
                end: originalEnd,
                limit: filters.limit,
              })
            }
          >
            重置筛选
          </Button>
        </div>
      </div>
      <div className={styles.hint}>
        UTC 半开区间 [开始, 结束)，默认最近 24 小时，最多 30 天；执行 Agent 与根
        Agent 归属分别筛选。
      </div>
      {error && (
        <div role="alert" className={styles.error}>
          {error}
        </div>
      )}
    </form>
  );
}
export function FilterBar(props: FilterBarProps) {
  return (
    <FilterFields
      key={JSON.stringify([
        props.filters,
        props.filters.start || props.window?.start,
        props.filters.end || props.window?.end,
      ])}
      {...props}
    />
  );
}
