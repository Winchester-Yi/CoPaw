export interface ChatWorkspaceFile {
  fileName: string;
  fileUrl: string;
  enableClickTracking: boolean;
}

export const CHAT_WORKSPACE_FILE_EVENT = "copaw:chat-workspace-file";

export type ChatWorkspaceFileEventDetail = ChatWorkspaceFile & {
  action: "register" | "open";
};

export function emitChatWorkspaceFile(
  detail: ChatWorkspaceFileEventDetail,
): void {
  window.dispatchEvent(
    new CustomEvent<ChatWorkspaceFileEventDetail>(CHAT_WORKSPACE_FILE_EVENT, {
      detail,
    }),
  );
}
