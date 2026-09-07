import React from "react";
import { act, render } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import useChatMessageHandler from "./useChatMessageHandler";
import type { CurrentQARef } from "./currentQARef";

const mocks = vi.hoisted(() => ({
  updateMessage: vi.fn(),
  getMessages: vi.fn(() => []),
  removeMessage: vi.fn(),
  flushSync: vi.fn((callback: () => void) => callback()),
}));

vi.mock("react-dom", () => ({
  default: {
    flushSync: mocks.flushSync,
  },
}));

vi.mock("../../Context/ChatAnywhereMessagesContext", () => ({
  useChatAnywhereMessages: () => ({
    updateMessage: mocks.updateMessage,
    getMessages: mocks.getMessages,
    removeMessage: mocks.removeMessage,
  }),
}));

vi.mock("@/components/agentscope-chat", () => ({
  uuid: () => "response-id",
}));

let hookApi: ReturnType<typeof useChatMessageHandler>;

function Harness(props: { currentQARef: CurrentQARef }) {
  hookApi = useChatMessageHandler({ currentQARef: props.currentQARef });
  return null;
}

describe("useChatMessageHandler", () => {
  beforeEach(() => {
    mocks.updateMessage.mockClear();
    mocks.getMessages.mockClear();
    mocks.removeMessage.mockClear();
    mocks.flushSync.mockClear();
  });

  it("stores a timestamp and synchronously mounts a live response", () => {
    const timestamp = Date.parse("2026-04-17T16:05:00+08:00");
    const dateNow = vi.spyOn(Date, "now").mockReturnValue(timestamp);
    const currentQARef = {
      current: {},
    } as CurrentQARef;

    render(<Harness currentQARef={currentQARef} />);

    let response;
    act(() => {
      response = hookApi.createResponseMessage();
    });

    expect(response).toMatchObject({
      role: "assistant",
      msgStatus: "generating",
      liveHeaderTimestamp: timestamp,
    });
    expect(currentQARef.current.response?.liveHeaderTimestamp).toBe(timestamp);
    expect(mocks.updateMessage).toHaveBeenCalledWith(
      currentQARef.current.response,
    );
    expect(mocks.flushSync).toHaveBeenCalledTimes(1);

    dateNow.mockRestore();
  });
});
