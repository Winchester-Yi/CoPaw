import { Button, Tag } from "antd";
import { ArrowRight, ListChecks } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { wplusSopApi } from "@/api/modules/wplusSop";
import type {
  WPlusSopEntryProposal,
  WPlusSopSession,
} from "@/api/types/wplusSop";
import { getWPlusSopStateLabel } from "@/pages/WPlusSopWorkspace/sessionView";
import type { WPlusSopChatProjection } from "../../wplusSopEntryEvents";
import WPlusSopEntryCard from "../WPlusSopEntryCard";
import styles from "./index.module.less";

interface WPlusSopActiveBarProps {
  chatId?: string;
  logicalSessionId?: string;
  projection?: WPlusSopChatProjection;
  onLocksChatInputChange?: (locked: boolean) => void;
}

export default function WPlusSopActiveBar({
  chatId,
  logicalSessionId,
  projection,
  onLocksChatInputChange,
}: WPlusSopActiveBarProps) {
  const navigate = useNavigate();
  const [session, setSession] = useState<WPlusSopSession | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setSession(null);
    const entryProposal = projection?.entryProposal;
    const pendingProposal = entryProposal?.status === "pending";
    const targetSessionId =
      entryProposal?.status === "confirmed" && entryProposal.session_id
        ? entryProposal.session_id
        : projection?.session?.session_id;

    const refresh = () => {
      if (pendingProposal) {
        setSession(null);
        return;
      }
      const request = targetSessionId
        ? wplusSopApi.getSession(targetSessionId, controller.signal)
        : chatId
        ? wplusSopApi.getActiveSession(chatId, controller.signal)
        : null;
      if (!request) {
        setSession(null);
        return;
      }
      void request
        .then((snapshot) =>
          setSession((current) =>
            current &&
            snapshot &&
            current.session_id === snapshot.session_id &&
            current.state_version > snapshot.state_version
              ? current
              : snapshot,
          ),
        )
        .catch(() => {
          if (!controller.signal.aborted) setSession(null);
        });
    };

    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    refresh();
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      controller.abort();
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [
    chatId,
    projection?.entryProposal,
    projection?.entryProposal?.proposal_id,
    projection?.entryProposal?.session_id,
    projection?.entryProposal?.status,
    projection?.session?.session_id,
    projection?.session?.state_version,
  ]);

  const entryProposal = projection?.entryProposal;
  const projectedSession = projection?.session;
  const visibleSession =
    projectedSession &&
    session?.session_id === projectedSession.session_id &&
    projectedSession.state_version > session.state_version
      ? projectedSession
      : session || projectedSession;
  const locksChatInput = Boolean(
    visibleSession &&
      visibleSession.state !== "Paused" &&
      visibleSession.state !== "Completed" &&
      visibleSession.state !== "Terminated",
  );

  useEffect(() => {
    onLocksChatInputChange?.(locksChatInput);
    return () => onLocksChatInputChange?.(false);
  }, [locksChatInput, onLocksChatInputChange]);

  if (entryProposal?.status === "pending") {
    const restoredProposal: WPlusSopEntryProposal = {
      object: "wplus_sop_entry_proposal",
      status: "completed",
      proposal_id: entryProposal.proposal_id,
      mode: entryProposal.mode,
      chat_id: chatId || "",
      session_id: logicalSessionId || chatId || "",
      title: "进入 W+ SOP 工作台",
      message: "工作台将完成逐环节澄清、系统预跑和反馈重跑。",
    };
    return (
      <aside className={styles.entryOverlay} aria-label="待确认的 W+ SOP 入口">
        <WPlusSopEntryCard data={restoredProposal} />
      </aside>
    );
  }

  if (!visibleSession) {
    if (entryProposal?.status === "confirmed") {
      return (
        <aside className={styles.bar} aria-label="已确认的 W+ SOP 工作台">
          <div className={styles.mark}>
            <ListChecks size={16} />
          </div>
          <div className={styles.copy}>
            <strong>已确认进入 W+ SOP</strong>
            <span>正在从 Chat 投影恢复工作台信息</span>
          </div>
          {entryProposal.session_id ? (
            <Button
              type="primary"
              size="small"
              icon={<ArrowRight size={14} />}
              iconPosition="end"
              onClick={() =>
                navigate(`/wplus-sop/${entryProposal.session_id}?from=chat`)
              }
            >
              返回工作台
            </Button>
          ) : (
            <Tag color="processing">正在同步</Tag>
          )}
        </aside>
      );
    }
    return null;
  }

  const terminal =
    visibleSession.state === "Completed" ||
    visibleSession.state === "Terminated";
  const paused = visibleSession.state === "Paused";

  return (
    <aside className={styles.bar} aria-label="当前 Chat 的 W+ SOP 工作台投影">
      <div className={styles.mark}>
        <ListChecks size={16} />
      </div>
      <div className={styles.copy}>
        <strong>
          {terminal
            ? "这个 Chat 的 W+ SOP 已结束"
            : paused
            ? "这个 Chat 的 W+ SOP 已暂停"
            : "这个 Chat 正在进行 W+ SOP"}
        </strong>
        <span>{visibleSession.title}</span>
      </div>
      <Tag color={terminal ? "default" : paused ? "gold" : "cyan"}>
        {getWPlusSopStateLabel(visibleSession.state)}
      </Tag>
      <Button
        type="primary"
        size="small"
        icon={<ArrowRight size={14} />}
        iconPosition="end"
        onClick={() =>
          navigate(`/wplus-sop/${visibleSession.session_id}?from=chat`)
        }
      >
        {terminal ? "查看工作台" : paused ? "继续工作" : "返回工作台"}
      </Button>
    </aside>
  );
}
