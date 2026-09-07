import type { IAgentScopeRuntimeWebUIMessage } from "@/components/agentscope-chat";

const READONLY_CARD_CODES = new Set([
  "AgentScopeRuntimeRequestCard",
  "AgentScopeRuntimeResponseCard",
  "ApprovalAction",
  "PlanInteraction",
  "TaskRunGroupCard",
  "ResponseFeedback",
  "WPlusSopEntryProposal",
  "ConversationCompactionBoundary",
  "ReadOnlyStructuredCard",
]);

export function prepareShareMessages(
  convertedMessages: IAgentScopeRuntimeWebUIMessage[],
): IAgentScopeRuntimeWebUIMessage[] {
  return convertedMessages.map((item) => ({
    ...item,
    cards: item.cards?.map((card) =>
      READONLY_CARD_CODES.has(card.code)
        ? card
        : {
            ...card,
            code: "ReadOnlyStructuredCard",
            data: { code: card.code, data: card.data },
          },
    ),
  })) as IAgentScopeRuntimeWebUIMessage[];
}
