import { describe, expect, it } from "vitest";
import { isAnnotationForNewChat } from "./sessionMigration";
import type { HtmlAnnotationBundle } from "./types";

const bundle = (chatKey: string): HtmlAnnotationBundle => ({
  token: "annotation-token",
  chatKey,
  sourceKey: "report.html",
  fileName: "report.html",
  canonicalHtml: "<html></html>",
  sourceSha256: "a".repeat(64),
  annotations: [],
});

describe("isAnnotationForNewChat", () => {
  it("only accepts an annotation staged before a chat is persisted", () => {
    expect(isAnnotationForNewChat(bundle("new-chat"))).toBe(true);
    expect(isAnnotationForNewChat(bundle("chat-a"))).toBe(false);
    expect(isAnnotationForNewChat(null)).toBe(false);
  });
});
