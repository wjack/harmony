/** The chat's routes, for the shell's router: `/` (new chat) and `/chat/:id`. */
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import { useDemoStory, useSession } from "../app/queries";
import { useHarmonySettings, useSettingsLive } from "../customize/api";
import { prefilledSuggestion, useDemoState } from "../demo/store";
import { useUnavailable } from "../shell/unavailable";
import { useStartConversation } from "./api";
import { ChatScreen } from "./ChatScreen";
import { NewChat } from "./NewChat";
import { describeFailure } from "./problem";

export function NewChatPage() {
  const session = useSession();
  const story = useDemoStory();
  const start = useStartConversation();
  const navigate = useNavigate();
  const unavailable = useUnavailable();
  const member = session.data?.member ?? null;
  // The new chat's send mode: the organization's default until the member picks one,
  // which the chat then keeps (LIVE_DEMO_PLAN 3.10).
  const [override, setOverride] = useState<boolean | null>(null);
  const orgSlug = member?.org.slug ?? null;
  const settings = useHarmonySettings(orgSlug);
  useSettingsLive(orgSlug);
  // Known once loaded: a failed refetch keeps the last one.
  const orgDefault = settings.data?.send_without_confirmation ?? null;
  // Until the default is known, a chat that follows it can't say how it will send.
  const unknownMode = override === null && orgDefault === null;
  // The walkthrough's message is the story's opening line, so only its protagonist sees it.
  const suggestion =
    story.data !== undefined && member !== null && story.data.acting_as.id === member.id
      ? story.data.suggested_message
      : null;
  // A first visit ends with the suggestion waiting in the composer (demo/Intro.tsx).
  const prefill = useDemoState((state) => state.prefillSuggestion) ? suggestion : null;

  return (
    <NewChat
      memberName={member?.name ?? null}
      suggestion={suggestion}
      prefill={prefill}
      onPrefilled={prefilledSuggestion}
      sending={start.isPending || unknownMode}
      error={start.isError ? describeFailure(start.error) : null}
      sendMode={{ override, org_default: orgDefault, effective: override ?? orgDefault ?? false }}
      onSendModeChange={setOverride}
      onRetrySendMode={
        unknownMode && settings.isError
          ? () => {
              void settings.refetch();
            }
          : null
      }
      onSend={(text) => {
        start.mutate(override === null ? { text } : { text, send_without_confirmation: override }, {
          onSuccess: (created) => {
            void navigate(`/chat/${created.id}`);
          },
        });
      }}
      onUnavailable={unavailable}
    />
  );
}

export function ChatPage() {
  const { id } = useParams();
  if (id === undefined) return null;
  // Keyed, so switching chats starts from a clean stream buffer, scroll and draft.
  return <ChatScreen key={id} conversationId={id} />;
}
