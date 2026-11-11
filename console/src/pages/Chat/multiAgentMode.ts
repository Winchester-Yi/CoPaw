export function getMultiAgentModeEnabled(
  session?: { meta?: Record<string, unknown> | null } | null,
): boolean {
  return session?.meta?.multi_agent_mode_enabled === true;
}

export function withMultiAgentIntent<T extends Record<string, unknown>>(
  params: T,
  enabled: boolean,
) {
  return { ...params, enable_subagents: enabled };
}
