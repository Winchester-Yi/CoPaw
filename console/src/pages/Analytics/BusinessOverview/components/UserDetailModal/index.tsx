import { useState, useEffect, useCallback, useMemo } from "react";
import { Modal, Spin, Empty, message } from "antd";
import { User } from "lucide-react";
import {
  tracingApi,
  UserStats,
  SessionStats,
  SessionListItem,
  SessionResourceFilter,
} from "../../../../../api/modules/tracing";
import type { ChatSummary } from "../../../../../api/types";
import { UserDetailModalProps } from "../../types";
import UserStatsHeader from "./UserStatsHeader";
import SessionCardList from "./SessionCardList";
import ReadOnlySessionChat from "./ReadOnlySessionChat";
import styles from "./index.module.less";

const DEFAULT_SESSIONS_PAGE_SIZE = 10;

export default function UserDetailModal({
  open,
  userId,
  userName,
  startDate,
  endDate,
  bbkIds,
  onClose,
}: UserDetailModalProps) {
  // 用户统计状态
  const [userStats, setUserStats] = useState<UserStats | null>(null);
  const [userLoading, setUserLoading] = useState(false);

  // 会话统计状态
  const [sessionStats, setSessionStats] = useState<SessionStats | null>(null);
  const [statsCollapsed, setStatsCollapsed] = useState(false);

  // 会话列表状态
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [sessionsTotal, setSessionsTotal] = useState(0);
  const [sessionsPage, setSessionsPage] = useState(1);
  const [sessionsPageSize, setSessionsPageSize] = useState(
    DEFAULT_SESSIONS_PAGE_SIZE,
  );
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(false);
  const [hasErrorFilter, setHasErrorFilter] = useState(false);
  const [resourceFilter, setResourceFilter] =
    useState<SessionResourceFilter | null>(null);
  const [hasAutoSelectedSession, setHasAutoSelectedSession] = useState(false);
  const [chatSpecs, setChatSpecs] = useState<ChatSummary[]>([]);

  // 聊天记录状态
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);

  const chatIdBySessionId = useMemo(
    () =>
      Object.fromEntries(
        (Array.isArray(chatSpecs) ? chatSpecs : []).map((chat) => [
          chat.session_id,
          chat.id,
        ]),
      ),
    [chatSpecs],
  );

  // 获取用户统计
  const fetchUserStats = useCallback(async () => {
    if (!userId) return;
    setUserLoading(true);
    try {
      const data = await tracingApi.getUserStats(userId, startDate, endDate, bbkIds);
      setUserStats(data);
    } catch (error) {
      console.error("Failed to fetch user stats:", error);
      message.error("获取用户统计失败");
    } finally {
      setUserLoading(false);
    }
  }, [userId, startDate, endDate, bbkIds]);

  // 获取会话列表
  const fetchSessions = useCallback(async (page: number, pageSize: number) => {
    if (!userId) return;
    setSessionsLoading(true);
    try {
      const data = await tracingApi.getSessions(page, pageSize, {
        user_id: userId,
        bbk_ids: bbkIds,
        start_date: startDate,
        end_date: endDate,
        has_error: hasErrorFilter ? true : undefined,
        resource_type: resourceFilter?.type,
        resource_name: resourceFilter?.name,
        mcp_server:
          resourceFilter?.type === "mcp_tool"
            ? resourceFilter.mcp_server
            : undefined,
      });
      setSessions(data.items || []);
      setSessionsTotal(data.total || 0);
    } catch (error) {
      console.error("Failed to fetch sessions:", error);
      message.error("获取会话列表失败");
    } finally {
      setSessionsLoading(false);
    }
  }, [
    userId,
    bbkIds,
    startDate,
    endDate,
    hasErrorFilter,
    resourceFilter,
  ]);

  // Only the selected Session needs a mapping; complete Chat metadata is unused.
  useEffect(() => {
    setChatSpecs([]);
    if (!open || !userId || !selectedSessionId) return;
    let cancelled = false;
    const load = async () => {
      try {
        const chats: ChatSummary[] = [];
        let cursor: string | null = null;
        do {
          const page = await tracingApi.getUserChatSummaries(userId, {
            session_id: selectedSessionId,
            page_size: 100,
            cursor,
          });
          if (cancelled) return;
          chats.push(...(Array.isArray(page.items) ? page.items : []));
          if (
            page.has_more &&
            (!page.next_cursor || page.next_cursor === cursor)
          ) {
            throw new Error(
              "Chat summary continuation cursor missing or repeated",
            );
          }
          cursor = page.has_more ? page.next_cursor : null;
        } while (cursor);
        setChatSpecs(chats);
      } catch (error) {
        if (!cancelled) {
          console.error("Failed to fetch chat summaries:", error);
          setChatSpecs([]);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [open, userId, selectedSessionId]);

  // 获取会话统计
  const fetchSessionStats = useCallback(async (sessionId: string) => {
    try {
      const data = await tracingApi.getSessionStats(
        sessionId,
        undefined,
        undefined,
        bbkIds,
      );
      setSessionStats(data);
    } catch (error) {
      console.error("Failed to fetch session stats:", error);
      message.error("获取会话统计失败");
    }
  }, [bbkIds]);

  // Modal 打开时加载数据
  useEffect(() => {
    if (open && userId) {
      fetchUserStats();
      setSessionsPage(1);
      setHasAutoSelectedSession(false);
      setSelectedSessionId(null);
      setSessionStats(null);
    }
  }, [open, userId, fetchUserStats]);

  // 打开弹窗或筛选条件变化时重新加载会话列表
  useEffect(() => {
    if (open && userId) {
      fetchSessions(1, DEFAULT_SESSIONS_PAGE_SIZE);
      setSessionsPage(1);
      setHasAutoSelectedSession(false);
      setSelectedSessionId(null);
      setSessionStats(null);
    }
  }, [open, userId, fetchSessions]);

  // 首次打开详情弹窗时自动选中第一条会话，便于直接查看聊天内容
  useEffect(() => {
    if (
      !open ||
      hasAutoSelectedSession ||
      selectedSessionId ||
      sessions.length === 0
    ) {
      return;
    }

    const firstSessionId = sessions[0].session_id;
    setHasAutoSelectedSession(true);
    setSelectedSessionId(firstSessionId);
    fetchSessionStats(firstSessionId);
  }, [
    open,
    hasAutoSelectedSession,
    selectedSessionId,
    sessions,
    fetchSessionStats,
  ]);

  // 关闭时重置状态
  const handleClose = () => {
    setUserStats(null);
    setSessionStats(null);
    setStatsCollapsed(false);
    setSessions([]);
    setSessionsTotal(0);
    setChatSpecs([]);
    setSessionsPage(1);
    setSessionsPageSize(DEFAULT_SESSIONS_PAGE_SIZE);
    setSessionsCollapsed(false);
    setHasAutoSelectedSession(false);
    setSelectedSessionId(null);
    setHasErrorFilter(false);
    setResourceFilter(null);
    onClose();
  };

  // 会话分页变化
  const handleSessionsPageChange = (page: number, pageSize: number) => {
    setSessionsPage(page);
    setSessionsPageSize(pageSize);
    fetchSessions(page, pageSize);
  };

  // 会话选中变化 - 点击已选中的会话则取消选中
  const handleSessionSelect = (sessionId: string) => {
    if (selectedSessionId === sessionId) {
      // 取消选中
      setSelectedSessionId(null);
      setSessionStats(null);
    } else {
      // 选中新会话
      setSelectedSessionId(sessionId);
      fetchSessionStats(sessionId);
    }
  };

  // 切换报错会话筛选
  const handleToggleErrorFilter = useCallback(() => {
    setHasErrorFilter((prev) => !prev);
  }, []);

  const handleResourceFilterChange = useCallback(
    (nextFilter: SessionResourceFilter) => {
      setResourceFilter((currentFilter) => {
        const isSameFilter =
          currentFilter?.type === nextFilter.type &&
          currentFilter.name === nextFilter.name &&
          (currentFilter.type !== "mcp_tool" ||
            nextFilter.type !== "mcp_tool" ||
            currentFilter.mcp_server === nextFilter.mcp_server);
        return isSameFilter ? null : nextFilter;
      });
    },
    [],
  );

  // 判断当前显示的是用户级还是会话级统计
  const showSessionStats =
    resourceFilter === null &&
    selectedSessionId !== null &&
    sessionStats !== null;

  return (
    <Modal
      title={
        <div className={styles.modalTitleBlock}>
          <span className={styles.modalTitleIcon}>
            <User size={18} />
          </span>
          <div className={styles.modalTitleText}>
            <div className={styles.modalTitle}>{userName || userId || '未知用户'}</div>
            <div className={styles.modalSubtitle}>{userId || "-"}</div>
          </div>
        </div>
      }
      open={open}
      onCancel={handleClose}
      width="100vw"
      footer={null}
      destroyOnClose
      className={styles.userDetailModal}
      classNames={{ body: styles.userDetailModalBody }}
      style={{ top: 0, paddingBottom: 0 }}
    >
      {userLoading ? (
        <div className={styles.loading}>
          <Spin />
        </div>
      ) : userStats ? (
        <div className={styles.modalContent}>
          {/* 顶部：统计信息 */}
          <div className={styles.topSection}>
            <UserStatsHeader
              userStats={userStats}
              sessionStats={showSessionStats ? sessionStats : null}
              collapsed={statsCollapsed}
              activeResourceFilter={resourceFilter}
              onResourceFilterChange={handleResourceFilterChange}
              onToggleCollapsed={() => setStatsCollapsed((value) => !value)}
            />
          </div>

          {/* 下方：会话列表 + 对话流 */}
          <div className={styles.bottomSection}>
            <div
              className={`${styles.leftPanel} ${
                sessionsCollapsed ? styles.leftPanelCollapsed : ""
              }`}
            >
              <SessionCardList
                sessions={sessions}
                total={sessionsTotal}
                page={sessionsPage}
                pageSize={sessionsPageSize}
                loading={sessionsLoading}
                selectedSessionId={selectedSessionId}
                collapsed={sessionsCollapsed}
                hasErrorFilter={hasErrorFilter}
                onSelect={handleSessionSelect}
                onPageChange={handleSessionsPageChange}
                onToggleCollapsed={() =>
                  setSessionsCollapsed((value) => !value)
                }
                onToggleErrorFilter={handleToggleErrorFilter}
              />
            </div>
            <div className={styles.rightPanel}>
              <ReadOnlySessionChat
                selectedSessionId={selectedSessionId}
                chatIdBySessionId={chatIdBySessionId}
                targetUserId={userId}
              />
            </div>
          </div>
        </div>
      ) : (
        <Empty />
      )}
    </Modal>
  );
}
