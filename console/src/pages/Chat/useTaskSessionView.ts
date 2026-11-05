import { useEffect, useRef } from "react";
import { useChatAnywhereSessionsState } from "@/components/agentscope-chat";
import type { TaskPresence } from "@/api/types/cronTaskSource";
import { mergeConcurrentSessions } from "./components/ChatSidebar/mergeConcurrentSessions";
import sessionApi from "./sessionApi";

/** Refresh a loaded view from source facts without another summary/detail request. */
export function useTaskSessionView(
  presence: TaskPresence,
  scopeKey: string,
  active: boolean,
) {
  const { sessions, setSessions } = useChatAnywhereSessionsState();
  const latest = useRef(sessions);
  latest.current = sessions;
  const previousScope = useRef(scopeKey);
  const retainedDetails = useRef(sessions);
  useEffect(() => {
    const changed = previousScope.current !== scopeKey;
    previousScope.current = scopeKey;
    const loaded = sessionApi.getLoadedSessionView();
    if (!active || changed) {
      retainedDetails.current = active ? loaded : [];
      setSessions(retainedDetails.current);
      return;
    }
    const details = mergeConcurrentSessions(
      retainedDetails.current,
      latest.current,
      true,
    );
    retainedDetails.current = mergeConcurrentSessions(loaded, details, true);
    setSessions(sessionApi.projectSessionView(retainedDetails.current));
  }, [presence.legacy, presence.scheduler, scopeKey, active, setSessions]);
}
