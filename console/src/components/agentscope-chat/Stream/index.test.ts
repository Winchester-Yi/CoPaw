import { describe, expect, it, vi } from "vitest";
import Stream from "./index";

describe("SSE reader lifetime", () => {
  it("cancels the underlying response when the consumer reconnects early", async () => {
    const cancel = vi.fn();
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode("event: stream.recover\ndata: {}\n\n"),
        );
      },
      cancel,
    });
    const stream = Stream({ readableStream: source });
    for await (const chunk of stream) {
      expect(chunk.event.trim()).toBe("stream.recover");
      break;
    }
    await vi.waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
  });
});
