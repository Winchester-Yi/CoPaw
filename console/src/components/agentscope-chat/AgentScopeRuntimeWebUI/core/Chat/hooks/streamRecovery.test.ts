import { describe, expect, it, vi } from "vitest";
import { readChatStream, StreamRecoveryError } from "./streamRecovery";
import type { ChatRequestOwner } from "./requestOwnership";

const owner = (): ChatRequestOwner => ({
  requestId: "request-a",
  kind: "submit",
  sessionId: "session-a",
  logicalSessionId: "session-a",
  chatId: "chat-a",
});
const response = () =>
  new Response("", { headers: { "X-Swe-Stream-Protocol": "cursor-v1" } });

describe("cursor stream recovery", () => {
  it("retries transient reconnect failures within a bounded budget", async () => {
    const first = response();
    const second = response();
    const decode = async function* (input: Response) {
      if (input === first) yield { id: "turn-a:1", data: "one" };
      else {
        yield { id: "turn-a:2", data: "two" };
        yield { event: "stream.end", data: "{}" };
      }
    };
    const reconnect = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("network"))
      .mockResolvedValue(second);
    const received = [];
    for await (const frame of readChatStream({
      response: first,
      owner: owner(),
      decode,
      reconnect,
      isActive: () => true,
    }))
      received.push(frame.data);
    expect(received).toEqual(["one", "two"]);
    expect(reconnect).toHaveBeenCalledTimes(2);
  });

  it("propagates deliberate aborts without reconnecting", async () => {
    const abort = new DOMException("aborted", "AbortError");
    const decode = async function* () {
      throw abort;
      yield { data: "unreachable" };
    };
    const reconnect = vi.fn(async () => response());
    const iterator = readChatStream({
      response: response(),
      owner: owner(),
      decode,
      reconnect,
      isActive: () => true,
    });
    await expect(iterator.next()).rejects.toBe(abort);
    expect(reconnect).not.toHaveBeenCalled();
  });
  it("resumes after the last applied event and ignores duplicates", async () => {
    const first = response();
    const second = response();
    const requestOwner = owner();
    const decode = async function* (input: Response) {
      yield { id: "turn-a:1", data: "one" };
      if (input === second) {
        yield { id: "turn-a:2", data: "two" };
        yield { event: "stream.end", data: "{}" };
      }
    };
    const reconnect = vi.fn(async () => second);
    const iterator = readChatStream({
      response: first,
      owner: requestOwner,
      decode,
      reconnect,
      isActive: () => true,
    });
    expect((await iterator.next()).value?.data).toBe("one");
    expect(requestOwner.streamCursor).toBeUndefined();
    expect((await iterator.next()).value?.data).toBe("two");
    expect(reconnect).toHaveBeenCalledWith("turn-a:1");
    expect(requestOwner.streamCursor).toBe("turn-a:1");
    expect((await iterator.next()).done).toBe(true);
    expect(requestOwner.streamCursor).toBe("turn-a:2");
  });

  it("rejects gaps and other turn identities without applying them", async () => {
    for (const id of ["turn-a:3", "turn-other:2"]) {
      const requestOwner = { ...owner(), streamCursor: "turn-a:1" };
      const decode = async function* () {
        yield { id, data: "bad" };
      };
      const iterator = readChatStream({
        response: response(),
        owner: requestOwner,
        decode,
        isActive: () => true,
      });
      await expect(iterator.next()).rejects.toBeInstanceOf(StreamRecoveryError);
      expect(requestOwner.streamCursor).toBe("turn-a:1");
    }
  });

  it("does not reconnect an obsolete owner or unavailable replay", async () => {
    const reconnect = vi.fn(async () => response());
    const decode = async function* () {
      yield { event: "stream.unavailable", data: "{}" };
    };
    const iterator = readChatStream({
      response: response(),
      owner: owner(),
      decode,
      reconnect,
      isActive: () => true,
    });
    await expect(iterator.next()).rejects.toBeInstanceOf(StreamRecoveryError);
    expect(reconnect).not.toHaveBeenCalled();
    const obsolete = readChatStream({
      response: response(),
      owner: owner(),
      decode,
      reconnect,
      isActive: () => false,
      isReplaced: () => true,
    });
    expect((await obsolete.next()).done).toBe(true);
  });

  it("keeps consuming a completed stream until it is replaced", async () => {
    const decode = async function* () {
      yield {
        id: "turn-a:1",
        data: JSON.stringify({
          object: "session_title_updated",
          session_id: "session-a",
          session_title: "Generated title",
        }),
      };
    };
    const iterator = readChatStream({
      response: response(),
      owner: owner(),
      decode,
      isActive: () => false,
      isReplaced: () => false,
    });

    expect((await iterator.next()).value?.data).toContain("Generated title");
  });

  it("keeps legacy EOF behavior when the protocol is not negotiated", async () => {
    const decode = async function* () {
      yield { data: "legacy" };
    };
    const reconnect = vi.fn(async () => response());
    const received = [];
    for await (const event of readChatStream({
      response: new Response(""),
      owner: owner(),
      decode,
      reconnect,
      isActive: () => true,
    }))
      received.push(event.data);
    expect(received).toEqual(["legacy"]);
    expect(reconnect).not.toHaveBeenCalled();
  });
});
