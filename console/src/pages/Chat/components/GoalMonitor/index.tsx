import { useCallback, useEffect, useMemo, useState } from "react";
import {
  PauseOutlined,
  PlayCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { chatApi } from "../../../../api/modules/chat";
import type { GoalSnapshot, GoalState } from "../../../../api/types/chat";
import { useAppMessage } from "../../../../hooks/useAppMessage";
import styles from "./index.module.less";

const POLL_INTERVAL_MS = 10_000;
const ACTIVE_STATES = new Set(["ACTIVE", "WAITING"]);
const TERMINAL_STATES = new Set(["COMPLETE", "CANCELLED"]);
const STATE_LABELS: Record<GoalState, string> = {
  ACTIVE: "执行中",
  WAITING: "等待中",
  PAUSED: "已暂停",
  BLOCKED: "已阻塞",
  LIMITED: "已达预算",
  INTERRUPTED: "已中断",
  COMPLETE: "已完成",
  CANCELLED: "已取消",
};

function criterionPercent(goal: GoalSnapshot): number {
  if (!goal.criteria.length) return 0;
  return Math.round(
    (goal.criteria.filter((criterion) => criterion.verified).length /
      goal.criteria.length) *
      100,
  );
}

export default function GoalMonitor({
  chatId,
  onResume,
}: {
  chatId: string | null;
  onResume?: (goalId: string) => void;
}) {
  const { message } = useAppMessage();
  const [goal, setGoal] = useState<GoalSnapshot | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [acting, setActing] = useState(false);
  const [editing, setEditing] = useState(false);
  const [objectiveDraft, setObjectiveDraft] = useState("");
  const [criteriaDraft, setCriteriaDraft] = useState("");
  const [mustPreserveDraft, setMustPreserveDraft] = useState("");
  const [mustNotDoDraft, setMustNotDoDraft] = useState("");
  const [autonomyDraft, setAutonomyDraft] = useState("");

  const refresh = useCallback(async () => {
    if (!chatId) return setGoal(null);
    try {
      setGoal(await chatApi.getRecentGoal(chatId));
    } catch {
      setGoal(null);
    }
  }, [chatId]);

  useEffect(() => {
    setExpanded(false);
    void refresh();
  }, [refresh]);

  useEffect(() => {
    window.addEventListener("goal-authorization-changed", refresh);
    return () =>
      window.removeEventListener("goal-authorization-changed", refresh);
  }, [refresh]);

  useEffect(() => {
    if (!goal || !ACTIVE_STATES.has(goal.state)) return undefined;
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [goal, refresh]);

  const passedCount = useMemo(
    () => goal?.criteria.filter((criterion) => criterion.verified).length ?? 0,
    [goal],
  );
  const progressPercent = useMemo(
    () => (goal ? criterionPercent(goal) : 0),
    [goal],
  );

  const act = useCallback(
    async (action: "pause" | "resume" | "cancel") => {
      if (!goal) return;
      setActing(true);
      try {
        const next =
          action === "pause"
            ? await chatApi.pauseGoal(goal.goal_id, chatId || "")
            : action === "resume"
            ? await chatApi.resumeGoal(goal.goal_id, chatId || "")
            : await chatApi.cancelGoal(goal.goal_id, chatId || "");
        setGoal(next);
        if (action === "resume" && next.state === "ACTIVE") {
          onResume?.(next.goal_id);
        }
      } catch {
        message.error("目标控制请求失败");
        void refresh();
      } finally {
        setActing(false);
      }
    },
    [chatId, goal, message, onResume, refresh],
  );

  const submitEdit = useCallback(async () => {
    if (!goal || !objectiveDraft.trim()) return;
    setActing(true);
    try {
      const criteria = JSON.parse(criteriaDraft);
      const next = await chatApi.editGoal(goal.goal_id, chatId || "", {
        objective: objectiveDraft.trim(),
        completion_criteria: criteria,
        constraints: {
          must_preserve: mustPreserveDraft
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean),
          must_not_do: mustNotDoDraft
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean),
        },
        autonomy_boundary: autonomyDraft.trim(),
      });
      setGoal(next);
      setEditing(false);
    } catch {
      message.error("合同编辑提交失败，请检查完成条件格式");
    } finally {
      setActing(false);
    }
  }, [
    autonomyDraft,
    chatId,
    criteriaDraft,
    goal,
    message,
    mustNotDoDraft,
    mustPreserveDraft,
    objectiveDraft,
  ]);

  if (!goal) return null;
  return (
    <div className={styles.anchor}>
      {expanded ? (
        <section className={styles.panel} aria-label="目标运行状态">
          <button
            type="button"
            className={styles.header}
            aria-expanded="true"
            onClick={() => setExpanded(false)}
          >
            <span className={styles.activityDots} aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span className={styles.title}>目标运行状态</span>
            <span className={styles.status}>{STATE_LABELS[goal.state]}</span>
          </button>
          <div className={styles.content}>
            <strong className={styles.objective}>
              {goal.contract.objective}
            </strong>
            <span className={styles.progress}>
              已通过 {passedCount} / {goal.criteria.length} 项条件
            </span>
            <span className={styles.progress}>
              子代理委派：
              {goal.subagent_delegation_enabled === undefined
                ? "状态未知"
                : goal.subagent_delegation_enabled
                ? "已授权"
                : "未授权"}
            </span>
            <div
              className={styles.progressTrack}
              role="progressbar"
              aria-label="Goal 完成条件审查进度"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progressPercent}
            >
              <div
                className={styles.progressFill}
                style={{ width: `${progressPercent}%` }}
              />
            </div>
            <ul className={styles.criteria} aria-label="完成条件">
              {goal.criteria.map((item) => (
                <li key={item.criterion_id} data-verified={item.verified}>
                  <span>{item.verified ? "已通过" : "待审查"}</span>
                  {item.criterion.requirement}
                </li>
              ))}
            </ul>
            {goal.state_reason || goal.next_focus ? (
              <p className={styles.reason}>
                {goal.state_reason || goal.next_focus}
              </p>
            ) : null}
            <div className={styles.controls}>
              <button
                type="button"
                aria-label="编辑目标合同"
                disabled={acting}
                onClick={() => {
                  setObjectiveDraft(goal.contract.objective);
                  setCriteriaDraft(
                    JSON.stringify(goal.contract.completion_criteria, null, 2),
                  );
                  setMustPreserveDraft(
                    goal.contract.constraints.must_preserve.join("\n"),
                  );
                  setMustNotDoDraft(
                    goal.contract.constraints.must_not_do.join("\n"),
                  );
                  setAutonomyDraft(goal.contract.autonomy_boundary);
                  setEditing(true);
                }}
              >
                编辑合同
              </button>
              {ACTIVE_STATES.has(goal.state) ? (
                <button
                  type="button"
                  aria-label="暂停目标"
                  disabled={acting}
                  onClick={() => void act("pause")}
                >
                  <PauseOutlined />
                  暂停
                </button>
              ) : null}
              {!ACTIVE_STATES.has(goal.state) &&
              !TERMINAL_STATES.has(goal.state) ? (
                <button
                  type="button"
                  aria-label="恢复目标"
                  disabled={acting}
                  onClick={() => void act("resume")}
                >
                  <PlayCircleOutlined />
                  恢复
                </button>
              ) : null}
              {!TERMINAL_STATES.has(goal.state) ? (
                <button
                  type="button"
                  aria-label="取消目标"
                  disabled={acting}
                  onClick={() => void act("cancel")}
                >
                  <StopOutlined />
                  取消
                </button>
              ) : null}
            </div>
            {editing ? (
              <div className={styles.editor}>
                <label htmlFor="goal-objective">目标</label>
                <input
                  id="goal-objective"
                  value={objectiveDraft}
                  onChange={(event) => setObjectiveDraft(event.target.value)}
                />
                <label htmlFor="goal-criteria">完成条件（JSON）</label>
                <textarea
                  id="goal-criteria"
                  value={criteriaDraft}
                  onChange={(event) => setCriteriaDraft(event.target.value)}
                />
                <label htmlFor="goal-must-preserve">必须保留（每行一项）</label>
                <textarea
                  id="goal-must-preserve"
                  value={mustPreserveDraft}
                  onChange={(event) => setMustPreserveDraft(event.target.value)}
                />
                <label htmlFor="goal-must-not-do">禁止操作（每行一项）</label>
                <textarea
                  id="goal-must-not-do"
                  value={mustNotDoDraft}
                  onChange={(event) => setMustNotDoDraft(event.target.value)}
                />
                <label htmlFor="goal-autonomy">自主边界</label>
                <textarea
                  id="goal-autonomy"
                  value={autonomyDraft}
                  onChange={(event) => setAutonomyDraft(event.target.value)}
                />
                <button
                  type="button"
                  aria-label="提交合同编辑"
                  disabled={acting || !objectiveDraft.trim()}
                  onClick={() => void submitEdit()}
                >
                  提交
                </button>
              </div>
            ) : null}
          </div>
        </section>
      ) : (
        <button
          type="button"
          className={styles.trigger}
          aria-expanded="false"
          aria-label="目标运行状态"
          onClick={() => setExpanded(true)}
        >
          <span className={styles.activityDots} aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </button>
      )}
    </div>
  );
}
