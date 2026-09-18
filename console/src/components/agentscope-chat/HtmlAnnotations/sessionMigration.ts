import type { HtmlAnnotationBundle } from "./types";

export const isAnnotationForNewChat = (
  bundle: HtmlAnnotationBundle | null,
): boolean => bundle?.chatKey === "new-chat";
