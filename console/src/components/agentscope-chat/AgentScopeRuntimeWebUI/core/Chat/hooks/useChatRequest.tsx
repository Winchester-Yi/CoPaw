import { sleep, Stream } from "@/components/agentscope-chat";
import { useCallback, useRef, useEffect } from "react";
import { useChatAnywhereOptions } from "../../Context/ChatAnywhereOptionsContext";
import AgentScopeRuntimeResponseBuilder from "../../AgentScopeRuntime/Response/Builder";
import {
  AgentScopeRuntimeRunStatus,
  AgentScopeRuntimeMessageType,
} from "../../AgentScopeRuntime/types";
import { IAgentScopeRuntimeWebUIMessage } from "@/components/agentscope-chat";
import { IAgentScopeRuntimeWebUIInputData } from "../../types";
import { withResponseHeaderMeta } from "./headerMeta";
import type { CurrentQARef } from "./currentQARef";
import {
  emitTaskProgressUpdate,
  extractTaskProgress,
} from "@/pages/Chat/taskProgressEvents";
import {
  isActiveChatRequestOwner,
  type ChatRequestOwner,
} from "./requestOwnership";
import {
  createChatStreamAbortReason,
  isAbortLikeError,
} from "./abortReasons";

interface UseChatRequestOptions {
  currentQARef: CurrentQARef;
  updateMessage: (message: IAgentScopeRuntimeWebUIMessage) => void;
  hasMessage?: (id: string) => boolean;
  getCurrentSessionId: () => string;
  onFinish: (owner: ChatRequestOwner) => void;
}

function isTaskCancellationMessage(message: unknown) {
  return (
    typeof message === "string" &&
    /^task has been cancell?ed!?$/i.test(message.trim())
  );
}

function isTaskCancellationFrame(data: unknown) {
  if (!data || typeof data !== "object") {
    return false;
  }

  const frame = data as {
    code?: unknown;
    message?: unknown;
    error?: { code?: unknown; message?: unknown };
  };

  return (
    (frame.code === "AGENT_ERROR" &&
      isTaskCancellationMessage(frame.message)) ||
    (frame.error?.code === "AGENT_ERROR" &&
      isTaskCancellationMessage(frame.error.message))
  );
}

function getUserVisibleErrorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : typeof error === "string"
      ? error
      : JSON.stringify(error);
}

/**
 * 处理 API 请求和流式响应的 Hook
 */
export default function useChatRequest(options: UseChatRequestOptions) {
  const {
    currentQARef,
    updateMessage,
    hasMessage = () => true,
    getCurrentSessionId,
    onFinish,
  } =
    options;
  const apiOptions = useChatAnywhereOptions((v) => v.api);

  // 使用 ref 保存最新的 apiOptions，避免闭包陷阱
  const apiOptionsRef = useRef(apiOptions);

  useEffect(() => {
    apiOptionsRef.current = apiOptions;
  }, [apiOptions]);

  const getResponseHeaderTimestamp = useCallback(() => {
    return (
      currentQARef.current.response?.cards?.[0]?.data?.headerMeta?.timestamp ??
      currentQARef.current.response?.liveHeaderTimestamp
    );
  }, [currentQARef]);

  const failActiveResponse = useCallback(
    (owner: ChatRequestOwner, error: unknown) => {
      if (
        currentQARef.current.response?.id &&
        !hasMessage(currentQARef.current.response.id)
      ) {
        return;
      }

      const responseHeaderTimestamp = getResponseHeaderTimestamp();
      const responseData = currentQARef.current.response?.cards?.[0]?.data as
        | {
            id?: string;
            status?: AgentScopeRuntimeRunStatus;
            created_at?: number;
          }
        | undefined;
      const responseBuilder = new AgentScopeRuntimeResponseBuilder({
        id: responseData?.id || "",
        status: responseData?.status || AgentScopeRuntimeRunStatus.Created,
        created_at: responseData?.created_at || 0,
      });

      if (responseData) {
        responseBuilder.handle(responseData as never);
      }

      const errorMessage = getUserVisibleErrorMessage(error);

      const failed = responseBuilder.handle({
        object: "message",
        type: AgentScopeRuntimeMessageType.ERROR,
        content: [],
        id: "error",
        role: "assistant",
        status: AgentScopeRuntimeRunStatus.Failed,
        code: "stream_error",
        message: errorMessage,
      });

      if (currentQARef.current.response) {
        currentQARef.current.response.cards = [
          {
            code: "AgentScopeRuntimeResponseCard",
            data: withResponseHeaderMeta(failed, responseHeaderTimestamp),
          },
        ];
        updateMessage(currentQARef.current.response);
      }

      onFinish(owner);
    },
    [currentQARef, getResponseHeaderTimestamp, hasMessage, onFinish, updateMessage],
  );

  const mockRequest = useCallback(async (mockdata) => {
    const responseHeaderTimestamp = getResponseHeaderTimestamp();
    const agentScopeRuntimeResponseBuilder =
      new AgentScopeRuntimeResponseBuilder({
        id: "",
        status: AgentScopeRuntimeRunStatus.Created,
        created_at: 0,
      });

    for await (const chunk of mockdata) {
      const res = agentScopeRuntimeResponseBuilder.handle(chunk);
      currentQARef.current.response.cards = [
        {
          code: "AgentScopeRuntimeResponseCard",
          data: withResponseHeaderMeta(res, responseHeaderTimestamp),
        },
      ];

      updateMessage(currentQARef.current.response);

      await sleep(100);
    }
  }, []);

  const processSSEResponse = useCallback(
    async (response: Response, owner: ChatRequestOwner) => {
      const responseHeaderTimestamp = getResponseHeaderTimestamp();
      const isOwnerActive = () =>
        isActiveChatRequestOwner(currentQARef.current.activeRequestOwner, owner);
      const isLiveResponseMounted = () => {
        const responseId = currentQARef.current.response?.id;
        return Boolean(responseId && hasMessage(responseId));
      };
      const buildResponseCard = () => {
        const responseData = currentQARef.current.response?.cards?.[0]
          ?.data as
          | {
              id?: string;
              status?: AgentScopeRuntimeRunStatus;
              created_at?: number;
              output?: unknown[];
            }
          | undefined;

        const builder = new AgentScopeRuntimeResponseBuilder({
          id: responseData?.id || "",
          status: responseData?.status || AgentScopeRuntimeRunStatus.Created,
          created_at: responseData?.created_at || 0,
        });

        if (responseData) {
          builder.handle(responseData as never);
        }

        return builder;
      };

      const cancelActiveRequest = async () => {
        currentQARef.current.abortController?.abort(
          createChatStreamAbortReason("stop"),
        );

        const currentApiOptions = apiOptionsRef.current;
        if (currentApiOptions.cancel) {
          await Promise.resolve(
            currentApiOptions.cancel({
              session_id: owner.sessionId,
              logical_session_id: owner.logicalSessionId,
              chat_id: owner.chatId,
            }),
          ).catch((error) => {
            console.error(error);
          });
        }

        if (currentQARef.current.response) {
          currentQARef.current.response.cards = [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: withResponseHeaderMeta(
                buildResponseCard().cancel(),
                responseHeaderTimestamp,
              ),
            },
          ];

          updateMessage(currentQARef.current.response);
        }
      };

      const agentScopeRuntimeResponseBuilder = buildResponseCard();

      if (!response.ok) {
        response.json().then((data) => {
          const res = agentScopeRuntimeResponseBuilder.handle({
            object: "message",
            type: AgentScopeRuntimeMessageType.ERROR,
            content: [],
            id: "error",
            role: "assistant",
            status: AgentScopeRuntimeRunStatus.Failed,
            code: String(response.status),
            message: JSON.stringify(data),
          });

          currentQARef.current.response.cards = [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: withResponseHeaderMeta(res, responseHeaderTimestamp),
            },
          ];
          onFinish(owner);
        });
        return;
      }

      // 辅助函数：从 chunkData 中提取 approval_action
      // 后端将 msg.metadata 嵌套在 message.metadata.metadata 中
      const extractApprovalAction = (data: any): any | null => {
        if (!data || typeof data !== "object") return null;

        // 获取 metadata 对象
        const getMetadata = (obj: any): any | null => {
          if (!obj || typeof obj !== "object") return null;
          return obj.metadata;
        };

        const metadata = getMetadata(data);

        if (metadata && typeof metadata === "object") {
          // 路径1: metadata.approval_action (直接)
          const directAction = (metadata as Record<string, unknown>).approval_action;
          if (directAction && typeof directAction === "object") {
            return directAction;
          }

          // 路径2: metadata.metadata.approval_action (嵌套)
          const nestedMetadata = (metadata as Record<string, unknown>).metadata;
          if (nestedMetadata && typeof nestedMetadata === "object") {
            const nestedAction = (nestedMetadata as Record<string, unknown>).approval_action;
            if (nestedAction && typeof nestedAction === "object") {
              return nestedAction;
            }
          }
        }

        // 在 output 数组中查找
        if (Array.isArray(data.output)) {
          for (const msg of data.output) {
            const msgMetadata = getMetadata(msg);
            if (msgMetadata && typeof msgMetadata === "object") {
              const directAction = (msgMetadata as Record<string, unknown>).approval_action;
              if (directAction && typeof directAction === "object") {
                return directAction;
              }

              const nestedMetadata = (msgMetadata as Record<string, unknown>).metadata;
              if (nestedMetadata && typeof nestedMetadata === "object") {
                const nestedAction = (nestedMetadata as Record<string, unknown>).approval_action;
                if (nestedAction && typeof nestedAction === "object") {
                  return nestedAction;
                }
              }
            }
          }
        }

        return null;
      };

      try {
        for await (const chunk of Stream({
          readableStream: response.body,
        })) {
          if (!isOwnerActive()) {
            return;
          }

          if (currentQARef.current.response?.msgStatus === "interrupted") {
            await cancelActiveRequest();
            break;
          }

          const responseParser =
            apiOptionsRef.current.responseParser || JSON.parse;
          const chunkData = responseParser(chunk.data);
          if (isTaskCancellationFrame(chunkData)) {
            emitTaskProgressUpdate(null);
            onFinish(owner);
            return;
          }

          const streamedTaskProgress = extractTaskProgress(chunkData);
          if (streamedTaskProgress !== undefined) {
            emitTaskProgressUpdate(streamedTaskProgress);
          }
          const res = agentScopeRuntimeResponseBuilder.handle(chunkData);
          const isTerminalResponse =
            res.status === AgentScopeRuntimeRunStatus.Completed ||
            res.status === AgentScopeRuntimeRunStatus.Failed;
          const hasRenderableOutput = Boolean(
            res.output?.some((message) => message.content?.length),
          );

          // A terminal response frame may legitimately advance only status
          // while leaving output empty. It must still finish the request.
          if (!isTerminalResponse && !hasRenderableOutput) {
            continue;
          }

          if (
            currentQARef.current.response &&
            isOwnerActive() &&
            isLiveResponseMounted()
          ) {
            const cards: any[] = [
              {
                code: "AgentScopeRuntimeResponseCard",
                data: withResponseHeaderMeta(res, responseHeaderTimestamp),
              },
            ];

            // 检测 approval_action metadata，额外创建审批卡片
            const approvalAction =
              extractApprovalAction(chunkData) || extractApprovalAction(res);
            if (approvalAction) {
              cards.push({
                code: "ApprovalAction",
                data: approvalAction,
              });
            }

            currentQARef.current.response.cards = cards;

            if (
              res.status === AgentScopeRuntimeRunStatus.Completed ||
              res.status === AgentScopeRuntimeRunStatus.Failed
            ) {
              emitTaskProgressUpdate(null);
              onFinish(owner);
            } else {
              updateMessage(currentQARef.current.response);
            }
          }
        }
      } catch (error) {
        console.error(error);
        if (!isOwnerActive()) {
          return;
        }
        if (
          currentQARef.current.response?.msgStatus === "interrupted" ||
          isAbortLikeError(error)
        ) {
          onFinish(owner);
          return;
        }
        failActiveResponse(owner, error);
      }
    },
    [
      currentQARef,
      failActiveResponse,
      getCurrentSessionId,
      getResponseHeaderTimestamp,
      hasMessage,
      onFinish,
      updateMessage,
    ],
  );

  const request = useCallback(
    async (
      historyMessages: any[],
      biz_params?: IAgentScopeRuntimeWebUIInputData["biz_params"],
      owner?: ChatRequestOwner,
    ) => {
      const requestOwner = owner ?? currentQARef.current.activeRequestOwner;
      if (!requestOwner) {
        return;
      }

      const currentApiOptions = apiOptionsRef.current;
      const { enableHistoryMessages = false } = currentApiOptions;
      const abortSignal = currentQARef.current.abortController?.signal;
      let response;
      try {
        response = currentApiOptions.fetch
          ? await currentApiOptions.fetch({
              input: historyMessages,
              biz_params,
              signal: abortSignal,
              session_id: requestOwner.sessionId,
              logical_session_id: requestOwner.logicalSessionId,
              chat_id: requestOwner.chatId,
              turn_id: currentQARef.current.response?.id,
            })
          : await fetch(currentApiOptions.baseURL, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${currentApiOptions.token || ""}`,
              },
              body: JSON.stringify({
                input: enableHistoryMessages
                  ? historyMessages
                  : historyMessages.slice(-1),
                session_id: getCurrentSessionId(),
                stream: true,
                biz_params,
                ...biz_params,
              }),
              signal: abortSignal,
            });
      } catch (error) {
        if (
          !isAbortLikeError(error) &&
          isActiveChatRequestOwner(currentQARef.current.activeRequestOwner, requestOwner)
        ) {
          failActiveResponse(requestOwner, error);
        }
        return;
      }

      if (response && response.body) {
        await processSSEResponse(response, requestOwner);
      }
    },
    [currentQARef, failActiveResponse, getCurrentSessionId, processSSEResponse],
  );

  const reconnect = useCallback(
    async (sessionId: string, owner?: ChatRequestOwner) => {
      const requestOwner = owner ?? currentQARef.current.activeRequestOwner;
      if (!requestOwner) {
        return;
      }

      const currentApiOptions = apiOptionsRef.current;
      if (!currentApiOptions.reconnect) return;

      const abortSignal = currentQARef.current.abortController?.signal;
      let response: Response | undefined;
      try {
        response = await currentApiOptions.reconnect({
          session_id: sessionId,
          signal: abortSignal,
          logical_session_id: requestOwner.logicalSessionId,
          chat_id: requestOwner.chatId,
        });
      } catch (error) {
        if (
          !isAbortLikeError(error) &&
          isActiveChatRequestOwner(currentQARef.current.activeRequestOwner, requestOwner)
        ) {
          failActiveResponse(requestOwner, error);
        }
        return;
      }

      if (response && response.body) {
        await processSSEResponse(response, requestOwner);
      }
    },
    [currentQARef, failActiveResponse, processSSEResponse],
  );

  const cancelActiveRequest = useCallback(async () => {
    const responseHeaderTimestamp = getResponseHeaderTimestamp();
    const responseData = currentQARef.current.response?.cards?.[0]?.data as
      | {
          id?: string;
          status?: AgentScopeRuntimeRunStatus;
          created_at?: number;
        }
      | undefined;
    const responseBuilder = new AgentScopeRuntimeResponseBuilder({
      id: responseData?.id || "",
      status: responseData?.status || AgentScopeRuntimeRunStatus.Created,
      created_at: responseData?.created_at || 0,
    });

    if (responseData) {
      responseBuilder.handle(responseData as never);
    }

    currentQARef.current.abortController?.abort(
      createChatStreamAbortReason("stop"),
    );

    const currentApiOptions = apiOptionsRef.current;
    const activeOwner = currentQARef.current.activeRequestOwner;
    const activeSessionId = activeOwner?.sessionId ?? getCurrentSessionId();
    if (currentApiOptions.cancel) {
      await Promise.resolve(
        currentApiOptions.cancel({
          session_id: activeSessionId,
          logical_session_id: activeOwner?.logicalSessionId,
          chat_id: activeOwner?.chatId,
        }),
      ).catch((error) => {
        console.error(error);
      });
    }

    if (currentQARef.current.response) {
      currentQARef.current.response.cards = [
        {
          code: "AgentScopeRuntimeResponseCard",
          data: withResponseHeaderMeta(
            responseBuilder.cancel(),
            responseHeaderTimestamp,
          ),
        },
      ];

      updateMessage(currentQARef.current.response);
    }

    emitTaskProgressUpdate(null);
  }, [currentQARef, getCurrentSessionId, getResponseHeaderTimestamp, updateMessage]);

  return { request, reconnect, mockRequest, cancelActiveRequest };
}
