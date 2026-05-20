import { request as apiRequest } from "../request";

export interface SuggestionsRequest {
  sessionId: string;
  turnId?: string;
}

interface BackendSuggestionEntry {
  id?: string;
  suggestions?: unknown;
}

interface BackendSuggestionsResponse {
  suggestions?: BackendSuggestionEntry[];
}

const MOCK_DELAY = 500;
const DEFAULT_ENABLE_MOCK = false;

const MOCK_SUGGESTIONS = [
  "能给我一个总结吗",
  "下一步该怎么做",
  "有哪些风险点需要注意",
];

function normalizeSuggestions(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean);
}

function buildMockSuggestions(): string[] {
  return MOCK_SUGGESTIONS;
}

export async function fetchSuggestions(
  payload: SuggestionsRequest,
): Promise<string[]> {
  const sessionId = payload.sessionId.trim();
  if (!sessionId) {
    return [];
  }

  try {
    if (DEFAULT_ENABLE_MOCK) {
      await new Promise((resolve) => setTimeout(resolve, MOCK_DELAY));
      return buildMockSuggestions();
    }

    const query = new URLSearchParams({ session_id: sessionId });
    if (payload.turnId?.trim()) {
      query.set("turn_id", payload.turnId.trim());
    }
    const result = await apiRequest<BackendSuggestionsResponse>(
      `/console/suggestions?${query.toString()}`,
    );

    return (result.suggestions ?? []).flatMap((entry) =>
      normalizeSuggestions(entry?.suggestions),
    );
  } catch (error) {
    console.error("[Suggestions] API request error:", error);
    return [];
  }
}
