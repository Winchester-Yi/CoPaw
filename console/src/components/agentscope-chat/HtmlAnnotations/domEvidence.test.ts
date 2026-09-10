import { describe, expect, it } from "vitest";
import {
  buildAnnotationTarget,
  buildStructuralSelector,
  computeHtmlSha256,
} from "./domEvidence";

describe("HTML annotation DOM evidence", () => {
  it("binds the draft to the canonical pre-execution HTML digest", async () => {
    await expect(computeHtmlSha256("<html>one</html>")).resolves.toMatch(
      /^[0-9a-f]{64}$/,
    );
    await expect(computeHtmlSha256("<html>one</html>")).resolves.not.toBe(
      await computeHtmlSha256("<html>two</html>"),
    );
  });

  it("prefers a unique stable id over a structural path", () => {
    document.body.innerHTML =
      '<main><section id="sales"><span>总额</span></section></main>';
    const element = document.querySelector("section")!;

    expect(buildStructuralSelector(element, document)).toBe("#sales");
    expect(
      buildAnnotationTarget(element, {
        canonicalHtml: document.documentElement.outerHTML,
        liveDocument: document,
      }).stable_attributes,
    ).toMatchObject({ id: "sales" });
  });

  it("escapes a unique id that starts with a digit", () => {
    document.body.innerHTML = '<main><section id="123">总额</section></main>';
    const element = document.querySelector("section")!;

    const selector = buildStructuralSelector(element, document);

    expect(() => document.querySelector(selector)).not.toThrow();
    expect(document.querySelector(selector)).toBe(element);
  });

  it("captures bounded excerpts, quotes, styles and runtime classification", () => {
    document.body.innerHTML =
      '<main><div class="card"><span data-status="active">' +
      "运行中".repeat(400) +
      "</span></div></main>";
    const element = document.querySelector("span")!;
    const target = buildAnnotationTarget(element, {
      canonicalHtml: "<html><body><main></main></body></html>",
      liveDocument: document,
    });

    expect(target.runtime_generated).toBe(true);
    expect(target.rendered_html!.length).toBeLessThanOrEqual(4096);
    expect(target.text_quote!.exact.length).toBeLessThanOrEqual(1024);
    expect(Object.keys(target.computed_style || {})).toEqual(
      expect.arrayContaining(["display", "position"]),
    );
    expect(target.stable_attributes).toMatchObject({
      "data-status": "active",
    });
  });

  it("does not mistake a runtime-inserted sibling for the canonical node at the same selector", () => {
    const canonicalHtml =
      "<html><body><main><button>原始按钮</button></main></body></html>";
    document.body.innerHTML =
      "<main><button>运行时按钮</button><button>原始按钮</button></main>";
    const runtimeButton = document.querySelector("button")!;

    const target = buildAnnotationTarget(runtimeButton, {
      canonicalHtml,
      liveDocument: document,
    });

    expect(target.selector).toContain("button:nth-of-type(1)");
    expect(target.runtime_generated).toBe(true);
  });

  it("keeps every target field within the backend annotation contract", () => {
    const dataAttributes = Array.from(
      { length: 24 },
      (_, index) => `data-field-${index}="${"x".repeat(600)}"`,
    ).join(" ");
    document.body.innerHTML = `<main>${"<div>".repeat(
      500,
    )}<button ${dataAttributes} role="${"r".repeat(
      200,
    )}">修改</button>${"</div>".repeat(500)}</main>`;
    const element = document.querySelector("button")!;

    const target = buildAnnotationTarget(element, {
      canonicalHtml: document.documentElement.outerHTML,
      liveDocument: document,
    });

    expect(target.selector!.length).toBeLessThanOrEqual(2048);
    expect(target.role!.length).toBeLessThanOrEqual(128);
    expect(
      Object.entries(target.stable_attributes || {}).every(
        ([name, value]) => name.length <= 128 && value.length <= 512,
      ),
    ).toBe(true);
    expect(Object.keys(target.stable_attributes || {})).toHaveLength(16);
  });
});
