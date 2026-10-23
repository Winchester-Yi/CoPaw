import { describe, expect, it } from "vitest";
import { readStopAppendMetadata, stopAppendBubbleId } from "./stopAppend";

describe("Stop appended message identity", () => {
  it.each([
    { stop_append: { message_id: "persisted-append", order: 1 } },
    { metadata: { stop_append: { message_id: "persisted-append", order: 1 } } },
  ])(
    "reads direct and history metadata without using transport IDs",
    (metadata) => {
      const marker = readStopAppendMetadata({ id: "runtime-id", metadata });
      expect(marker).toMatchObject({
        message_id: "persisted-append",
        order: 1,
      });
      expect(stopAppendBubbleId(marker!)).toBe("stop-append-persisted-append");
    },
  );

  it.each([
    undefined,
    null,
    {},
    { stop_append: {} },
    { stop_append: { message_id: " " } },
  ])("ignores invalid markers", (metadata) => {
    expect(readStopAppendMetadata({ metadata })).toBeNull();
  });
});
