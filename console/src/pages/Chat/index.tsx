// ==================== 组件引入方式变更 (Kun He) ====================
import {
  AgentScopeRuntimeWebUILayout,
  AgentScopeRuntimeWebUIComposedProvider,
  IAgentScopeRuntimeWebUIOptions,
  type IAgentScopeRuntimeWebUISenderOptions,
  type IAgentScopeRuntimeWebUIRef,
  useChatAnywhereSessionsState,
} from "@/components/agentscope-chat";
import AgentScopeRuntimeRequestCard from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Request/Card";
import AgentScopeRuntimeResponseCard from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Card";
// ==================== 组件引入方式变更结束 ====================
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
} from "react";
import { flushSync } from "react-dom";
import { Button, Modal, Result, Tooltip } from "antd";
import { useAppMessage } from "../../hooks/useAppMessage";
import { ExclamationCircleOutlined, SettingOutlined } from "@ant-design/icons";
import { SparkCopyLine, SparkAttachmentLine } from "@agentscope-ai/icons";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import sessionApi from "./sessionApi";
import defaultConfig, { getDefaultConfig } from "./OptionsPanel/defaultConfig";
import { chatApi } from "../../api/modules/chat";
import { cronJobApi } from "../../api/modules/cronjob";
import { getApiUrl } from "../../api/config";
import { buildAuthHeaders } from "../../api/authHeaders";
import { providerApi } from "../../api/modules/provider";
import type {
  ProviderInfo,
  ModelInfo,
  CronJobSpecOutput,
} from "../../api/types";
import ModelSelector from "./ModelSelector";
import { useTheme } from "../../contexts/ThemeContext";
import { useAgentStore } from "../../stores/agentStore";
// ==================== 组件引入方式变更 (Kun He) ====================
import { useChatAnywhereInput } from "@/components/agentscope-chat";
import DragUploadOverlay from "@/components/agentscope-chat/DragUploadOverlay";
// ==================== 组件引入方式变更结束 ====================
// ==================== userId 统一整改 (Kun He) ====================
// 使用统一的 getUserId/getChannel helper
import { getUserId, getChannel } from "../../utils/identity";
// ==================== userId 统一整改结束 ====================
// ==================== 品牌主题 (Kun He) ====================
import { useBrandTheme } from "../../contexts/BrandThemeContext";
// ==================== 品牌主题结束 ====================
// ==================== URL 导航参数 (Kun He, 2026-04-15) ====================
import { useIframeStore } from "../../stores/iframeStore";
// ==================== URL 导航参数结束 ====================
import styles from "./index.module.less";
import { IconButton } from "@agentscope-ai/design";
// import ChatActionGroup from "./components/ChatActionGroup";
import ChatHeaderTitle from "./components/ChatHeaderTitle";
import ChatSessionInitializer from "./components/ChatSessionInitializer";
import ConversationQuickNav from "@/components/ConversationQuickNav";
// ==================== 首页改版 (Kun He) ====================
import WelcomeCenterLayout from "@/components/agentscope-chat/WelcomeCenterLayout";
import ChatSidebar from "./components/ChatSidebar";
// ==================== 首页改版结束 ====================
// ==================== 自定义工具渲染器 (customToolRenderConfig) ====================
import CopyFileToStatic from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/customToolRenders/CopyFileToStatic";
// ==================== 自定义工具渲染器结束 ====================
import {
  toDisplayUrl,
  copyText,
  extractCopyableText,
  buildModelError,
  normalizeContentUrls,
  extractUserMessageText,
  type CopyableResponse,
  type RuntimeLoadingBridgeApi,
} from "./utils";
import {
  deriveChatTaskState,
  getTaskOpenTarget,
  shouldMarkTaskReadOnOpen,
} from "./taskJobs";
import { shouldRefreshCurrentTaskMessages } from "./taskMessageRefresh";
import { matchesResolvedChatId } from "./sessionApi/resolvedSessionMapping";

import RuntimeRequestCard from "./components/RuntimeRequestCard";
import { FOLLOW_UP_SUBMIT_FAILED_EVENT } from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/followUpSubmit";
import {
  createChatStreamAbortReason,
  shouldStopBackendForFetchAbort,
} from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/abortReasons";
import RuntimeResponseCard from "./components/RuntimeResponseCard";
import ApprovalActionCard from "./components/ApprovalActionCard";
import TaskRunGroupCard from "./components/TaskRunGroupCard";
import TaskProgressFloatingCard from "./components/TaskProgressFloatingCard";
import GeneratedFilesDrawer from "./components/GeneratedFilesDrawer";
import type {
  ChatApprovalActionCardData,
  ChatRuntimeRequestCardData,
  ChatRuntimeResponseCardData,
  ChatTaskRunGroupCardData,
} from "./messageMeta";
import {
  CHAT_TASK_PROGRESS_UPDATE_EVENT,
  type ChatTaskProgressData,
} from "./taskProgressEvents";

const CHAT_ATTACHMENT_MAX_MB = 10;
const TASK_RUNNING_POLL_MS = 30_000;
const TASK_PAGE_POLL_MS = 30_000;
const TASK_PENDING_POLL_MS = 30_000;

function createTimedAbortSignal(
  externalSignal?: AbortSignal,
  timeoutMs: number | null = null,
) {
  const controller = new AbortController();

  const abortWithReason = (reason?: unknown) => {
    if (controller.signal.aborted) return;
    controller.abort(
      reason ?? new DOMException("The operation was aborted.", "AbortError"),
    );
  };

  if (externalSignal?.aborted) {
    abortWithReason(externalSignal.reason);
  }

  const handleExternalAbort = () => {
    abortWithReason(externalSignal?.reason);
  };

  if (externalSignal && !externalSignal.aborted) {
    externalSignal.addEventListener("abort", handleExternalAbort, {
      once: true,
    });
  }

  const timeoutId =
    typeof timeoutMs === "number" && Number.isFinite(timeoutMs) && timeoutMs > 0
      ? window.setTimeout(() => {
          const elapsedSeconds = Math.ceil(timeoutMs / 1000);
          abortWithReason(
            createChatStreamAbortReason(
              "timeout",
              `任务执行超时（${elapsedSeconds}s），已自动终止。`,
            ),
          );
        }, timeoutMs)
      : undefined;

  return {
    signal: controller.signal,
    cleanup: () => {
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
      if (externalSignal) {
        externalSignal.removeEventListener("abort", handleExternalAbort);
      }
    },
  };
}

interface SessionInfo {
  session_id?: string;
  user_id?: string;
  channel?: string;
}

interface ChatRequestTarget {
  session_id?: string;
  logical_session_id?: string;
  chat_id?: string | null;
}

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

interface CommandSuggestion {
  command: string;
  value: string;
  description: string;
}

type InputMessage = {
  role?: string;
  content?: unknown;
};

type AttachmentTriggerProps = {
  disabled?: boolean;
};

function renderSuggestionLabel(command: string, description: string) {
  return (
    <div className={styles.suggestionLabel}>
      <span className={styles.suggestionCommand}>{command}</span>
      <span className={styles.suggestionDescription}>{description}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// ==================== userId 统一整改 (Kun He) ====================
// DEFAULT_USER_ID 和 DEFAULT_CHANNEL 已移至 constants/identity.ts
// 通过 getUserId() 和 getChannel() 获取
// ==================== userId 统一整改结束 ====================

// ---------------------------------------------------------------------------
// Custom hooks
// ---------------------------------------------------------------------------

/** Handle IME composition events to prevent premature Enter key submission. */
function useIMEComposition(isChatActive: () => boolean) {
  const isComposingRef = useRef(false);

  useEffect(() => {
    const handleCompositionStart = () => {
      if (!isChatActive()) return;
      isComposingRef.current = true;
    };

    const handleCompositionEnd = () => {
      if (!isChatActive()) return;
      // Use a slightly longer delay for Safari on macOS, which fires keydown
      // after compositionend within the same event loop tick.
      setTimeout(() => {
        isComposingRef.current = false;
      }, 200);
    };

    const suppressImeEnter = (e: KeyboardEvent) => {
      if (!isChatActive()) return;
      const target = e.target as HTMLElement;
      if (target?.tagName === "TEXTAREA" && e.key === "Enter" && !e.shiftKey) {
        // e.isComposing is the standard flag; isComposingRef covers the
        // post-compositionend grace period needed by Safari.
        if (isComposingRef.current || e.isComposing) {
          e.stopPropagation();
          e.stopImmediatePropagation();
          e.preventDefault();
          return false;
        }
      }
    };

    document.addEventListener("compositionstart", handleCompositionStart, true);
    document.addEventListener("compositionend", handleCompositionEnd, true);
    // Listen on both keydown (Safari) and keypress (legacy) in capture phase.
    document.addEventListener("keydown", suppressImeEnter, true);
    document.addEventListener("keypress", suppressImeEnter, true);

    return () => {
      document.removeEventListener(
        "compositionstart",
        handleCompositionStart,
        true,
      );
      document.removeEventListener(
        "compositionend",
        handleCompositionEnd,
        true,
      );
      document.removeEventListener("keydown", suppressImeEnter, true);
      document.removeEventListener("keypress", suppressImeEnter, true);
    };
  }, [isChatActive]);

  return isComposingRef;
}

/** Fetch and track multimodal capabilities for the active model. */
function useMultimodalCapabilities(
  refreshKey: number,
  locationPathname: string,
  isChatActive: () => boolean,
  selectedAgent: string,
) {
  const [multimodalCaps, setMultimodalCaps] = useState<{
    supportsMultimodal: boolean;
    supportsImage: boolean;
    supportsVideo: boolean;
  }>({ supportsMultimodal: false, supportsImage: false, supportsVideo: false });

  const fetchMultimodalCaps = useCallback(async () => {
    try {
      const [providers, activeModels] = await Promise.all([
        providerApi.listProviders(),
        providerApi.getActiveModels({
          scope: "effective",
          agent_id: selectedAgent,
        }),
      ]);
      const activeProviderId = activeModels?.active_llm?.provider_id;
      const activeModelId = activeModels?.active_llm?.model;
      if (!activeProviderId || !activeModelId) {
        setMultimodalCaps({
          supportsMultimodal: false,
          supportsImage: false,
          supportsVideo: false,
        });
        return;
      }
      const provider = (providers as ProviderInfo[]).find(
        (p) => p.id === activeProviderId,
      );
      if (!provider) {
        setMultimodalCaps({
          supportsMultimodal: false,
          supportsImage: false,
          supportsVideo: false,
        });
        return;
      }
      const allModels: ModelInfo[] = [
        ...(provider.models ?? []),
        ...(provider.extra_models ?? []),
      ];
      const model = allModels.find((m) => m.id === activeModelId);
      setMultimodalCaps({
        supportsMultimodal: model?.supports_multimodal ?? false,
        supportsImage: model?.supports_image ?? false,
        supportsVideo: model?.supports_video ?? false,
      });
    } catch {
      setMultimodalCaps({
        supportsMultimodal: false,
        supportsImage: false,
        supportsVideo: false,
      });
    }
  }, [selectedAgent]);

  // Fetch caps on mount and whenever refreshKey changes
  useEffect(() => {
    fetchMultimodalCaps();
  }, [fetchMultimodalCaps, refreshKey]);

  // Also poll caps when navigating back to chat
  useEffect(() => {
    if (isChatActive()) {
      fetchMultimodalCaps();
    }
  }, [locationPathname, fetchMultimodalCaps, isChatActive]);

  // Listen for model-switched event from ModelSelector
  useEffect(() => {
    const handler = () => {
      fetchMultimodalCaps();
    };
    window.addEventListener("model-switched", handler);
    return () => window.removeEventListener("model-switched", handler);
  }, [fetchMultimodalCaps]);

  return multimodalCaps;
}

function RuntimeLoadingBridge({
  bridgeRef,
}: {
  bridgeRef: { current: RuntimeLoadingBridgeApi | null };
}) {
  const { setLoading, getLoading } = useChatAnywhereInput(
    (value) =>
      ({
        setLoading: value.setLoading,
        getLoading: value.getLoading,
      }) as RuntimeLoadingBridgeApi,
  );

  useEffect(() => {
    if (!setLoading || !getLoading) {
      bridgeRef.current = null;
      return;
    }

    bridgeRef.current = {
      setLoading,
      getLoading,
    };

    return () => {
      if (bridgeRef.current?.setLoading === setLoading) {
        bridgeRef.current = null;
      }
    };
  }, [getLoading, setLoading, bridgeRef]);

  return null;
}

export default function ChatPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { isDark } = useTheme();
  // ==================== 品牌主题 (Kun He) ====================
  // 获取动态品牌配置，用于 welcome avatar
  const { theme: brandTheme } = useBrandTheme();
  // ==================== 品牌主题结束 ====================
  const chatId = useMemo(() => {
    const match = location.pathname.match(/^\/chat\/(.+)$/);
    return match?.[1];
  }, [location.pathname]);
  const [showModelPrompt, setShowModelPrompt] = useState(false);
  const [jobs, setJobs] = useState<CronJobSpecOutput[]>([]);
  const [taskProgress, setTaskProgress] = useState<ChatTaskProgressData | null>(
    null,
  );
  const { selectedAgent } = useAgentStore();
  const [refreshKey, setRefreshKey] = useState(0);
  const [isDragging, setIsDragging] = useState(false);
  const dragCounterRef = useRef(0);
  const runtimeLoadingBridgeRef = useRef<RuntimeLoadingBridgeApi | null>(null);
  const { message } = useAppMessage();
  const { setSessionLoading, setSessions } = useChatAnywhereSessionsState();

  // useTransition for non-urgent state updates (badge clearing)
  const [, startTransition] = useTransition();
  // Debounce flag for markTaskRead API calls
  const markTaskReadPendingRef = useRef(false);

  const isChatActiveRef = useRef(false);
  isChatActiveRef.current =
    location.pathname === "/" || location.pathname.startsWith("/chat");

  useEffect(() => {
    const handler = () => {
      message.error(t("chat.followUp.autoSubmitFailed"));
    };

    document.addEventListener(FOLLOW_UP_SUBMIT_FAILED_EVENT, handler);
    return () =>
      document.removeEventListener(FOLLOW_UP_SUBMIT_FAILED_EVENT, handler);
  }, [message, t]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<ChatTaskProgressData | null>).detail;
      if (!detail) {
        setTaskProgress(null);
        return;
      }
      setTaskProgress((previous) => {
        if (
          previous &&
          previous.turn_id === detail.turn_id &&
          previous.version > detail.version
        ) {
          return previous;
        }
        return detail;
      });
    };

    document.addEventListener(CHAT_TASK_PROGRESS_UPDATE_EVENT, handler);
    return () =>
      document.removeEventListener(CHAT_TASK_PROGRESS_UPDATE_EVENT, handler);
  }, []);

  const isChatActive = useCallback(() => isChatActiveRef.current, []);

  // Use custom hooks for better separation of concerns
  const isComposingRef = useIMEComposition(isChatActive);
  const multimodalCaps = useMultimodalCapabilities(
    refreshKey,
    location.pathname,
    isChatActive,
    selectedAgent,
  );

  const lastSessionIdRef = useRef<string | null>(null);
  /** Tracks the stale auto-selected session ID that was skipped on init, so we can suppress its late-arriving onSessionSelected callback. */
  const staleAutoSelectedIdRef = useRef<string | null>(null);
  const taskHadResultRef = useRef(false);
  const previousCurrentTaskRef = useRef<CronJobSpecOutput | null>(null);
  const chatIdRef = useRef(chatId);
  const navigateRef = useRef(navigate);
  const chatRef = useRef<IAgentScopeRuntimeWebUIRef>(null);
  chatIdRef.current = chatId;
  navigateRef.current = navigate;

  // Tell sessionApi which session to put first in getSessionList, so the library's
  // useMount auto-selects the correct session without an extra getSession round-trip.
  if (chatId && sessionApi.preferredChatId !== chatId) {
    sessionApi.preferredChatId = chatId;
  }

  // Register session API event callbacks for URL synchronization

  useEffect(() => {
    sessionApi.onSessionIdResolved = (_tempId, realId) => {
      if (!isChatActiveRef.current) return;
      // Update URL when realId is resolved, regardless of current chatId
      // (chatId may be undefined if URL was cleared in onSessionCreated)
      lastSessionIdRef.current = realId;
      navigateRef.current(`/chat/${realId}`, { replace: true });
    };

    sessionApi.onSessionRemoved = (removedId) => {
      if (!isChatActiveRef.current) return;
      // Clear URL when current session is removed
      // Check if removed session matches current session (by realId or sessionId)
      const currentRealId = sessionApi.getRealIdForSession(
        chatIdRef.current || "",
      );
      if (chatIdRef.current === removedId || currentRealId === removedId) {
        lastSessionIdRef.current = null;
        navigateRef.current("/chat", { replace: true });
      }
    };

    sessionApi.onSessionSelected = (
      sessionId: string | null | undefined,
      realId: string | null,
    ) => {
      if (!isChatActiveRef.current) return;
      // Update URL when session is selected and different from current
      const targetId = realId || sessionId;
      if (!targetId) return;

      // If current URL's chatId differs from targetId, skip this callback.
      // This happens when user quickly switches sessions via sidebar:
      // 1. User clicks A → getSession(A) starts
      // 2. User clicks B → URL becomes /chat/B
      // 3. A's request completes → onSessionSelected(A) fires
      // 4. Should NOT navigate back to A since user already chose B
      const currentUrlChatId = chatIdRef.current;
      if (
        currentUrlChatId &&
        currentUrlChatId !== targetId &&
        !matchesResolvedChatId({
          requestedSessionId: currentUrlChatId,
          chatId: targetId,
        })
      ) {
        return;
      }

      // If a preferred chatId from the URL exists and no navigation has happened yet,
      // skip the library's initial auto-selection (always first session).
      // ChatSessionInitializer will apply the correct selection afterward.
      if (
        chatIdRef.current &&
        lastSessionIdRef.current === null &&
        targetId !== chatIdRef.current
      ) {
        lastSessionIdRef.current = targetId;
        // Record the stale ID so its delayed getSession callback is also suppressed.
        staleAutoSelectedIdRef.current = targetId;
        return;
      }

      // Suppress the stale getSession callback that arrives after the correct session loads.
      if (
        staleAutoSelectedIdRef.current &&
        staleAutoSelectedIdRef.current === targetId
      ) {
        staleAutoSelectedIdRef.current = null;
        return;
      }

      if (targetId !== lastSessionIdRef.current) {
        lastSessionIdRef.current = targetId;
        navigateRef.current(`/chat/${targetId}`, { replace: true });
      }
    };

    sessionApi.onSessionCreated = () => {
      if (!isChatActiveRef.current) return;
      // Clear URL when creating new session, wait for realId resolution to update
      lastSessionIdRef.current = null;
      navigateRef.current("/chat", { replace: true });
    };

    return () => {
      sessionApi.onSessionIdResolved = null;
      sessionApi.onSessionRemoved = null;
      sessionApi.onSessionSelected = null;
      sessionApi.onSessionCreated = null;
    };
  }, []);

  useEffect(() => {
    setTaskProgress(null);
  }, [chatId, location.pathname]);

  // ==================== URL 导航参数 (Kun He, 2026-04-15) ====================
  // 处理 iframe URL 传递的 sessionId/taskId 参数，自动跳转到对应聊天页面
  // sessionId: 可传 backend chat.id 或逻辑 session_id，后续由初始选择逻辑解析
  // taskId: 查找 task.chat_id 后导航
  const sessionIdRef = useRef<string | null>(null);
  const taskIdRef = useRef<string | null>(null);

  useEffect(() => {
    const store = useIframeStore.getState();
    const { sessionId, taskId } = store;

    // 只在首次加载时处理，避免重复导航
    if (sessionId) {
      sessionIdRef.current = sessionId;
      taskIdRef.current = null; // sessionId 优先，忽略 taskId
      store.clearNavigationParams();
      console.info("[Chat] Navigating to sessionId:", sessionId);
      navigate(`/chat/${sessionId}`, { replace: true });
      return;
    }

    if (taskId) {
      taskIdRef.current = taskId;
      store.clearNavigationParams();
      console.info("[Chat] taskId set, waiting for jobs:", taskId);
    }
  }, [navigate]);

  // taskId 导航需要等待 jobs 加载完成
  useEffect(() => {
    if (!taskIdRef.current || jobs.length === 0) return;

    const task = jobs.find((j) => j.id === taskIdRef.current);
    const chatId = task?.task?.chat_id;

    if (chatId) {
      console.info("[Chat] Navigating from taskId to chatId:", {
        taskId: taskIdRef.current,
        chatId,
      });
      navigate(`/chat/${chatId}`, { replace: true });
      taskIdRef.current = null;
    } else {
      console.warn("[Chat] taskId not found or no chat_id:", taskIdRef.current);
      taskIdRef.current = null;
    }
  }, [jobs, navigate]);
  // ==================== URL 导航参数结束 ====================

  // Setup multimodal capabilities tracking via custom hook

  // Refresh chat when selectedAgent changes
  const prevSelectedAgentRef = useRef(selectedAgent);
  useEffect(() => {
    // Only refresh if selectedAgent actually changed (not initial mount)
    if (
      prevSelectedAgentRef.current !== selectedAgent &&
      prevSelectedAgentRef.current !== undefined
    ) {
      // Force re-render by updating refresh key
      setRefreshKey((prev) => prev + 1);
    }
    prevSelectedAgentRef.current = selectedAgent;
  }, [selectedAgent]);

  const refreshJobs = useCallback(async () => {
    try {
      const nextJobs = await cronJobApi.listCronJobs();
      setJobs(Array.isArray(nextJobs) ? nextJobs : []);
    } catch {
      setJobs([]);
    }
  }, []);

  const { tasks, currentTask } = useMemo(
    () => deriveChatTaskState(jobs, chatId),
    [jobs, chatId],
  );
  const hasRunningTask = useMemo(
    () => tasks.some((task) => task.task?.is_running),
    [tasks],
  );

  useEffect(() => {
    void refreshJobs();

    // 仅从其他标签页切换回来时刷新（移除 window.focus 触发，减少不必要的 API 调用）
    const handleVisibilityRefresh = () => {
      if (document.visibilityState === "visible") {
        void refreshJobs();
      }
    };

    // 监听定时任务创建成功事件
    const handleTaskCreated = () => {
      void refreshJobs();
    };

    document.addEventListener("visibilitychange", handleVisibilityRefresh);
    document.addEventListener("taskCreated", handleTaskCreated);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityRefresh);
      document.removeEventListener("taskCreated", handleTaskCreated);
    };
  }, [refreshJobs]);

  useEffect(() => {
    const pollMs = hasRunningTask
      ? TASK_RUNNING_POLL_MS
      : currentTask?.task?.has_scheduled_result === false
      ? TASK_PENDING_POLL_MS
      : TASK_PAGE_POLL_MS;

    const intervalId = window.setInterval(() => {
      void refreshJobs();
    }, pollMs);

    return () => window.clearInterval(intervalId);
  }, [currentTask?.task?.has_scheduled_result, hasRunningTask, refreshJobs]);

  useEffect(() => {
    const hadResult = Boolean(currentTask?.task?.has_scheduled_result);
    if (hadResult && !taskHadResultRef.current) {
      setRefreshKey((prev) => prev + 1);
    }
    taskHadResultRef.current = hadResult;
  }, [currentTask?.task?.has_scheduled_result]);

  useEffect(() => {
    if (!currentTask?.id) return;
    if ((currentTask.task?.unread_execution_count || 0) <= 0) return;
    if (!shouldMarkTaskReadOnOpen(currentTask)) return;

    // Debounce: skip if there's already a pending markTaskRead request
    if (markTaskReadPendingRef.current) return;

    markTaskReadPendingRef.current = true;

    // Non-urgent update: badge clearing can be delayed
    startTransition(() => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === currentTask.id && job.task
            ? {
                ...job,
                task: {
                  ...job.task,
                  unread_execution_count: 0,
                },
              }
            : job,
        ),
      );
    });

    void cronJobApi
      .markTaskRead(currentTask.id)
      .catch(() => {})
      .finally(() => {
        markTaskReadPendingRef.current = false;
      });
  }, [currentTask?.id, currentTask?.task?.unread_execution_count]);

  const handleTaskOpen = useCallback(
    (task: CronJobSpecOutput) => {
      const taskOpenTarget = getTaskOpenTarget(task);
      if (!taskOpenTarget) return;

      // Force loading to render immediately before navigate triggers re-render
      flushSync(() => {
        setSessionLoading(true);
      });

      navigate(`/chat/${taskOpenTarget}`, { replace: true });
    },
    [navigate, setSessionLoading],
  );

  const handleTaskResume = useCallback(
    async (task: CronJobSpecOutput) => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === task.id
            ? {
                ...job,
                enabled: true,
                task: job.task
                  ? {
                      ...job.task,
                      is_paused: false,
                      pause_reason: null,
                      auto_paused_at: null,
                      unread_execution_count: 0,
                    }
                  : job.task,
              }
            : job,
        ),
      );

      try {
        await cronJobApi.resumeCronJob(task.id);
        message.success("任务已恢复");
        void refreshJobs();
      } catch {
        message.error("恢复失败");
        void refreshJobs();
      }
    },
    [message, refreshJobs],
  );

  const handleTaskPause = useCallback(
    async (task: CronJobSpecOutput) => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === task.id
            ? {
                ...job,
                enabled: false,
                task: job.task
                  ? {
                      ...job.task,
                      is_paused: true,
                      pause_reason: "manual",
                    }
                  : job.task,
              }
            : job,
        ),
      );

      try {
        await cronJobApi.pauseCronJob(task.id);
        message.success("任务已停止");
        void refreshJobs();
      } catch {
        message.error("停止失败");
        void refreshJobs();
      }
    },
    [message, refreshJobs],
  );

  const handleTaskRun = useCallback(
    async (task: CronJobSpecOutput) => {
      setJobs((prev) =>
        prev.map((job) =>
          job.id === task.id
            ? {
                ...job,
                state: {
                  ...job.state,
                  last_status: "running",
                  last_error: null,
                },
                task: job.task
                  ? {
                      ...job.task,
                      is_running: true,
                    }
                  : job.task,
              }
            : job,
        ),
      );

      try {
        await cronJobApi.runCronJob(task.id);
        message.success("任务已开始执行");
        void refreshJobs();
      } catch {
        message.error("执行失败");
        void refreshJobs();
      }
    },
    [message, refreshJobs],
  );

  const handleTaskDelete = useCallback(
    (task: CronJobSpecOutput) => {
      Modal.confirm({
        title: "删除任务",
        content: `确认删除任务“${task.name || task.id}”？删除后无法恢复。`,
        centered: true,
        okText: "删除",
        okType: "danger",
        cancelText: "取消",
        cancelButtonProps: { type: "text" },
        onOk: async () => {
          setJobs((prev) => prev.filter((job) => job.id !== task.id));
          if (task.task?.chat_id && task.task.chat_id === chatIdRef.current) {
            navigate("/chat", { replace: true });
          }
          try {
            await cronJobApi.deleteCronJob(task.id);
            message.success("任务已删除");
            void refreshJobs();
          } catch {
            message.error("删除失败");
            void refreshJobs();
          }
        },
      });
    },
    [message, navigate, refreshJobs],
  );

  useEffect(() => {
    const previousTask = previousCurrentTaskRef.current;
    previousCurrentTaskRef.current = currentTask;

    if (
      !shouldRefreshCurrentTaskMessages({
        previousTask,
        currentTask,
      })
    ) {
      return;
    }

    void chatRef.current?.refreshSession?.();
  }, [
    currentTask?.id,
    currentTask?.task?.has_scheduled_result,
    currentTask?.task?.last_scheduled_run_at,
    currentTask?.task?.unread_execution_count,
  ]);

  // Show toast when task has no scheduled result yet
  const taskNoResultShownIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (currentTask && !currentTask.task?.has_scheduled_result) {
      if (taskNoResultShownIdRef.current !== currentTask.id) {
        taskNoResultShownIdRef.current = currentTask.id;
        message.info("当前任务暂未启动，等下次收到提醒再来看看哟~");
      }
    } else {
      taskNoResultShownIdRef.current = null;
    }
  }, [currentTask?.id, currentTask?.task?.has_scheduled_result]);

  const copyResponse = useCallback(
    async (response: CopyableResponse) => {
      try {
        await copyText(extractCopyableText(response));
        message.success(t("common.copied"));
      } catch {
        message.error(t("common.copyFailed"));
      }
    },
    [t],
  );

  const resolveLogicalRequestSessionId = useCallback(
    (target: ChatRequestTarget, session?: SessionInfo): string => {
      if (target.logical_session_id) {
        return target.logical_session_id;
      }

      return sessionApi.getLogicalSessionId(
        target.session_id ||
          window.currentSessionId ||
          session?.session_id ||
          "",
      );
    },
    [],
  );

  const resolveRequestChatId = useCallback(
    (target: ChatRequestTarget, logicalSessionId: string): string => {
      return (
        target.chat_id ||
        sessionApi.getChatIdForSession(logicalSessionId) ||
        sessionApi.getChatIdForSession(target.session_id || "") ||
        target.session_id ||
        chatIdRef.current ||
        logicalSessionId
      );
    },
    [],
  );

  const customFetch = useCallback(
    async (data: {
      input?: Array<Record<string, unknown>>;
      biz_params?: Record<string, unknown>;
      signal?: AbortSignal;
      session_id?: string;
      logical_session_id?: string;
      chat_id?: string | null;
      turn_id?: string;
    }): Promise<Response> => {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        ...buildAuthHeaders(),
      };

      try {
        const activeModels = await providerApi.getActiveModels({
          scope: "effective",
          agent_id: selectedAgent,
        });
        if (
          !activeModels?.active_llm?.provider_id ||
          !activeModels?.active_llm?.model
        ) {
          setShowModelPrompt(true);
          return buildModelError();
        }
      } catch {
        setShowModelPrompt(true);
        return buildModelError();
      }

      const {
        input = [],
        biz_params,
        session_id,
        logical_session_id,
        chat_id,
        turn_id,
      } = data;
      const session: SessionInfo = input[input.length - 1]?.session || {};
      const lastInput = input.slice(-1);
      const lastMsg = lastInput[0];
      const rewrittenInput =
        lastMsg?.content && Array.isArray(lastMsg.content)
          ? [
              {
                ...lastMsg,
                content: lastMsg.content.map(normalizeContentUrls),
              },
            ]
          : lastInput;

      const resolvedLogicalSessionId = resolveLogicalRequestSessionId(
        {
          session_id,
          logical_session_id,
          chat_id,
        },
        session,
      );

      const requestBody = {
        input: rewrittenInput,
        session_id: resolvedLogicalSessionId,
        turn_id,
        // ==================== userId 统一整改 (Kun He) ====================
        // 使用 getUserId()/getChannel() 获取，优先级：iframe > window > session > default
        user_id: getUserId(session?.user_id),
        channel: getChannel(session?.channel),
        // ==================== userId 统一整改结束 ====================
        stream: true,
        ...biz_params,
      };

      const backendChatId = resolveRequestChatId(
        {
          session_id,
          logical_session_id: resolvedLogicalSessionId,
          chat_id,
        },
        requestBody.session_id,
      );
      if (backendChatId) {
        const userText = rewrittenInput
          .filter((m: InputMessage) => m.role === "user")
          .map(extractUserMessageText)
          .join("\n")
          .trim();
        if (userText) {
          sessionApi.setLastUserMessage(backendChatId, userText);
        }
      }

      const timeoutSignal = createTimedAbortSignal(data.signal);
      try {
        const response = await fetch(getApiUrl("/console/chat"), {
          method: "POST",
          headers,
          body: JSON.stringify(requestBody),
          signal: timeoutSignal.signal,
        });

        return response;
      } catch (error) {
        if (shouldStopBackendForFetchAbort(error, timeoutSignal.signal)) {
          const backendChatId = resolveRequestChatId(
            {
              session_id: data.session_id,
              logical_session_id: data.logical_session_id,
              chat_id: data.chat_id,
            },
            requestBody.session_id,
          );
          if (backendChatId) {
            chatApi.stopChat(backendChatId).catch((err) => {
              console.error("Failed to stop chat after timeout:", err);
            });
          }
        }
        throw error;
      } finally {
        timeoutSignal.cleanup();
      }
    },
    [resolveLogicalRequestSessionId, resolveRequestChatId, selectedAgent],
  );

  const handleFileUpload = useCallback(
    async (options: {
      file: File;
      onSuccess: (body: { url?: string; thumbUrl?: string }) => void;
      onError?: (e: Error) => void;
      onProgress?: (e: { percent?: number }) => void;
    }) => {
      const { file, onSuccess, onError, onProgress } = options;
      try {
        // Warn when model has no multimodal support
        if (!multimodalCaps.supportsMultimodal) {
          message.warning(t("chat.attachments.multimodalWarning"));
        } else if (
          multimodalCaps.supportsImage &&
          !multimodalCaps.supportsVideo &&
          !file.type.startsWith("image/")
        ) {
          // Warn (not block) when only image is supported
          message.warning(t("chat.attachments.imageOnlyWarning"));
        }
        const sizeMb = file.size / 1024 / 1024;
        const isWithinLimit = sizeMb < CHAT_ATTACHMENT_MAX_MB;

        if (!isWithinLimit) {
          message.error(
            t("chat.attachments.fileSizeExceeded", {
              limit: CHAT_ATTACHMENT_MAX_MB,
              size: sizeMb.toFixed(2),
            }),
          );
          onError?.(new Error(`File size exceeds ${CHAT_ATTACHMENT_MAX_MB}MB`));
          return;
        }

        const res = await chatApi.uploadFile(file);
        onProgress?.({ percent: 100 });
        onSuccess({ url: chatApi.filePreviewUrl(res.url) });
      } catch (e) {
        onError?.(e instanceof Error ? e : new Error(String(e)));
      }
    },
    [multimodalCaps, t],
  );

  // ==================== Drag & drop file upload (Kun He) ====================
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes("Files")) {
      dragCounterRef.current += 1;
      if (dragCounterRef.current === 1) {
        setIsDragging(true);
      }
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current === 0) {
      setIsDragging(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDragging(false);

    const files = Array.from(e.dataTransfer.files);
    for (const file of files) {
      document.dispatchEvent(
        new CustomEvent("pasteFile", {
          detail: { file },
        }),
      );
    }
  }, []);

  const handleDragOverlayClose = useCallback(() => {
    dragCounterRef.current = 0;
    setIsDragging(false);
  }, []);
  // ==================== Drag & drop end ====================

  const options = useMemo(() => {
    const i18nConfig = getDefaultConfig(
      t,
    ) as unknown as Partial<IAgentScopeRuntimeWebUIOptions>;
    const commandSuggestions: CommandSuggestion[] = [
      {
        command: "/clear",
        value: "clear",
        description: t("chat.commands.clear.description"),
      },
      {
        command: "/compact",
        value: "compact",
        description: t("chat.commands.compact.description"),
      },
      {
        command: "/approve",
        value: "approve",
        description: t("chat.commands.approve.description"),
      },
      {
        command: "/deny",
        value: "deny",
        description: t("chat.commands.deny.description"),
      },
    ];

    const senderConfig = i18nConfig.sender as
      | IAgentScopeRuntimeWebUISenderOptions
      | undefined;

    const handleBeforeSubmit = async () => {
      if (isComposingRef.current) return false;
      return true;
    };

    return {
      ...i18nConfig,
      theme: {
        ...defaultConfig.theme,
        darkMode: isDark,
        leftHeader: {
          ...defaultConfig.theme.leftHeader,
        },
        rightHeader: (
          <>
            <ChatSessionInitializer />
            <RuntimeLoadingBridge bridgeRef={runtimeLoadingBridgeRef} />
            <ChatHeaderTitle />
            <span style={{ flex: 1 }} />
            <GeneratedFilesDrawer />
            <ModelSelector />
            {/* <ChatActionGroup /> */}
          </>
        ),
      },
      welcome: {
        ...i18nConfig.welcome,
        nick: brandTheme.brandName,
        // ==================== 品牌主题 (Kun He) ====================
        // 使用动态品牌 avatar
        avatar: brandTheme.avatar
          ? `${import.meta.env.BASE_URL}${brandTheme.avatar.replace(/^\//, "")}`
          : undefined,
        // ==================== 品牌主题结束 ====================
        // ==================== 首页改版 (Kun He) ====================
        // 使用自定义欢迎页渲染，替代默认 WelcomePrompts
        render: ({ greeting, onSubmit }) => (
          <WelcomeCenterLayout
            greeting={
              typeof greeting === "string" ? greeting : "你好，有什么可以帮您？"
            }
            onSubmit={(data) => onSubmit(data)}
          />
        ),
        // ==================== 首页改版结束 ====================
      },
      sender: {
        ...senderConfig,
        beforeSubmit: handleBeforeSubmit,
        beforeUI: <TaskProgressFloatingCard progress={taskProgress} />,
        allowSpeech: false,
        attachments: {
          trigger: function AttachmentTrigger(props: AttachmentTriggerProps) {
            const tooltipKey = multimodalCaps.supportsMultimodal
              ? multimodalCaps.supportsImage && !multimodalCaps.supportsVideo
                ? "chat.attachments.tooltipImageOnly"
                : "chat.attachments.tooltip"
              : "chat.attachments.tooltipNoMultimodal";
            return (
              <Tooltip title={t(tooltipKey, { limit: CHAT_ATTACHMENT_MAX_MB })}>
                <IconButton
                  disabled={props?.disabled}
                  icon={<SparkAttachmentLine />}
                  bordered={false}
                />
              </Tooltip>
            );
          },
          accept: "*/*",
          customRequest: handleFileUpload,
        },
        placeholder: t("chat.inputPlaceholder"),
        suggestions: commandSuggestions.map((item) => ({
          label: renderSuggestionLabel(item.command, item.description),
          value: item.value,
        })),
      },
      session: {
        multiple: true,
        hideBuiltInSessionList: true,
        api: sessionApi,
      },
      cards: {
        AgentScopeRuntimeRequestCard: (props: {
          data: ChatRuntimeRequestCardData;
        }) => <RuntimeRequestCard {...props} />,
        AgentScopeRuntimeResponseCard: (props: {
          data: ChatRuntimeResponseCardData;
          isLast?: boolean;
        }) => <RuntimeResponseCard {...props} />,
        ApprovalAction: (props: { data: ChatApprovalActionCardData }) => (
          <ApprovalActionCard {...props} />
        ),
        TaskRunGroupCard: (props: { data: ChatTaskRunGroupCardData }) => (
          <TaskRunGroupCard {...props} />
        ),
      },
      api: {
        ...defaultConfig.api,
        fetch: customFetch,
        replaceMediaURL: (url: string) => {
          return toDisplayUrl(url);
        },
        cancel(data: {
          session_id: string;
          logical_session_id?: string;
          chat_id?: string | null;
        }) {
          const logicalSessionId = resolveLogicalRequestSessionId(data);
          const chatId = resolveRequestChatId(data, logicalSessionId);
          if (chatId) {
            chatApi.stopChat(chatId).catch((err) => {
              console.error("Failed to stop chat:", err);
            });
          }
        },
        async reconnect(data: {
          session_id: string;
          signal?: AbortSignal;
          logical_session_id?: string;
          chat_id?: string | null;
        }) {
          const headers: Record<string, string> = {
            "Content-Type": "application/json",
            ...buildAuthHeaders(),
          };
          const logicalSessionId = resolveLogicalRequestSessionId(data);
          const reconnectChatId = resolveRequestChatId(data, logicalSessionId);

          const timeoutSignal = createTimedAbortSignal(data.signal);
          try {
            return await fetch(getApiUrl("/console/chat"), {
              method: "POST",
              headers,
              body: JSON.stringify({
                reconnect: true,
                session_id: logicalSessionId,
                chat_id: reconnectChatId,
                // ==================== userId 统一整改 (Kun He) ====================
                // 使用 getUserId()/getChannel() 获取
                user_id: getUserId(),
                channel: getChannel(),
                // ==================== userId 统一整改结束 ====================
              }),
              signal: timeoutSignal.signal,
            });
          } finally {
            timeoutSignal.cleanup();
          }
        },
      },
      // ==================== 自定义工具渲染器 ====================
      customToolRenderConfig: {
        copy_file_to_static: CopyFileToStatic,
      },
      // ==================== 自定义工具渲染器结束 ====================
      actions: {
        list: [
          {
            icon: (
              <span title={t("common.copy")}>
                <SparkCopyLine />
              </span>
            ),
            onClick: ({ data }: { data: CopyableResponse }) => {
              void copyResponse(data);
            },
          },
        ],
        replace: true,
      },
    } as unknown as IAgentScopeRuntimeWebUIOptions;
  }, [
    brandTheme.avatar,
    brandTheme.brandName,
    customFetch,
    copyResponse,
    handleFileUpload,
    isComposingRef,
    isDark,
    multimodalCaps,
    resolveLogicalRequestSessionId,
    resolveRequestChatId,
    taskProgress,
    t,
  ]);

  // ==================== 首页改版 (Kun He) ====================
  // 新建聊天：通过 chatRef 调用后端 createSession API
  const handleCreateSessionFromSidebar = useCallback(async () => {
    const newId = await chatRef.current?.createSession?.();
    if (newId) {
      navigate(`/chat/${newId}`, { replace: true });
    } else {
      navigate("/chat", { replace: true });
    }
  }, [navigate]);
  // ==================== 首页改版结束 ====================

  // 定义 cards 配置（与 AgentScopeRuntimeWebUI 内部一致）
  const cards = useMemo(() => {
    return {
      AgentScopeRuntimeRequestCard,
      AgentScopeRuntimeResponseCard,
      ...options.cards,
    };
  }, [options.cards]);

  return (
    <AgentScopeRuntimeWebUIComposedProvider options={options} cards={cards}>
      <div
        style={{
          height: "100%",
          width: "100%",
          display: "flex",
          flexDirection: "row",
        }}
      >
        {/* ==================== 首页改版 (Kun He) ==================== */}
        {/* 聊天专用侧栏：支持折叠为64px工具条 */}
        <ChatSidebar
          tasks={tasks}
          selectedTaskId={currentTask?.id}
          onCreateSession={handleCreateSessionFromSidebar}
          onTaskClick={handleTaskOpen}
          onTaskPause={handleTaskPause}
          onTaskRun={handleTaskRun}
          onTaskResume={handleTaskResume}
          onTaskDelete={handleTaskDelete}
        />
        {/* ==================== 首页改版结束 ==================== */}
        <div
          className={styles.chatMessagesArea}
          style={{ flex: 1, minWidth: 0, position: "relative" }}
          onDragEnter={handleDragEnter}
          onDragLeave={handleDragLeave}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
        >
          <AgentScopeRuntimeWebUILayout ref={chatRef} key={refreshKey} />
          <DragUploadOverlay
            visible={isDragging}
            onClose={handleDragOverlayClose}
          />
          <ConversationQuickNav />
        </div>

        <Modal
          open={showModelPrompt}
          closable={false}
          footer={null}
          width={480}
          styles={{
            content: isDark
              ? {
                  background: "#1f1f1f",
                  boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
                }
              : undefined,
          }}
        >
          <Result
            icon={<ExclamationCircleOutlined style={{ color: "#faad14" }} />}
            title={
              <span
                style={{ color: isDark ? "rgba(255,255,255,0.88)" : undefined }}
              >
                {t("modelConfig.promptTitle")}
              </span>
            }
            subTitle={
              <span
                style={{ color: isDark ? "rgba(255,255,255,0.55)" : undefined }}
              >
                {t("modelConfig.promptMessage")}
              </span>
            }
            extra={[
              <Button key="skip" onClick={() => setShowModelPrompt(false)}>
                {t("modelConfig.skipButton")}
              </Button>,
              <Button
                key="configure"
                type="primary"
                icon={<SettingOutlined />}
                onClick={() => {
                  setShowModelPrompt(false);
                  navigate("/models");
                }}
              >
                {t("modelConfig.configureButton")}
              </Button>,
            ]}
          />
        </Modal>
      </div>
    </AgentScopeRuntimeWebUIComposedProvider>
  );
}
