import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";
import type {
  WPlusSopCommandReceipt,
  WPlusSopCommandRequest,
  WPlusSopCumulativeArtifactIdentity,
  WPlusSopEntryRejectReceipt,
  WPlusSopSession,
  WPlusSopSessionEvent,
  WPlusSopStageArtifactIdentity,
} from "../types/wplusSop";

function sessionPath(sessionId: string): string {
  return `/wplus-sop/sessions/${encodeURIComponent(sessionId)}`;
}

function artifactPath(sessionId: string, artifactId: string): string {
  return `${sessionPath(sessionId)}/artifacts/${encodeURIComponent(
    artifactId,
  )}`;
}

function stageArtifactPath(
  sessionId: string,
  identity: WPlusSopStageArtifactIdentity,
): string {
  const query = new URLSearchParams({
    stage_id: identity.stageId,
    revision: String(identity.revision),
    report_no: String(identity.reportNo),
  });
  return `${sessionPath(sessionId)}/stage-report-artifacts/${encodeURIComponent(
    identity.artifactId,
  )}?${query.toString()}`;
}

function cumulativeArtifactPath(
  sessionId: string,
  identity: WPlusSopCumulativeArtifactIdentity,
): string {
  const query = new URLSearchParams({
    preview_version: String(identity.previewVersion),
  });
  return `${sessionPath(sessionId)}/cumulative-artifacts/${encodeURIComponent(
    identity.artifactId,
  )}?${query.toString()}`;
}

function downloadArtifactPath(path: string): string {
  return `${path}${path.includes("?") ? "&" : "?"}download=true`;
}

async function fetchArtifact(
  path: string,
  signal?: AbortSignal,
): Promise<Response> {
  const response = await fetch(getApiUrl(path), {
    method: "GET",
    headers: buildAuthHeaders(),
    signal,
  });
  if (!response.ok) {
    const error = new Error(`产物读取失败（${response.status}）`);
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }
  return response;
}

function parseEventBlock(block: string): WPlusSopSessionEvent | null {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data || data === "[DONE]") {
    return null;
  }
  try {
    return JSON.parse(data) as WPlusSopSessionEvent;
  } catch {
    return null;
  }
}

export interface SessionEventSubscription {
  close: () => void;
  done: Promise<void>;
}

export const wplusSopApi = {
  confirmEntry: (
    proposalId: string,
    commandRequestId: string,
    signal?: AbortSignal,
  ): Promise<WPlusSopCommandReceipt> =>
    request(
      `/wplus-sop/entry-proposals/${encodeURIComponent(proposalId)}/confirm`,
      {
        method: "POST",
        body: JSON.stringify({
          command_request_id: commandRequestId,
        }),
        signal,
      },
    ),

  rejectEntry: (
    proposalId: string,
    commandRequestId: string,
    signal?: AbortSignal,
  ): Promise<WPlusSopEntryRejectReceipt> =>
    request(
      `/wplus-sop/entry-proposals/${encodeURIComponent(proposalId)}/reject`,
      {
        method: "POST",
        body: JSON.stringify({
          command_request_id: commandRequestId,
        }),
        signal,
      },
    ),

  getSession: (
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<WPlusSopSession> =>
    request(sessionPath(sessionId), {
      signal,
    }),

  getActiveSession: (
    chatId: string,
    signal?: AbortSignal,
  ): Promise<WPlusSopSession | null> =>
    request(`/wplus-sop/chats/${encodeURIComponent(chatId)}/active-session`, {
      signal,
    }),

  sendCommand: (
    sessionId: string,
    command: WPlusSopCommandRequest,
    signal?: AbortSignal,
  ): Promise<WPlusSopCommandReceipt> =>
    request(`${sessionPath(sessionId)}/commands`, {
      method: "POST",
      body: JSON.stringify(command),
      signal,
    }),

  downloadArtifact: async (
    sessionId: string,
    artifactId: string,
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const response = await fetchArtifact(
      downloadArtifactPath(artifactPath(sessionId, artifactId)),
      signal,
    );
    return response.blob();
  },

  readArtifact: async (
    sessionId: string,
    artifactId: string,
    signal?: AbortSignal,
  ): Promise<string> => {
    const response = await fetchArtifact(
      artifactPath(sessionId, artifactId),
      signal,
    );
    return response.text();
  },

  downloadStageReportArtifact: async (
    sessionId: string,
    identity: WPlusSopStageArtifactIdentity,
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const response = await fetchArtifact(
      downloadArtifactPath(stageArtifactPath(sessionId, identity)),
      signal,
    );
    return response.blob();
  },

  readStageReportArtifact: async (
    sessionId: string,
    identity: WPlusSopStageArtifactIdentity,
    signal?: AbortSignal,
  ): Promise<string> => {
    const response = await fetchArtifact(
      stageArtifactPath(sessionId, identity),
      signal,
    );
    return response.text();
  },

  downloadCumulativeArtifact: async (
    sessionId: string,
    identity: WPlusSopCumulativeArtifactIdentity,
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const response = await fetchArtifact(
      downloadArtifactPath(cumulativeArtifactPath(sessionId, identity)),
      signal,
    );
    return response.blob();
  },

  readCumulativeArtifact: async (
    sessionId: string,
    identity: WPlusSopCumulativeArtifactIdentity,
    signal?: AbortSignal,
  ): Promise<string> => {
    const response = await fetchArtifact(
      cumulativeArtifactPath(sessionId, identity),
      signal,
    );
    return response.text();
  },

  subscribeSessionEvents(
    sessionId: string,
    afterStateVersion: number,
    onEvent: (event: WPlusSopSessionEvent) => void,
    onError?: (error: unknown) => void,
  ): SessionEventSubscription {
    const controller = new AbortController();
    const path = `${sessionPath(
      sessionId,
    )}/events?after_state_version=${afterStateVersion}`;
    const done = (async () => {
      try {
        const response = await fetch(getApiUrl(path), {
          method: "GET",
          headers: {
            ...buildAuthHeaders(),
            Accept: "text/event-stream",
          },
          signal: controller.signal,
        });
        if (!response.ok) {
          const error = new Error(`事件流连接失败（${response.status}）`);
          (error as Error & { status?: number }).status = response.status;
          throw error;
        }
        if (!response.body) {
          throw new Error("浏览器未提供事件流读取能力");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const { done: streamDone, value } = await reader.read();
          buffer += decoder.decode(value, { stream: !streamDone });
          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop() || "";
          for (const block of blocks) {
            const event = parseEventBlock(block);
            if (event) {
              onEvent(event);
            }
          }
          if (streamDone) {
            const event = parseEventBlock(buffer);
            if (event) {
              onEvent(event);
            }
            if (!controller.signal.aborted) {
              throw new Error("W+ SOP 事件流已结束");
            }
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          onError?.(error);
        }
      }
    })();

    return {
      close: () => controller.abort(),
      done,
    };
  },
};
