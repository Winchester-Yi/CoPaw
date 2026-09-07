export function isShareableTurn(
  turnId: string,
  statuses: Record<string, string>,
): boolean {
  return Boolean(turnId) && statuses[turnId] === "completed";
}

export interface ShareTurn {
  id: string;
  messageIds: string[];
  selectable: boolean;
}

export type ShareSelectionState = "empty" | "none" | "partial" | "all";

export function getShareSelectionState(
  selectableCount: number,
  selectedCount: number,
): ShareSelectionState {
  if (selectableCount === 0) return "empty";
  if (selectedCount === 0) return "none";
  if (selectedCount === selectableCount) return "all";
  return "partial";
}

export function getShareMessageId(message: Record<string, unknown>): string {
  const metadata = message.metadata;
  if (metadata && typeof metadata === "object") {
    const originalId = (metadata as Record<string, unknown>).original_id;
    if (typeof originalId === "string" && originalId) return originalId;
    const nested = (metadata as Record<string, unknown>).metadata;
    if (nested && typeof nested === "object") {
      const originalId = (nested as Record<string, unknown>).original_id;
      if (typeof originalId === "string" && originalId) return originalId;
    }
  }
  return typeof message.id === "string" ? message.id : "";
}

export function buildShareTurns(
  messages: Array<Record<string, unknown>>,
  statuses: Record<string, string>,
): ShareTurn[] {
  const turns: Array<ShareTurn & { hasAssistantOutput: boolean }> = [];
  let current: (ShareTurn & { hasAssistantOutput: boolean }) | null = null;
  for (const message of messages) {
    const id = getShareMessageId(message);
    if (message.role === "user") {
      if (current) turns.push(current);
      const rawId = typeof message.id === "string" ? message.id : "";
      current = id
        ? {
            id,
            messageIds: rawId && rawId !== id ? [id, rawId] : [id],
            selectable: false,
            hasAssistantOutput: false,
          }
        : null;
    } else if (current && id) {
      current.messageIds.push(id);
      const rawId = typeof message.id === "string" ? message.id : "";
      if (rawId && rawId !== id) current.messageIds.push(rawId);
      if (message.role === "assistant") current.hasAssistantOutput = true;
    }
  }
  if (current) turns.push(current);
  return turns.map(({ hasAssistantOutput, ...turn }) => ({
    ...turn,
    selectable: isShareableTurn(turn.id, statuses) && hasAssistantOutput,
  }));
}

export function getDefaultSelectedTurnIds(turns: ShareTurn[]): string[] {
  return turns.filter((turn) => turn.selectable).map((turn) => turn.id);
}

export function toggleTurnSelection(
  selected: string[],
  turnId: string,
  selectableTurnIds: string[],
): string[] {
  if (!selectableTurnIds.includes(turnId)) return selected;
  return selected.includes(turnId)
    ? selected.filter((id) => id !== turnId)
    : [...selected, turnId];
}
