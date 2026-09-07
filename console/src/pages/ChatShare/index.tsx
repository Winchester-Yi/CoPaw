import { useEffect, useMemo, useState } from "react";
import { Alert, Empty, Spin } from "antd";
import {
  Bubble,
  AgentScopeRuntimeWebUIComposedProvider,
  type IAgentScopeRuntimeWebUIOptions,
} from "@/components/agentscope-chat";
import { useParams } from "react-router-dom";
import { chatApi } from "@/api/modules/chat";
import type { ChatShareSnapshot } from "@/api/types/chat";
import { convertMessages } from "../Chat/sessionApi";
import RuntimeRequestCard from "../Chat/components/RuntimeRequestCard";
import AgentScopeRuntimeResponseCard from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Card";
import type {
  ChatRuntimeRequestCardData,
  ChatRuntimeResponseCardData,
  ChatPlanInteractionCardData,
  ChatPlanClarificationCardData,
  ChatGoalProposalCardData,
} from "../Chat/messageMeta";
import { PlanReviewSnapshot } from "../Chat/components/PlanInteractionCards";
import planStyles from "../Chat/components/PlanInteractionCards.module.less";
import { HtmlPreviewTrackingProvider } from "@/components/agentscope-chat/HtmlPreviewTrackingContext";
import { prepareShareMessages } from "./shareView";
import styles from "./index.module.less";

const READONLY_OPTIONS = {
  theme: { locale: "zh-CN", bubbleList: { pagination: false } },
  session: {
    multiple: false,
    api: {
      getSessionList: async () => [],
      getSession: async () => ({ id: "", name: "", messages: [] }),
      createSession: async () => [],
      updateSession: async () => [],
      removeSession: async () => [],
    },
  },
  actions: { list: [], replace: false },
  api: { replaceMediaURL: (url: string) => url },
} as unknown as IAgentScopeRuntimeWebUIOptions;

function serializeCardData(data: unknown): string {
  if (typeof data === "string") return data;
  if (data === undefined) return "";
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

export function ReadOnlyStructuredCard(props: {
  code?: string;
  data?: unknown;
}) {
  const wrappedData =
    props.code === "ReadOnlyStructuredCard" &&
    props.data &&
    typeof props.data === "object" &&
    "code" in props.data &&
    "data" in props.data
      ? (props.data as { code?: unknown; data?: unknown })
      : null;
  const code =
    typeof wrappedData?.code === "string"
      ? wrappedData.code
      : props.code || "会话记录";
  const payload = wrappedData ? wrappedData.data : props.data;
  const serialized = serializeCardData(payload);

  return (
    <section className={styles.structuredCard}>
      <strong>{code}</strong>
      <span>该内容仅供查看，分享页不支持交互操作。</span>
      {serialized ? (
        <pre data-testid="readonly-structured-card-payload">{serialized}</pre>
      ) : null}
    </section>
  );
}

function ReadOnlyPlanClarificationCard({
  data,
}: {
  data: ChatPlanClarificationCardData;
}) {
  const fields = data.kind === "form" ? data.fields || [] : [];
  const options = data.options || [];
  return (
    <section className={planStyles.planClarificationCard}>
      <header className={planStyles.cardHeader}>
        <div className={planStyles.cardHeading}>
          <strong>{data.prompt}</strong>
          <span>只读</span>
        </div>
      </header>
      {data.kind !== "form" && options.length > 0 ? (
        <div className={planStyles.choiceOptionsViewport}>
          {options.map((option, index) => (
            <div className={planStyles.optionRow} key={option.id}>
              <span className={planStyles.optionNumber}>{index + 1}.</span>
              <span className={planStyles.optionLabel}>{option.label}</span>
            </div>
          ))}
        </div>
      ) : null}
      {fields.map((field) => (
        <section className={planStyles.reviewSection} key={field.id}>
          <h4>{field.label}</h4>
          {field.description ? (
            <p className={planStyles.reviewStatus}>{field.description}</p>
          ) : null}
          {field.options?.map((option, index) => (
            <div className={planStyles.optionRow} key={option.id}>
              <span className={planStyles.optionNumber}>{index + 1}.</span>
              <span className={planStyles.optionLabel}>{option.label}</span>
            </div>
          ))}
        </section>
      ))}
    </section>
  );
}

export function ReadOnlyGoalProposalCard({
  data,
}: {
  data: ChatGoalProposalCardData;
}) {
  return (
    <section
      className={`${planStyles.goalProposalCard} ${styles.readOnlyGoalProposalCard}`}
      data-testid="readonly-goal-proposal-card"
      data-scrollable="true"
    >
      <header className={planStyles.cardHeader}>
        <div className={planStyles.cardHeading}>
          <strong>Goal 合同草案</strong>
          <span>只读</span>
        </div>
      </header>
      <p className={planStyles.reviewSummary}>{data.objective}</p>
      <section className={planStyles.reviewSection}>
        <h4>完成标准</h4>
        <ul>
          {data.completion_criteria.map((item, index) => (
            <li key={`${item.requirement}-${index}`}>
              <strong>{item.requirement}</strong>
              <div>可观察断言：{item.observable_assertion}</div>
              <div>验证方式：{item.verification_method}</div>
              <div>预期结果：{item.expected_outcome}</div>
            </li>
          ))}
        </ul>
      </section>
      <section className={planStyles.reviewSection}>
        <h4>必须保留</h4>
        <ul>
          {data.constraints.must_preserve.map((item, index) => (
            <li key={`preserve-${index}`}>{item}</li>
          ))}
        </ul>
      </section>
      <section className={planStyles.reviewSection}>
        <h4>禁止操作</h4>
        <ul>
          {data.constraints.must_not_do.map((item, index) => (
            <li key={`must-not-do-${index}`}>{item}</li>
          ))}
        </ul>
      </section>
      <section className={planStyles.reviewSection}>
        <h4>约束与边界</h4>
        <p className={planStyles.reviewFeedbackSummary}>
          {data.autonomy_boundary}
        </p>
      </section>
    </section>
  );
}

function ReadOnlyPlanInteractionCard({
  data,
}: {
  data: ChatPlanInteractionCardData;
}) {
  if (data.card_type === "plan_review") {
    return <PlanReviewSnapshot data={data} />;
  }
  if (data.card_type === "plan_clarification") {
    return <ReadOnlyPlanClarificationCard data={data} />;
  }
  return <ReadOnlyGoalProposalCard data={data} />;
}

function ReadOnlyResponseCard(props: {
  data: ChatRuntimeResponseCardData;
  isLast?: boolean;
}) {
  const data = useMemo(
    () => ({ ...props.data, suggestions: [] }),
    [props.data],
  );
  return <AgentScopeRuntimeResponseCard data={data} isLast={false} />;
}

const READONLY_CARDS = {
  AgentScopeRuntimeRequestCard: (props: { data: ChatRuntimeRequestCardData }) => (
    <RuntimeRequestCard {...props} />
  ),
  AgentScopeRuntimeResponseCard: ReadOnlyResponseCard,
  ApprovalAction: (props: { data: unknown }) => (
    <ReadOnlyStructuredCard code="审批记录" data={props.data} />
  ),
  PlanInteraction: (props: { data: ChatPlanInteractionCardData }) => (
    <ReadOnlyPlanInteractionCard data={props.data} />
  ),
  TaskRunGroupCard: (props: { data: unknown }) => (
    <ReadOnlyStructuredCard code="任务记录" data={props.data} />
  ),
  ResponseFeedback: () => null,
  WPlusSopEntryProposal: (props: { data: unknown }) => (
    <ReadOnlyStructuredCard code="SOP 记录" data={props.data} />
  ),
  ConversationCompactionBoundary: (props: { data: unknown }) => (
    <ReadOnlyStructuredCard code="会话归档边界" data={props.data} />
  ),
  ReadOnlyStructuredCard,
};

export default function ChatSharePage() {
  const { token = "" } = useParams<{ token: string }>();
  const [snapshot, setSnapshot] = useState<ChatShareSnapshot | null>(null);
  const [error, setError] = useState<"not-found" | "unavailable" | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) {
      setError("not-found");
      setLoading(false);
      return;
    }
    void chatApi.getChatShare(token).then(setSnapshot).catch((reason) => {
      setError((reason as { status?: number })?.status === 404 ? "not-found" : "unavailable");
    }).finally(() => setLoading(false));
  }, [token]);

  if (loading) return <div className={styles.state}><Spin /></div>;
  if (error === "not-found") return <div className={styles.state}><Alert type="error" message="分享不存在" /></div>;
  if (error) return <div className={styles.state}><Alert type="error" message="分享服务暂不可用" /></div>;
  if (!snapshot) return <div className={styles.state}><Empty description="暂无分享内容" /></div>;

  const messages = prepareShareMessages(convertMessages(snapshot.messages || []));

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1>{snapshot.chat_name || "分享的会话"}</h1>
        <span>只读分享</span>
      </header>
      {messages.length === 0 ? (
        <Empty description="暂无分享内容" />
      ) : (
        <HtmlPreviewTrackingProvider value={{ disableEventRecording: true }}>
          <AgentScopeRuntimeWebUIComposedProvider
            options={READONLY_OPTIONS}
            cards={READONLY_CARDS}
          >
            <div
              className={styles.messageViewport}
              data-testid="share-message-viewport"
            >
              <Bubble.List
                classNames={{ wrapper: styles.messageList }}
                items={messages}
                order="asc"
                pagination={false}
              />
            </div>
          </AgentScopeRuntimeWebUIComposedProvider>
        </HtmlPreviewTrackingProvider>
      )}
    </main>
  );
}
