import type { IAgentScopeRuntimeWebUISession } from "@/components/agentscope-chat";

type SessionWithIdentity = IAgentScopeRuntimeWebUISession & {
  realId?: string;
  sessionId?: string;
};

type SessionIdentity = Partial<SessionWithIdentity> & {
  id?: string;
};

function identityKeys(session: SessionIdentity): string[] {
  return [session.id, session.realId, session.sessionId].filter(
    (value): value is string => Boolean(value),
  );
}

function hasExcludedIdentity(
  session: IAgentScopeRuntimeWebUISession,
  excludedIdentityKeys: ReadonlySet<string>,
): boolean {
  return identityKeys(session).some((key) => excludedIdentityKeys.has(key));
}

export function mergeConcurrentSessions(
  incoming: IAgentScopeRuntimeWebUISession[],
  current: IAgentScopeRuntimeWebUISession[],
  preserveCurrentDetails: boolean,
  excludedSessions: SessionIdentity[] = [],
): IAgentScopeRuntimeWebUISession[] {
  const excludedIdentityKeys = new Set(excludedSessions.flatMap(identityKeys));
  const incomingKeys = new Set(incoming.flatMap(identityKeys));
  const currentByKey = new Map<
    string,
    { session: SessionWithIdentity; index: number }
  >();
  current.forEach((session, index) => {
    identityKeys(session).forEach((key) => {
      if (!currentByKey.has(key)) currentByKey.set(key, { session, index });
    });
  });
  const currentOnly = current.filter(
    (currentSession) =>
      !hasExcludedIdentity(currentSession, excludedIdentityKeys) &&
      !identityKeys(currentSession).some((key) => incomingKeys.has(key)),
  );
  const mergedIncoming = incoming.map((incomingSession) => {
    // Multiple aliases can match different rows; retain the first current row.
    let match: { session: SessionWithIdentity; index: number } | undefined;
    identityKeys(incomingSession).forEach((key) => {
      const candidate = currentByKey.get(key);
      if (candidate && (!match || candidate.index < match.index))
        match = candidate;
    });
    const currentSession = match?.session;
    if (!currentSession) return incomingSession;

    const merged = preserveCurrentDetails
      ? { ...incomingSession, ...currentSession }
      : { ...currentSession, ...incomingSession };
    merged.messages = currentSession.messages;
    if (Object.prototype.hasOwnProperty.call(currentSession, "generating")) {
      merged.generating = currentSession.generating;
    }
    return merged;
  });

  return [...currentOnly, ...mergedIncoming];
}
