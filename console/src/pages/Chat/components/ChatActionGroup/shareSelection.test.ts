import { describe, expect, it } from "vitest";
import {
  buildShareTurns,
  getDefaultSelectedTurnIds,
  getShareSelectionState,
  toggleTurnSelection,
} from "./shareSelection";

const message = (id: string, role: string) => ({
  id,
  role,
  content: [{ type: "text", text: id }],
});

describe("share selection turn rules", () => {
  it("shares Stop append messages with their existing user turn", () => {
    expect(
      buildShareTurns(
        [
          message("q1", "user"),
          message("body", "assistant"),
          {
            ...message("append", "assistant"),
            metadata: {
              stop_append: { message_id: "append", turn_id: "turn-1" },
            },
          },
        ],
        { q1: "completed" },
      ),
    ).toEqual([
      { id: "q1", messageIds: ["q1", "body", "append"], selectable: true },
    ]);
  });
  it("derives all, partial, and empty toolbar selection states", () => {
    expect(getShareSelectionState(0, 0)).toBe("empty");
    expect(getShareSelectionState(3, 0)).toBe("none");
    expect(getShareSelectionState(3, 1)).toBe("partial");
    expect(getShareSelectionState(3, 3)).toBe("all");
  });

  it("groups each user input with every following output before the next input", () => {
    const turns = buildShareTurns(
      [
        message("q1", "user"),
        message("a1", "assistant"),
        message("tool1", "tool"),
        message("q2", "user"),
        message("a2", "assistant"),
      ],
      { q1: "completed", q2: "completed" },
    );

    expect(turns).toEqual([
      { id: "q1", messageIds: ["q1", "a1", "tool1"], selectable: true },
      { id: "q2", messageIds: ["q2", "a2"], selectable: true },
    ]);
  });

  it("does not select incomplete or output-less turns", () => {
    const turns = buildShareTurns(
      [
        message("q1", "user"),
        message("a1", "assistant"),
        message("q2", "user"),
        message("q3", "user"),
      ],
      { q1: "running", q2: "completed", q3: "completed" },
    );

    expect(turns.map(({ id, selectable }) => ({ id, selectable }))).toEqual([
      { id: "q1", selectable: false },
      { id: "q2", selectable: false },
      { id: "q3", selectable: false },
    ]);
    expect(getDefaultSelectedTurnIds(turns)).toEqual([]);
  });

  it("toggles a complete turn atomically", () => {
    const selectable = ["q1", "q2"];
    expect(toggleTurnSelection(["q1"], "q1", selectable)).toEqual([]);
    expect(toggleTurnSelection([], "q2", selectable)).toEqual(["q2"]);
    expect(toggleTurnSelection(["q1"], "q3", selectable)).toEqual(["q1"]);
  });
});
