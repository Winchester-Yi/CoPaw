import type { ChatRequestOwner } from "./requestOwnership";
import { isAbortLikeError } from "./abortReasons";

type Frame = { id?: string; event?: string; data?: string };
interface StreamOptions {
  response: Response;
  owner: ChatRequestOwner;
  decode: (response: Response) => AsyncIterable<Frame>;
  reconnect?: (after?: string) => Promise<Response>;
  isActive: () => boolean;
  isReplaced?: () => boolean;
}

export class StreamRecoveryError extends Error {
  constructor() {
    super("实时连接暂不可用，后台任务可能仍在运行。请刷新会话查看最新状态。");
    this.name = "StreamRecoveryError";
  }
}

function cursor(value: string) {
  const match = /^(turn-[^:\s]+):([0-9]+)$/.exec(value.trim());
  const sequence = Number(match?.[2]);
  if (!match || !Number.isSafeInteger(sequence) || sequence < 1) {
    throw new StreamRecoveryError();
  }
  return { turn: match[1], sequence };
}

export async function* readChatStream(
  options: StreamOptions,
): AsyncGenerator<Frame> {
  const {
    owner,
    decode,
    reconnect,
    isActive,
    isReplaced = () => false,
  } = options;
  let response = options.response;
  const negotiated =
    response.headers?.get("X-Swe-Stream-Protocol") === "cursor-v1";
  let attempts = 0;
  while (!isReplaced()) {
    try {
      for await (const frame of decode(response)) {
        if (isReplaced()) return;
        const event = frame.event?.trim();
        if (event === "stream.end") return;
        if (event === "stream.unavailable") throw new StreamRecoveryError();
        if (event === "stream.recover") {
          break;
        }
        if (!frame.data) continue;
        if (event === "chat.snapshot") {
          yield { ...frame, event };
          return;
        }
        if (negotiated) {
          if (
            !frame.id ||
            response.headers.get("X-Swe-Stream-Protocol") !== "cursor-v1"
          )
            throw new StreamRecoveryError();
          const next = cursor(frame.id);
          const previous = owner.streamCursor
            ? cursor(owner.streamCursor)
            : null;
          if (previous && next.turn !== previous.turn)
            throw new StreamRecoveryError();
          if (previous && next.sequence <= previous.sequence) continue;
          if (next.sequence !== (previous?.sequence ?? 0) + 1)
            throw new StreamRecoveryError();
        }
        yield { ...frame, event };
        // Resuming the generator means its caller successfully applied the frame.
        if (negotiated) owner.streamCursor = frame.id!.trim();
      }
    } catch (error) {
      if (
        error instanceof StreamRecoveryError ||
        isAbortLikeError(error) ||
        !negotiated
      )
        throw error;
    }
    if (!negotiated) return;
    if (!isActive() || isReplaced()) return;
    if (!reconnect) throw new StreamRecoveryError();
    // EOF without stream.end is an interrupted transport, never business success.
    let recovered = false;
    while (attempts < 3 && isActive() && !isReplaced()) {
      attempts += 1;
      await new Promise((resolve) =>
        setTimeout(resolve, 250 * 2 ** (attempts - 1)),
      );
      if (!isActive() || isReplaced()) return;
      try {
        response = await reconnect(owner.streamCursor);
      } catch (error) {
        if (isAbortLikeError(error)) throw error;
        continue;
      }
      if (response.status === 503) {
        await response.body?.cancel().catch(() => undefined);
        continue;
      }
      if (!response.ok || !response.body) throw new StreamRecoveryError();
      recovered = true;
      break;
    }
    if (!isActive() || isReplaced()) return;
    if (!recovered) throw new StreamRecoveryError();
  }
}
