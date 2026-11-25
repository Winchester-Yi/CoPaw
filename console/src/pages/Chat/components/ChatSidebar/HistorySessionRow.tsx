import React from "react";
import { Button, Dropdown } from "antd";
import { Ellipsis, Pencil, Trash2 } from "lucide-react";
import ChatSessionItem from "../ChatSessionItem";
import type { HistorySession } from "./historySessions";
import { getHistorySessionTargetId } from "./historySessions";
import { formatListTime } from "../../listTimeFormat";

export interface HistorySessionRowProps {
  name?: string;
  session: HistorySession & {
    realId?: string;
  };
  active: boolean;
  onSessionClick: (sessionId: string) => void;
  onSessionRename: (
    backendId: string,
    name: string,
    trigger: HTMLButtonElement | null,
  ) => void;
  onSessionDelete: (
    sessionId: string,
    backendId: string | null,
    sessionName: string,
  ) => void;
}

function resolveBackendChatId(
  session: HistorySession & {
    realId?: string;
  },
): string | null {
  if (session.realId) {
    return session.realId;
  }
  if (!session.id || /^\d+$/.test(session.id)) {
    return null;
  }
  return session.id;
}

function HistorySessionRowInner(props: HistorySessionRowProps) {
  const { session, active, onSessionClick, onSessionDelete, onSessionRename } =
    props;
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const [menuOpen, setMenuOpen] = React.useState(false);
  const targetId = getHistorySessionTargetId(session);
  const backendId = resolveBackendChatId(session);
  const sessionName = session.name || "新会话";

  const handleClick = React.useCallback(() => {
    onSessionClick(targetId);
  }, [onSessionClick, targetId]);

  return (
    <ChatSessionItem
      name={sessionName}
      time={formatListTime(session.createdAt)}
      active={active}
      onClick={handleClick}
      titleAction={
        <Dropdown
          autoFocus
          trigger={["click"]}
          placement="bottomRight"
          open={menuOpen}
          onOpenChange={setMenuOpen}
          menu={{
            items: [
              {
                key: "rename",
                label: "编辑标题",
                icon: <Pencil size={15} />,
                disabled: !backendId,
              },
              {
                key: "delete",
                label: "删除",
                icon: <Trash2 size={15} />,
                danger: true,
              },
            ],
            onClick: ({ key, domEvent }) => {
              domEvent.stopPropagation();
              setMenuOpen(false);
              if (key === "rename" && backendId) {
                onSessionRename(backendId, sessionName, triggerRef.current);
              } else if (key === "delete") {
                onSessionDelete(targetId, backendId, sessionName);
              }
            },
          }}
        >
          <Button
            ref={triggerRef}
            className="chat-history-more"
            aria-label={`会话“${sessionName}”的更多操作`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            size="small"
            type="text"
            icon={<Ellipsis size={18} />}
            onClick={(event) => event.stopPropagation()}
          />
        </Dropdown>
      }
      showTimeline={false}
      showChannel={false}
    />
  );
}

function areEqual(
  prevProps: HistorySessionRowProps,
  nextProps: HistorySessionRowProps,
): boolean {
  return (
    prevProps.active === nextProps.active &&
    prevProps.onSessionClick === nextProps.onSessionClick &&
    prevProps.onSessionDelete === nextProps.onSessionDelete &&
    prevProps.onSessionRename === nextProps.onSessionRename &&
    prevProps.session.id === nextProps.session.id &&
    prevProps.session.realId === nextProps.session.realId &&
    prevProps.session.name === nextProps.session.name &&
    prevProps.session.createdAt === nextProps.session.createdAt
  );
}

export const HistorySessionRow = React.memo(HistorySessionRowInner, areEqual);
