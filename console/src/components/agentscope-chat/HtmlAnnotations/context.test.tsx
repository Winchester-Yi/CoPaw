import { act, render, renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { describe, expect, it } from "vitest";
import { HtmlAnnotationProvider, useHtmlAnnotations } from "./context";
import type { HtmlAnnotationBundle } from "./types";

const bundle = (chatKey: string, digest: string): HtmlAnnotationBundle => ({
  token: `${chatKey}-${digest}`,
  chatKey,
  sourceKey: "report.html",
  fileName: "report.html",
  canonicalHtml: "<html></html>",
  sourceSha256: digest,
  annotations: [
    {
      id: "ann-1",
      comment: "修改标题",
      target: {
        runtime_generated: false,
        stable_attributes: { id: "title" },
      },
    },
  ],
});

describe("HtmlAnnotationProvider", () => {
  it("isolates pending bundles by logical chat and consumes only matching tokens", () => {
    const wrapper = ({ children }: PropsWithChildren) => (
      <HtmlAnnotationProvider activeChatKey="chat-a" composerAvailable>
        {children}
      </HtmlAnnotationProvider>
    );
    const { result } = renderHook(() => useHtmlAnnotations(), { wrapper });

    act(() => result.current.stageBundle(bundle("chat-a", "a".repeat(64))));
    expect(result.current.pendingBundle?.chatKey).toBe("chat-a");
    act(() => result.current.consumeBundle("wrong-token"));
    expect(result.current.pendingBundle).not.toBeNull();
    act(() => result.current.consumeBundle(`chat-a-${"a".repeat(64)}`));
    expect(result.current.pendingBundle).toBeNull();
  });

  it("rejects a bundle that belongs to another active chat", () => {
    const wrapper = ({ children }: PropsWithChildren) => (
      <HtmlAnnotationProvider activeChatKey="chat-b" composerAvailable>
        {children}
      </HtmlAnnotationProvider>
    );
    const { result } = renderHook(() => useHtmlAnnotations(), { wrapper });

    act(() => result.current.stageBundle(bundle("chat-a", "a".repeat(64))));

    expect(result.current.pendingBundle).toBeNull();
  });

  it("migrates a newly created chat bundle and consumes it by token", () => {
    let context!: ReturnType<typeof useHtmlAnnotations>;
    const Consumer = () => {
      context = useHtmlAnnotations();
      return null;
    };
    const view = render(
      <HtmlAnnotationProvider activeChatKey="new-chat" composerAvailable>
        <Consumer />
      </HtmlAnnotationProvider>,
    );
    const pending = bundle("new-chat", "a".repeat(64));

    act(() => context.stageBundle(pending));
    act(() =>
      context.migrateBundle("new-chat", "chat-real", pending.token),
    );
    view.rerender(
      <HtmlAnnotationProvider activeChatKey="chat-real" composerAvailable>
        <Consumer />
      </HtmlAnnotationProvider>,
    );

    expect(context.pendingBundle?.chatKey).toBe("chat-real");

    view.rerender(
      <HtmlAnnotationProvider activeChatKey="another-chat" composerAvailable>
        <Consumer />
      </HtmlAnnotationProvider>,
    );
    act(() => context.consumeBundle(pending.token));
    view.rerender(
      <HtmlAnnotationProvider activeChatKey="chat-real" composerAvailable>
        <Consumer />
      </HtmlAnnotationProvider>,
    );

    expect(context.pendingBundle).toBeNull();
  });
});
