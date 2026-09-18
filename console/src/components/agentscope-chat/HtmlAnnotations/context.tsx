import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
  type PropsWithChildren,
} from "react";
import type { HtmlAnnotationBundle } from "./types";

interface HtmlAnnotationContextValue {
  activeChatKey: string;
  composerAvailable: boolean;
  pendingBundle: HtmlAnnotationBundle | null;
  stageBundle: (bundle: HtmlAnnotationBundle) => void;
  removeBundle: () => void;
  consumeBundle: (token: string) => void;
  migrateBundle: (
    fromChatKey: string,
    toChatKey: string,
    token?: string,
  ) => void;
}

type State = Record<string, HtmlAnnotationBundle>;
type Action =
  | { type: "stage"; bundle: HtmlAnnotationBundle }
  | { type: "remove"; chatKey: string }
  | { type: "consume"; token: string }
  | {
      type: "migrate";
      fromChatKey: string;
      toChatKey: string;
      token?: string;
    };

function reducer(state: State, action: Action): State {
  if (action.type === "stage") {
    return { ...state, [action.bundle.chatKey]: action.bundle };
  }
  if (action.type === "consume") {
    const match = Object.entries(state).find(
      ([, bundle]) => bundle.token === action.token,
    );
    if (!match) return state;
    const next = { ...state };
    delete next[match[0]];
    return next;
  }
  if (action.type === "migrate") {
    if (action.fromChatKey === action.toChatKey) return state;
    const source = state[action.fromChatKey];
    if (!source || (action.token && source.token !== action.token)) {
      return state;
    }
    const destination = state[action.toChatKey];
    if (destination && destination.token !== source.token) return state;
    const next = { ...state };
    delete next[action.fromChatKey];
    next[action.toChatKey] = { ...source, chatKey: action.toChatKey };
    return next;
  }
  const current = state[action.chatKey];
  if (!current) return state;
  const next = { ...state };
  delete next[action.chatKey];
  return next;
}

const unavailableContext: HtmlAnnotationContextValue = {
  activeChatKey: "",
  composerAvailable: false,
  pendingBundle: null,
  stageBundle: () => undefined,
  removeBundle: () => undefined,
  consumeBundle: () => undefined,
  migrateBundle: () => undefined,
};

const HtmlAnnotationContext = createContext(unavailableContext);

export function HtmlAnnotationProvider({
  activeChatKey,
  composerAvailable,
  children,
}: PropsWithChildren<{
  activeChatKey: string;
  composerAvailable: boolean;
}>) {
  const [bundles, dispatch] = useReducer(reducer, {});
  const stageBundle = useCallback(
    (bundle: HtmlAnnotationBundle) => {
      if (bundle.chatKey === activeChatKey) {
        dispatch({ type: "stage", bundle });
      }
    },
    [activeChatKey],
  );
  const removeBundle = useCallback(
    () => dispatch({ type: "remove", chatKey: activeChatKey }),
    [activeChatKey],
  );
  const consumeBundle = useCallback(
    (token: string) => dispatch({ type: "consume", token }),
    [],
  );
  const migrateBundle = useCallback(
    (fromChatKey: string, toChatKey: string, token?: string) =>
      dispatch({ type: "migrate", fromChatKey, toChatKey, token }),
    [],
  );
  const value = useMemo<HtmlAnnotationContextValue>(
    () => ({
      activeChatKey,
      composerAvailable,
      pendingBundle: bundles[activeChatKey] || null,
      stageBundle,
      removeBundle,
      consumeBundle,
      migrateBundle,
    }),
    [
      activeChatKey,
      bundles,
      composerAvailable,
      consumeBundle,
      migrateBundle,
      removeBundle,
      stageBundle,
    ],
  );
  return (
    <HtmlAnnotationContext.Provider value={value}>
      {children}
    </HtmlAnnotationContext.Provider>
  );
}

export const useHtmlAnnotations = () => useContext(HtmlAnnotationContext);
