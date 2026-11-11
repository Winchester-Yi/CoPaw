import React from "react";
import { Switch, Tooltip } from "antd";
import type { IAgentScopeRuntimeWebUIInputData } from "@/components/agentscope-chat";
import { CloseCircleFilled } from "@ant-design/icons";
import { Lightbulb } from "lucide-react";
import { ComposerQuickMenuItem } from "@/components/agentscope-chat/ComposerQuickMenu";
import styles from "./index.module.less";

const PLAN_MODE_BUTTON_EXIT_MS = 180;

export type ChatPlanMode = "plan" | "normal";

export type PlanModeSubmitCancelled = {
  shouldSubmit: false;
  clearInput?: boolean;
};

export type PlanModeSubmitResult =
  | IAgentScopeRuntimeWebUIInputData
  | PlanModeSubmitCancelled;

export type PlanModeSessionLike = {
  id?: string;
  realId?: string;
  sessionId?: string;
  session_id?: string;
  meta?: Record<string, unknown> | null;
};

export type PlanModeLocalState = {
  scopeKey: string;
  enabled: boolean;
  aliases?: string[];
};

export function getScopedPlanModeEnabled({
  metadataEnabled,
  localState,
  scopeKey,
}: {
  metadataEnabled: boolean;
  localState: PlanModeLocalState;
  scopeKey: string;
}): boolean {
  return localState.scopeKey === scopeKey ||
    localState.aliases?.includes(scopeKey)
    ? localState.enabled
    : metadataEnabled;
}

type PersistPlanModeStateOptions<TSession extends PlanModeSessionLike> = {
  enabled: boolean;
  session: TSession | null;
  ensureChatId: (
    session: TSession | null,
    meta: Record<string, unknown>,
  ) => Promise<string | null>;
  updateChat: (
    chatId: string,
    payload: { meta: Record<string, unknown> },
  ) => Promise<{ meta?: Record<string, unknown> | null }>;
  updateSession: (session: {
    id: string;
    meta: Record<string, unknown>;
  }) => Promise<unknown>;
  setPlanModeEnabled: (enabled: boolean) => void;
  onPersistError?: (error: unknown) => void;
};

export function getPlanModeEnabled(
  session?: PlanModeSessionLike | null,
): boolean {
  return session?.meta?.plan_mode_enabled === true;
}

export function buildPlanModeMeta(
  meta: Record<string, unknown> | null | undefined,
  enabled: boolean,
): Record<string, unknown> {
  const writableMeta = Object.fromEntries(
    Object.entries(meta || {}).filter(
      ([key]) =>
        key !== "goal_draft_subagent_delegation_enabled" &&
        key !== "multi_agent_mode_enabled" &&
        key !== "expert_main_binding",
    ),
  );
  return {
    ...writableMeta,
    plan_mode_enabled: enabled,
  };
}

export function getPlanModeForRequest(enabled: boolean): ChatPlanMode {
  return enabled ? "plan" : "normal";
}

export function resolveActivePlanModeSession<
  TSession extends PlanModeSessionLike,
>(
  sessions: TSession[],
  ids: Array<string | null | undefined>,
): TSession | null {
  const idSet = new Set(ids.filter((value): value is string => Boolean(value)));

  if (idSet.size === 0) {
    return null;
  }

  for (const id of idSet) {
    const match = sessions.find((session) =>
      [session.id, session.realId, session.sessionId, session.session_id].some(
        (value) => value === id,
      ),
    );
    if (match) {
      return match;
    }
  }

  return null;
}

function withPlanMode(
  data: IAgentScopeRuntimeWebUIInputData,
  mode: ChatPlanMode,
): IAgentScopeRuntimeWebUIInputData {
  const explicitMode =
    data.biz_params?.mode === "plan" || data.biz_params?.mode === "normal"
      ? data.biz_params.mode
      : mode;
  return {
    ...data,
    biz_params: {
      ...(data.biz_params || {}),
      mode: explicitMode,
    },
  };
}

function parsePlanCommand(query: string): string | null {
  const match = query.match(/^\/plan(?:\s+([\s\S]*))?$/i);
  if (!match) {
    return null;
  }
  return (match[1] || "").trim();
}

export async function preparePlanModeSubmit(
  data: IAgentScopeRuntimeWebUIInputData,
  options: {
    planModeEnabled: boolean;
    persistPlanMode: (enabled: boolean) => Promise<void>;
    setPlanModeEnabled?: (enabled: boolean) => void;
  },
): Promise<PlanModeSubmitResult> {
  const planCommandText = parsePlanCommand(data.query.trim());
  if (planCommandText === null) {
    return withPlanMode(data, getPlanModeForRequest(options.planModeEnabled));
  }

  if (!planCommandText) {
    await options.persistPlanMode(true);
    return {
      shouldSubmit: false,
      clearInput: true,
    };
  }

  await options.persistPlanMode(true);

  return withPlanMode(
    {
      ...data,
      query: planCommandText,
    },
    "plan",
  );
}

export function isPlanModeSubmitCancelled(
  result: unknown,
): result is PlanModeSubmitCancelled {
  return (
    Boolean(result) &&
    typeof result === "object" &&
    (result as PlanModeSubmitCancelled).shouldSubmit === false
  );
}

export async function persistPlanModeState<
  TSession extends PlanModeSessionLike,
>({
  enabled,
  session,
  ensureChatId,
  updateChat,
  updateSession,
  setPlanModeEnabled,
  onPersistError,
}: PersistPlanModeStateOptions<TSession>): Promise<void> {
  const nextMeta = buildPlanModeMeta(session?.meta, enabled);

  setPlanModeEnabled(enabled);

  try {
    const targetChatId = await ensureChatId(session, nextMeta);
    if (!targetChatId) {
      throw new Error("Missing chat id for Plan Mode state");
    }

    const updated = await updateChat(targetChatId, {
      meta: nextMeta,
    });

    await updateSession({
      id: session?.id || targetChatId,
      meta: updated.meta || nextMeta,
    });
  } catch (error) {
    setPlanModeEnabled(getPlanModeEnabled(session));
    onPersistError?.(error);
    throw error;
  }
}

export function PlanModeMenuItem({
  ariaLabel,
  enabled,
  disabled = false,
  label,
  showIcon = true,
  tooltip,
  onChange,
}: {
  ariaLabel?: string;
  enabled: boolean;
  disabled?: boolean;
  label: string;
  showIcon?: boolean;
  tooltip?: string;
  onChange: (enabled: boolean) => void;
}) {
  const switchLabel = ariaLabel || label;
  const control = (
    <ComposerQuickMenuItem
      icon={
        showIcon ? (
          <Lightbulb size={18} strokeWidth={1.5} aria-hidden />
        ) : undefined
      }
      interactive
      label={label}
      extra={
        <Switch
          size="small"
          checked={enabled}
          disabled={disabled}
          aria-label={switchLabel}
          onChange={(checked) => onChange(checked)}
        />
      }
    />
  );

  return tooltip ? <Tooltip title={tooltip}>{control}</Tooltip> : control;
}

export function ActivePlanModeButton({
  enabled,
  disabled = false,
  label,
  displayLabel,
  showIcon = true,
  icon = <Lightbulb size={18} strokeWidth={1.5} aria-hidden />,
  onDisable,
}: {
  enabled: boolean;
  disabled?: boolean;
  label: string;
  displayLabel?: string;
  showIcon?: boolean;
  icon?: React.ReactNode;
  onDisable: () => void;
}) {
  const [isRendered, setIsRendered] = React.useState(enabled);
  const [isExiting, setIsExiting] = React.useState(false);
  const exitTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    if (exitTimerRef.current) {
      clearTimeout(exitTimerRef.current);
      exitTimerRef.current = null;
    }

    if (enabled) {
      setIsRendered(true);
      setIsExiting(false);
      return undefined;
    }

    if (!isRendered) {
      setIsExiting(false);
      return undefined;
    }

    setIsExiting(true);
    exitTimerRef.current = setTimeout(() => {
      setIsRendered(false);
      setIsExiting(false);
      exitTimerRef.current = null;
    }, PLAN_MODE_BUTTON_EXIT_MS);

    return () => {
      if (exitTimerRef.current) {
        clearTimeout(exitTimerRef.current);
        exitTimerRef.current = null;
      }
    };
  }, [enabled, isRendered]);

  if (!enabled && !isRendered) {
    return null;
  }

  return (
    <button
      type="button"
      className={styles.planModeActiveButton}
      aria-label={label}
      data-plan-mode-exiting={isExiting ? "true" : undefined}
      disabled={disabled || isExiting}
      onClick={onDisable}
    >
      {showIcon && <span className={styles.planModeActiveIcon}>{icon}</span>}
      <CloseCircleFilled className={styles.planModeCloseIcon} />
      <span>{displayLabel || label}</span>
    </button>
  );
}
