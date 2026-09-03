import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import type { Message } from "@/api/types";
import {
  buildShareTurns,
  getDefaultSelectedTurnIds,
  getShareMessageId,
  toggleTurnSelection,
  type ShareTurn,
} from "./components/ChatActionGroup/shareSelection";

interface ChatShareSelectionContextValue {
  active: boolean;
  turns: ShareTurn[];
  selectedTurnIds: string[];
  selectableTurnIds: string[];
  turnByMessageId: Record<string, string>;
  shareUrl: string | null;
  open: (messages: Message[], statuses: Record<string, string>) => void;
  close: () => void;
  toggleTurn: (turnId: string) => void;
  selectAll: (checked: boolean) => void;
  setShareUrl: (url: string) => void;
}

const DEFAULT_CONTEXT: ChatShareSelectionContextValue = {
  active: false,
  turns: [],
  selectedTurnIds: [],
  selectableTurnIds: [],
  turnByMessageId: {},
  shareUrl: null,
  open: () => {},
  close: () => {},
  toggleTurn: () => {},
  selectAll: () => {},
  setShareUrl: () => {},
};

export const ChatShareSelectionContext = createContext(DEFAULT_CONTEXT);

export function ChatShareSelectionProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [active, setActive] = useState(false);
  const [turns, setTurns] = useState<ShareTurn[]>([]);
  const [selectedTurnIds, setSelectedTurnIds] = useState<string[]>([]);
  const [turnByMessageId, setTurnByMessageId] = useState<
    Record<string, string>
  >({});
  const [shareUrl, setShareUrlState] = useState<string | null>(null);

  const open = useCallback(
    (messages: Message[], statuses: Record<string, string>) => {
      const records = messages as Array<Record<string, unknown>>;
      const nextTurns = buildShareTurns(records, statuses);
      const nextIndex: Record<string, string> = {};
      for (const turn of nextTurns) {
        for (const messageId of turn.messageIds) nextIndex[messageId] = turn.id;
      }
      for (const message of records) {
        const rawId = typeof message.id === "string" ? message.id : "";
        const shareId = getShareMessageId(message);
        if (rawId && shareId && rawId !== shareId) nextIndex[rawId] = shareId;
      }
      setTurns(nextTurns);
      setSelectedTurnIds(getDefaultSelectedTurnIds(nextTurns));
      setTurnByMessageId(nextIndex);
      setShareUrlState(null);
      setActive(true);
    },
    [],
  );

  const close = useCallback(() => {
    setActive(false);
    setShareUrlState(null);
  }, []);

  const toggleTurn = useCallback(
    (turnId: string) => {
      setSelectedTurnIds((current) => {
        const next = toggleTurnSelection(
          current,
          turnId,
          turns.filter((turn) => turn.selectable).map((turn) => turn.id),
        );
        setShareUrlState(null);
        return next;
      });
    },
    [turns],
  );

  const selectAll = useCallback(
    (checked: boolean) => {
      setSelectedTurnIds(checked ? getDefaultSelectedTurnIds(turns) : []);
      setShareUrlState(null);
    },
    [turns],
  );

  const value = useMemo<ChatShareSelectionContextValue>(
    () => ({
      active,
      turns,
      selectedTurnIds,
      selectableTurnIds: turns
        .filter((turn) => turn.selectable)
        .map((turn) => turn.id),
      turnByMessageId,
      shareUrl,
      open,
      close,
      toggleTurn,
      selectAll,
      setShareUrl: setShareUrlState,
    }),
    [
      active,
      close,
      open,
      selectAll,
      selectedTurnIds,
      shareUrl,
      toggleTurn,
      turnByMessageId,
      turns,
    ],
  );

  return (
    <div
      className={active ? "swe-chat-share-active" : undefined}
      style={{ height: "100%", width: "100%" }}
    >
      <ChatShareSelectionContext.Provider value={value}>
        {children}
      </ChatShareSelectionContext.Provider>
    </div>
  );
}

export function useChatShareSelection() {
  return useContext(ChatShareSelectionContext);
}
