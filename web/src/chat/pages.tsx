/** The chat's routes, for the shell's router: `/` (new chat) and `/chat/:id`. */
import { useNavigate, useParams } from "react-router";

import { useDemoStory, useSession } from "../app/queries";
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
  // The walkthrough's message is the story's opening line, so only its protagonist sees it.
  const suggestion =
    story.data !== undefined && member !== null && story.data.acting_as.id === member.id
      ? story.data.suggested_message
      : null;

  return (
    <NewChat
      memberName={member?.name ?? null}
      suggestion={suggestion}
      sending={start.isPending}
      error={start.isError ? describeFailure(start.error) : null}
      onSend={(text) => {
        start.mutate(text, {
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
