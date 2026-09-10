import { useEffect, useRef, useState } from "react";
import { Alert, Button, Space, Switch, Tag } from "antd";
import {
  batchOperations,
  type BatchRunState,
} from "../../../../api/modules/batchOperations";
import styles from "./BatchRunStateControl.module.less";

type Parent = { id: string; meta?: Record<string, unknown> | null };

function isSavedBatchParent(job: Parent): boolean {
  const mode = job.meta?.broadcast_dispatch_intents_enabled;
  return (
    (mode === true || mode === 1 || mode === "true" || mode === "1") &&
    !job.meta?.broadcast_source_job_id
  );
}

export default function BatchRunStateControl({
  job,
  disabled = false,
}: {
  job: Parent;
  disabled?: boolean;
}) {
  const eligible = isSavedBatchParent(job);
  const [snapshot, setSnapshot] = useState<BatchRunState | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  const submitLock = useRef(false);

  useEffect(() => {
    const current = ++generation.current;
    setSnapshot(null);
    setError("");
    setSaving(false);
    submitLock.current = false;
    setLoading(eligible);
    if (eligible) {
      batchOperations
        .getRunState(job.id)
        .then((state) => {
          if (current === generation.current) setSnapshot(state);
        })
        .catch((cause) => {
          if (current === generation.current)
            setError(
              cause instanceof Error
                ? cause.message
                : "批调度状态加载失败，请确认服务已升级并完成迁移",
            );
        })
        .finally(() => {
          if (current === generation.current) setLoading(false);
        });
    }
    return () => {
      generation.current = current + 1;
    };
  }, [job.id, eligible, refresh]);

  const state = snapshot?.parent_job_id === job.id ? snapshot : null;
  const change = async (running: boolean) => {
    if (!state || disabled || loading || error || submitLock.current) return;
    const current = generation.current;
    submitLock.current = true;
    setSaving(true);
    try {
      const result = await batchOperations.setRunState(
        job.id,
        !running,
        state.version,
      );
      if (current === generation.current) setSnapshot(result);
    } catch (cause) {
      if (current === generation.current)
        setError(
          cause instanceof Error
            ? cause.message
            : "保存失败，请刷新确认实际状态",
        );
    } finally {
      if (current === generation.current) {
        setSaving(false);
        submitLock.current = false;
      }
    }
  };

  if (!eligible) return null;
  return (
    <section className={styles.control} aria-label="独立批调度控制">
      <Space wrap size={12}>
        <span>批调度运行状态</span>
        <Switch
          aria-label="批调度运行状态"
          checked={state ? !state.paused : false}
          checkedChildren="运行"
          unCheckedChildren="暂停"
          loading={loading || saving}
          disabled={disabled || loading || saving || !state || !!error}
          onChange={change}
        />
        <Tag>
          {loading
            ? "加载中"
            : error
            ? "状态待确认"
            : state?.paused
            ? "已暂停"
            : "运行中"}
        </Tag>
        <Button
          size="small"
          disabled={saving || loading}
          onClick={() => setRefresh((value) => value + 1)}
        >
          刷新状态
        </Button>
      </Space>
      <p className={styles.hint}>
        {state?.paused
          ? "已暂停后续调度；交接中及运行中任务自然收尾"
          : "独立于任务自身开关；暂停不会切回普通调度"}
      </p>
      {state && state.inflight_count > 0 ? (
        <span className={styles.hint}>
          交接中／运行中 {state.inflight_count} 个
        </span>
      ) : null}
      {error ? <Alert type="error" showIcon message={error} /> : null}
    </section>
  );
}
