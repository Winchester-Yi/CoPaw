import React, { useCallback, useMemo, useState } from "react";
import { Drawer, Modal } from "antd";
import { IconButton } from "@agentscope-ai/design";
import { SparkOperateRightLine } from "@agentscope-ai/icons";
// ==================== 组件引入方式变更 (Kun He) ====================
import {
  useChatAnywhereSessionsState,
  useChatAnywhereSessions,
  type IAgentScopeRuntimeWebUISession,
} from "@/components/agentscope-chat";
// ==================== 组件引入方式变更结束 ====================
import { useAgentStore } from "@/stores/agentStore";
import { useTranslation } from "react-i18next";
import { chatApi } from "../../../../api/modules/chat";
import sessionApi from "../../sessionApi";
import { getSessionAgentId } from "../../sessionApi/sessionAgent";
import ChatSessionItem from "../ChatSessionItem";
import { getChannelLabel } from "../../../Control/Channels/components";
import styles from "./index.module.less";

/** Sessions from CoPaw backend include extra fields beyond the runtime UI type */
interface ExtendedChatSession extends IAgentScopeRuntimeWebUISession {
  realId?: string;
  sessionId?: string;
  userId?: string;
  channel?: string;
  createdAt?: string | null;
  meta?: Record<string, unknown>;
  status?: "idle" | "running" | "stopping";
}

interface ChatSessionDrawerProps {
  /** Whether the drawer is visible */
  open: boolean;
  /** Callback to close the drawer */
  onClose: () => void;
}

/** Format an ISO 8601 timestamp to YYYY-MM-DD HH:mm:ss */
const formatCreatedAt = (raw: string | null | undefined): string => {
  if (!raw) return "";
  const date = new Date(raw);
  if (isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}`;
};

const getBackendId = (session: ExtendedChatSession): string | null => {
  if (session.realId) return session.realId;
  return /^\d+$/.test(session.id) ? null : session.id;
};

const ChatSessionDrawer: React.FC<ChatSessionDrawerProps> = (props) => {
  const { t } = useTranslation();
  const { sessions, currentSessionId, setCurrentSessionId, setSessions } =
    useChatAnywhereSessionsState();
  const { selectedAgent, setSelectedAgent } = useAgentStore();

  const { createSession } = useChatAnywhereSessions();

  /** Create a new session and close the drawer */
  const handleCreateSession = useCallback(async () => {
    await createSession();
    props.onClose();
  }, [createSession, props.onClose]);

  /** ID of the session currently being renamed */
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  /** Current value of the rename input */
  const [editValue, setEditValue] = useState("");

  /** Sessions sorted by createdAt descending, independent of context internal order */
  const sortedSessions = useMemo(() => {
    return [...sessions].sort((a, b) => {
      const aTime = (a as ExtendedChatSession).createdAt;
      const bTime = (b as ExtendedChatSession).createdAt;
      if (!aTime && !bTime) return 0;
      if (!aTime) return 1;
      if (!bTime) return -1;
      return new Date(bTime).getTime() - new Date(aTime).getTime();
    });
  }, [sessions]);

  /** Re-fetch session list from the backend and sync to context state */
  const refreshSessions = useCallback(async () => {
    const list = await sessionApi.getSessionList();
    setSessions(list);
  }, [setSessions]);

  const handleSessionClick = useCallback(
    (sessionId: string) => {
      const session = sessions.find((item) => item.id === sessionId) as
        | ExtendedChatSession
        | undefined;
      const sessionAgentId = getSessionAgentId(session?.meta);
      if (sessionAgentId && sessionAgentId !== selectedAgent) {
        setSelectedAgent(sessionAgentId);
      }
      setCurrentSessionId(sessionId);
    },
    [selectedAgent, sessions, setCurrentSessionId, setSelectedAgent],
  );

  /** Delete a session and clear any persisted identity mapping */
  const handleDelete = useCallback(
    (sessionId: string, sessionName: string = "新会话") => {
      const nextSessionId =
        currentSessionId === sessionId
          ? sessions.filter((s) => s.id !== sessionId)[0]?.id
          : currentSessionId;

      Modal.confirm({
        title: "删除历史记录",
        content: `确认删除会话“${sessionName}”？删除后无法恢复。`,
        centered: true,
        okText: "删除",
        okType: "danger",
        cancelText: "取消",
        cancelButtonProps: { type: "text" },
        onOk: async () => {
          await sessionApi.removeSession({ id: sessionId });

          if (currentSessionId === sessionId) {
            setCurrentSessionId(nextSessionId);
          }

          await refreshSessions();
        },
      });
    },
    [sessions, currentSessionId, setCurrentSessionId, refreshSessions],
  );

  /** Enter rename mode for a session */
  const handleEditStart = useCallback(
    (sessionId: string, currentName: string) => {
      setEditingSessionId(sessionId);
      setEditValue(currentName);
    },
    [],
  );

  /** Update rename input value */
  const handleEditChange = useCallback((value: string) => {
    setEditValue(value);
  }, []);

  /** Save the title and refresh the list. */
  const handleEditSubmit = useCallback(async () => {
    if (!editingSessionId) return;

    const session = sessions.find((s) => s.id === editingSessionId) as
      | ExtendedChatSession
      | undefined;
    const backendId = session ? getBackendId(session) : null;
    const newName = editValue.trim();

    if (backendId && newName && session) {
      await chatApi.updateChat(backendId, { name: newName });
    }

    setEditingSessionId(null);
    setEditValue("");
    await refreshSessions();
  }, [editingSessionId, editValue, sessions, refreshSessions]);

  /** Cancel rename mode */
  const handleEditCancel = useCallback(() => {
    setEditingSessionId(null);
    setEditValue("");
  }, []);

  return (
    <Drawer
      open={props.open}
      onClose={props.onClose}
      placement="right"
      width={360}
      closable={false}
      title={null}
      styles={{
        header: { display: "none" },
        body: {
          padding: 0,
          display: "flex",
          flexDirection: "column",
          height: "100%",
          overflow: "hidden",
        },
        mask: { background: "transparent" },
      }}
      className={styles.drawer}
    >
      {/* Header bar */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.headerTitle}>{t("chat.allChats")}</span>
        </div>
        <div className={styles.headerRight}>
          <IconButton
            bordered={false}
            icon={<SparkOperateRightLine />}
            onClick={props.onClose}
          />
        </div>
      </div>

      {/* Create new chat button */}
      <div className={styles.createSection}>
        <div className={styles.createButton} onClick={handleCreateSession}>
          {t("chat.createNewChat")}
        </div>
      </div>

      {/* Session list */}
      <div className={styles.listWrapper}>
        <div className={styles.topGradient} />
        <div className={styles.list}>
          {sortedSessions.map((session) => {
            const ext = session as ExtendedChatSession;
            const channelKey = ext.channel?.trim() || "";
            const channelLabel = channelKey
              ? getChannelLabel(channelKey, t)
              : undefined;
            return (
              <ChatSessionItem
                key={session.id}
                name={session.name || "新会话"}
                time={formatCreatedAt(ext.createdAt ?? null)}
                channelKey={channelKey || undefined}
                channelLabel={channelLabel}
                active={session.id === currentSessionId}
                editing={editingSessionId === session.id}
                editValue={
                  editingSessionId === session.id ? editValue : undefined
                }
                onClick={() => handleSessionClick(session.id!)}
                onEdit={() =>
                  handleEditStart(session.id!, session.name || "新会话")
                }
                onDelete={() =>
                  handleDelete(session.id!, session.name || "新会话")
                }
                onEditChange={handleEditChange}
                onEditSubmit={handleEditSubmit}
                onEditCancel={handleEditCancel}
              />
            );
          })}
        </div>
        <div className={styles.bottomGradient} />
      </div>
    </Drawer>
  );
};

export default ChatSessionDrawer;
