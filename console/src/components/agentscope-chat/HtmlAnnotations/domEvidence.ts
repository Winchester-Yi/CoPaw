import {
  HTML_ANNOTATION_MAX_ATTRIBUTES,
  HTML_ANNOTATION_MAX_ATTRIBUTE_NAME_CHARS,
  HTML_ANNOTATION_MAX_ATTRIBUTE_VALUE_CHARS,
  HTML_ANNOTATION_MAX_EXCERPT_CHARS,
  HTML_ANNOTATION_MAX_ROLE_CHARS,
  HTML_ANNOTATION_MAX_SELECTOR_CHARS,
  HTML_ANNOTATION_MAX_TEXT_QUOTE_CHARS,
  type HtmlAnnotationTarget,
} from "./types";

const STABLE_ATTRIBUTE_NAMES = ["name", "aria-label"] as const;
const COMPUTED_STYLE_NAMES = [
  "display",
  "position",
  "width",
  "height",
  "color",
  "background-color",
  "font-size",
  "font-weight",
  "text-align",
  "grid-template-columns",
  "flex-direction",
  "gap",
] as const;

const bounded = (value: string, max: number) => value.slice(0, max);

function escapeIdentifier(value: string, liveDocument: Document): string {
  const nativeEscape = liveDocument.defaultView?.CSS?.escape;
  if (nativeEscape) return nativeEscape(value);
  const characters = Array.from(value);
  return characters
    .map((character, index) => {
      const code = character.codePointAt(0) || 0;
      if (code === 0) return "�";
      if (
        (code >= 1 && code <= 31) ||
        code === 127 ||
        (index === 0 && code >= 48 && code <= 57) ||
        (index === 1 && code >= 48 && code <= 57 && characters[0] === "-")
      ) {
        return `\\${code.toString(16)} `;
      }
      if (index === 0 && character === "-" && characters.length === 1) {
        return "\\-";
      }
      if (
        code >= 128 ||
        character === "-" ||
        character === "_" ||
        (code >= 48 && code <= 57) ||
        (code >= 65 && code <= 90) ||
        (code >= 97 && code <= 122)
      ) {
        return character;
      }
      return `\\${character}`;
    })
    .join("");
}

function uniqueIdSelector(id: string, liveDocument: Document): string | null {
  if (id.length > HTML_ANNOTATION_MAX_ATTRIBUTE_VALUE_CHARS) return null;
  const selector = `#${escapeIdentifier(id, liveDocument)}`;
  if (selector.length > HTML_ANNOTATION_MAX_SELECTOR_CHARS) return null;
  try {
    return liveDocument.querySelectorAll(selector).length === 1
      ? selector
      : null;
  } catch {
    return null;
  }
}

export async function computeHtmlSha256(html: string): Promise<string> {
  const bytes = new TextEncoder().encode(html);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export function buildStructuralSelector(
  element: Element,
  liveDocument: Document,
): string {
  const id = element.getAttribute("id");
  const rootIdSelector = id ? uniqueIdSelector(id, liveDocument) : null;
  if (rootIdSelector) return rootIdSelector;
  const parts: string[] = [];
  let current: Element | null = element;
  while (current && current !== liveDocument.documentElement) {
    let part = bounded(current.tagName.toLowerCase(), 64) || "*";
    const currentId = current.getAttribute("id");
    const currentIdSelector = currentId
      ? uniqueIdSelector(currentId, liveDocument)
      : null;
    if (currentIdSelector) {
      parts.unshift(currentIdSelector);
      break;
    }
    const parent = current.parentElement;
    if (parent) {
      const sameTag = Array.from(parent.children).filter(
        (child) => child.tagName === current!.tagName,
      );
      if (sameTag.length > 1) {
        part += `:nth-of-type(${sameTag.indexOf(current) + 1})`;
      }
    }
    parts.unshift(part);
    current = parent;
  }
  while (
    parts.length > 1 &&
    parts.join(" > ").length > HTML_ANNOTATION_MAX_SELECTOR_CHARS
  ) {
    parts.shift();
  }
  return parts.join(" > ");
}

function stableAttributes(
  element: Element,
  liveDocument: Document,
): Record<string, string> {
  const attributes: Record<string, string> = {};
  const addAttribute = (name: string, value: string | null) => {
    if (
      !value ||
      name.length > HTML_ANNOTATION_MAX_ATTRIBUTE_NAME_CHARS ||
      Object.keys(attributes).length >= HTML_ANNOTATION_MAX_ATTRIBUTES
    ) {
      return;
    }
    attributes[name] = bounded(
      value,
      HTML_ANNOTATION_MAX_ATTRIBUTE_VALUE_CHARS,
    );
  };
  const id = element.getAttribute("id");
  if (id && uniqueIdSelector(id, liveDocument)) {
    addAttribute("id", id);
  }
  for (const name of STABLE_ATTRIBUTE_NAMES) {
    addAttribute(name, element.getAttribute(name));
  }
  if (element.tagName.toLowerCase() === "a") {
    const href = element.getAttribute("href");
    if (href && !/^javascript:/i.test(href)) addAttribute("href", href);
  }
  for (const { name, value } of Array.from(element.attributes)) {
    if (name.startsWith("data-") && !name.startsWith("data-copaw-")) {
      addAttribute(name, value);
    }
  }
  return attributes;
}

function computedStyles(element: Element): Record<string, string> {
  const view = element.ownerDocument.defaultView;
  if (!view) return {};
  const style = view.getComputedStyle(element);
  return Object.fromEntries(
    COMPUTED_STYLE_NAMES.map((name) => [
      name,
      bounded(
        style.getPropertyValue(name),
        HTML_ANNOTATION_MAX_ATTRIBUTE_VALUE_CHARS,
      ),
    ]),
  );
}

export function buildAnnotationTarget(
  element: Element,
  options: { canonicalHtml: string; liveDocument: Document },
): HtmlAnnotationTarget {
  const selector = buildStructuralSelector(element, options.liveDocument);
  const attributes = stableAttributes(element, options.liveDocument);
  const canonicalDocument = new DOMParser().parseFromString(
    options.canonicalHtml,
    "text/html",
  );
  let sourceResolvable = false;
  try {
    const sourceMatches = canonicalDocument.querySelectorAll(selector);
    const sourceMatch = sourceMatches.length === 1 ? sourceMatches[0] : null;
    sourceResolvable = Boolean(
      sourceMatch &&
        sourceMatch.tagName === element.tagName &&
        (attributes.id
          ? sourceMatch.getAttribute("id") === attributes.id
          : sourceMatch.outerHTML === element.outerHTML),
    );
  } catch {
    sourceResolvable = false;
  }
  const text = (element.textContent || "").replace(/\s+/g, " ").trim();
  const parent = element.parentElement;
  return {
    runtime_generated: !sourceResolvable,
    tag_name: bounded(element.tagName.toLowerCase(), 64),
    role:
      bounded(
        element.getAttribute("role") || "",
        HTML_ANNOTATION_MAX_ROLE_CHARS,
      ) || undefined,
    stable_attributes: attributes,
    selector,
    sibling_index: parent ? Array.from(parent.children).indexOf(element) : 0,
    text_quote: text
      ? { exact: bounded(text, HTML_ANNOTATION_MAX_TEXT_QUOTE_CHARS) }
      : undefined,
    rendered_html: bounded(
      element.outerHTML,
      HTML_ANNOTATION_MAX_EXCERPT_CHARS,
    ),
    ancestor_html: parent
      ? bounded(parent.outerHTML, HTML_ANNOTATION_MAX_EXCERPT_CHARS)
      : undefined,
    computed_style: computedStyles(element),
  };
}

const MEANINGFUL_SELECTOR = [
  "a",
  "button",
  "input",
  "select",
  "textarea",
  "label",
  "li",
  "section",
  "article",
  "header",
  "footer",
  "nav",
  "main",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "[role]",
].join(",");

export function resolveMeaningfulElement(element: Element): Element {
  return element.closest(MEANINGFUL_SELECTOR) || element;
}
