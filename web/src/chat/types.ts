/**
 * The chat's named types, all derived from the generated OpenAPI types (`make gen-api`),
 * so a regenerated `schema.d.ts` flows through without hand edits.
 */
import type { components } from "../api/schema";
import type { EventOf } from "../api/types";

type Schemas = components["schemas"];

export type ConversationDetail = Schemas["ConversationDetail"];
export type ConversationSummary = Schemas["ConversationSummary"];
export type ConversationCreated = Schemas["ConversationCreated"];
export type StartConversation = Schemas["StartConversation"];
export type SendMode = Schemas["SendMode"];
export type MessagePosted = Schemas["MessagePosted"];

export type ChatItem = ConversationDetail["items"][number];
export type ChatItemType = ChatItem["type"];
export type ChatItemOf<T extends ChatItemType> = Extract<ChatItem, { type: T }>;

export type UserTextItem = Schemas["UserTextItem"];
export type AssistantTextItem = Schemas["AssistantTextItem"];
export type ThinkingSummaryItem = Schemas["ThinkingSummaryItem"];
export type ToolActivityItem = Schemas["ToolActivityItem"];
export type OutreachCardItem = Schemas["OutreachCardItem"];
/** How a rejected card was turned down: its Decline, or a message typed while it waited. */
export type CardRejectedWith = NonNullable<OutreachCardItem["rejected_with"]>;
export type RequestStatusItem = Schemas["RequestStatusItem"];
export type InlineQuestionItem = Schemas["InlineQuestionItem"];
export type QuestionAsker = Schemas["QuestionAsker"];
export type QuestionDelivery = Schemas["QuestionDelivery"];
export type CardStatus = OutreachCardItem["status"];

export type CardRecipient = Schemas["CardRecipient"];
export type ScreenedCounts = Schemas["ScreenedCounts"];
export type Fit = NonNullable<CardRecipient["fit"]>;
export type ApproveCard = Schemas["ApproveCard"];
export type CardResolved = Schemas["CardResolved"];
export type RejectCard = Schemas["RejectCard"];

export type RequestStatusView = Schemas["RequestStatusView"];
export type RecipientStatus = Schemas["RecipientStatus"];
export type ParticipantStatus = RecipientStatus["participant"];

export type ResolveEscalation = Schemas["ResolveEscalation"];
export type EscalationResolved = Schemas["EscalationResolved"];

export type OrgRef = Schemas["OrgRef"];
export type MemberRef = Schemas["MemberRef"];
export type DemoStory = Schemas["DemoStory"];
export type ApiProblem = Schemas["ApiProblem"];

/** `stream.chat_delta`: coalesced internal Claude output for one conversation. */
export type ChatDelta = EventOf<"stream.chat_delta">;
