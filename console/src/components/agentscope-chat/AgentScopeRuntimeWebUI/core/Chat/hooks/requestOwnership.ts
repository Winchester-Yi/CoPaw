export interface ChatRequestOwner {
  requestId: string;
  kind: "submit" | "reconnect" | "regenerate" | "approval";
  sessionId: string;
  logicalSessionId: string;
  chatId: string | null;
  msgid?: string | null;
  streamCursor?: string;
}

export function createChatRequestOwner(
  input: Omit<ChatRequestOwner, "requestId">,
): ChatRequestOwner {
  return {
    requestId: `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
    ...input,
  };
}

export function isActiveChatRequestOwner(
  activeOwner: ChatRequestOwner | undefined,
  candidateOwner: ChatRequestOwner,
): boolean {
  return activeOwner?.requestId === candidateOwner.requestId;
}
