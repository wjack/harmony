/**
 * `GET /api/demo/whose` fixtures for the not-found pages (switching.html): Alex's chat,
 * a Henderson–Ridgeline request, and ids from an earlier demo, which it answers 404.
 */
import type { DemoWhose, EventEnvelope, MemberView, SessionView } from "../api/types";
import { problem, type FixtureRoutes } from "../fixtures";
import { conversations, FIXTURE_NOW, members, orgs } from "../fixtures/shell";

/** Alex's chat (the first of his Recents). */
export const ALEX_CHAT = conversations[0]?.id ?? "";
/** A request between Henderson and Ridgeline, which Helen can't open. */
export const OTHERS_THREAD = "0b1d7a3e-0000-4000-8000-0000000003c1";
/** An id from before a reset. */
export const EARLIER_ID = "0b1d7a3e-0000-4000-8000-0000000003ff";

const person = ({ id, name }: MemberView) => ({ member_id: id, name });

export const whose = {
  alexChat: {
    kind: "chat",
    orgs: [{ org: orgs.henderson, people: [person(members.alex)] }],
  },
  othersThread: {
    kind: "thread",
    orgs: [
      { org: orgs.henderson, people: [person(members.alex), person(members.maya)] },
      { org: orgs.ridgeline, people: [person(members.dana)] },
    ],
  },
} satisfies Record<string, DemoWhose>;

export const whoseRoutes: FixtureRoutes = {
  "GET /api/demo/whose": ({ query }) => {
    if (query.get("chat") === ALEX_CHAT) return whose.alexChat;
    if (query.get("thread") === OTHERS_THREAD) return whose.othersThread;
    return problem(404, "not_found", "not found");
  },
};

/** `session.member_changed` as the stream delivers it: a switch made in another tab. */
export function memberChangedEnvelope(seq: number, session: SessionView): EventEnvelope {
  return {
    world_id: session.world_id,
    seq,
    created_at: FIXTURE_NOW,
    scope: { kind: "session", org_id: null, member_id: null, thread_id: null, session_id: session.session_id },
    event: {
      type: "session.member_changed",
      session_id: session.session_id,
      member_id: session.member.id,
      epoch: session.epoch,
    },
  };
}
