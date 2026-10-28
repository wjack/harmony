/** The network view's gallery sections (/__gallery), over fixtures/network.ts. */
import { useEffect, useState } from "react";

import type { EventEnvelope, MemberView } from "../api/types";
import type { BudgetView } from "../app/budget";
import { useSession } from "../app/queries";
import { networkRoutes, typingThreads, type NetworkStage } from "../fixtures/network";
import { WORLD_ID, budgets, members, orgs } from "../fixtures/shell";
import { useFixtureEmit } from "../gallery/fixtureContext";
import type { GallerySection } from "../gallery/types";
import type { EventSourceLike } from "../realtime/dispatcher";
import { ShellFrame } from "../shell/AppLayout";
import { LensSourceContext, type OpenLensSource } from "./lens";
import { NetworkScreen } from "./NetworkPage";
import { StatusPill } from "./StatusPill";
import type { ParticipantStatus, WorldLens } from "./types";

const TYPING_EVERY_MS = 900;
const WORLD_SCOPE: EventEnvelope["scope"] = {
  kind: "world",
  org_id: null,
  member_id: null,
  thread_id: null,
  session_id: null,
};

/**
 * Every 900 ms, a typing frame for each of `threadIds`, and now and then a committed
 * message on one of them (a pulse). Returns a stop function.
 */
function scriptTyping(threadIds: readonly string[], deliver: (envelope: EventEnvelope) => void): () => void {
  let tick = 0;
  const send = (event: EventEnvelope["event"]) => {
    deliver({ world_id: WORLD_ID, seq: null, created_at: null, scope: WORLD_SCOPE, event });
  };
  const timer = setInterval(() => {
    tick += 1;
    threadIds.forEach((threadId, index) => {
      // Stagger the threads so they don't animate in lockstep.
      const phase = (tick + index * 3) % 6;
      if (phase === 5) {
        send({
          type: "entity.snapshot",
          entity_type: "thread_message",
          entity_id: `${threadId}:${String(tick)}`,
          version: 1,
          data: { thread_id: threadId },
        });
      } else {
        send({ type: "stream.typing", org_id: "", thread_id: threadId, turn_id: `${threadId}:${String(tick)}` });
      }
    });
  }, TYPING_EVERY_MS);
  return () => {
    clearInterval(timer);
  };
}

/** The big run's lens stream, scripted: it never touches the network. */
function scriptedLens(threadIds: readonly string[]): OpenLensSource {
  return () => {
    const source: EventSourceLike = {
      readyState: 1,
      onopen: null,
      onmessage: null,
      onerror: null,
      close: () => {
        stop();
      },
    };
    const stop = scriptTyping(threadIds, (envelope) => {
      source.onmessage?.(new MessageEvent("message", { data: JSON.stringify(envelope) }));
    });
    return source;
  };
}

/** The desk world's typing arrives on the tab's own stream: emit it there. */
function DeskTyping({ threadIds }: { threadIds: readonly string[] }) {
  const emit = useFixtureEmit();
  useEffect(() => scriptTyping(threadIds, emit), [threadIds, emit]);
  return null;
}

function Screen({
  initialWorld = "desk",
  initialSelected,
  typing = [],
}: {
  initialWorld?: WorldLens;
  initialSelected?: string;
  typing?: readonly string[];
}) {
  const session = useSession();
  const [world, setWorld] = useState<WorldLens>(initialWorld);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [lens] = useState(() => scriptedLens(typingThreads.bigRun));
  return (
    <ShellFrame session={session.data ?? null}>
      <LensSourceContext value={lens}>
        {world === "desk" && <DeskTyping threadIds={typing} />}
        <NetworkScreen
          world={world}
          requestId={requestId}
          onWorldChange={(next) => {
            setWorld(next);
            setRequestId(null);
          }}
          onRequestChange={setRequestId}
          initialSelectedOrgId={initialSelected}
        />
      </LensSourceContext>
    </ShellFrame>
  );
}

function page(
  title: string,
  description: string,
  stage: NetworkStage,
  options: {
    world?: WorldLens;
    selected?: string;
    typing?: readonly string[];
    bigRun?: boolean;
    actingAs?: MemberView;
    budget?: BudgetView;
  } = {},
): GallerySection {
  const routes = networkRoutes(stage, { bigRun: options.bigRun ?? true, actingAs: options.actingAs });
  return {
    title,
    description,
    layout: "page",
    height: 720,
    fixtures: options.budget === undefined ? routes : { ...routes, "GET /api/budget": options.budget },
    render: () => (
      <Screen initialWorld={options.world} initialSelected={options.selected} typing={options.typing} />
    ),
  };
}

const STATUSES: readonly ParticipantStatus[] = ["screened", "messaged", "replied", "needs_human", "closed"];

function StatusPills() {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {STATUSES.map((status) => (
          <StatusPill key={status} status={status} />
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {STATUSES.map((status) => (
          <StatusPill key={status} status={status} size="sm" />
        ))}
      </div>
    </div>
  );
}

export default [
  page(
    "Network view: Northlake needs a human",
    "As Alex: who Northlake asked is Northlake's to know, so the panel suggests acting as its admin, Helen.",
    "needs_human",
    { selected: orgs.northlake.id },
  ),
  page(
    "Network view: Ridgeline's question waits on you",
    "As Alex: Henderson's own representative asked him about the tube; he answers in the chat.",
    "question",
    { selected: orgs.ridgeline.id, typing: typingThreads.question },
  ),
  page(
    "Network view: as Helen",
    "Helen sees Northlake's wait as hers, and nothing of whom else Henderson asked.",
    "needs_human",
    { selected: orgs.northlake.id, actingAs: members.helen },
  ),
  page(
    "Network view: representatives messaging",
    "Right after Alex approves: Northlake's thread is open and moving, Ridgeline's opens next.",
    "messaging",
    { typing: typingThreads.messaging },
  ),
  page(
    "Network view: before a request",
    "The story's suggested recipients are ringed; the rest of the network waits below.",
    "before",
  ),
  page(
    "Network view: a thread stopped on the budget",
    "Today's live budget ran out before Ridgeline replied: its edge and panel say Closed, and why, with no summary.",
    "stopped",
    { selected: orgs.ridgeline.id, budget: budgets.dailyExhausted },
  ),
  page(
    "Network view: closed",
    "Both threads closed with Henderson's own summaries; Calder stays screened.",
    "closed",
    { selected: orgs.northlake.id },
  ),
  page(
    "Network view: the big run",
    "The network world, through the demo lens: one request fanned out to the network, and no walkthrough rings.",
    "messaging",
    { world: "network" },
  ),
  page(
    "Network view: big run not started",
    "The session has no network world yet.",
    "messaging",
    { world: "network", bigRun: false },
  ),
  { title: "Status pills", render: () => <StatusPills /> },
] satisfies GallerySection[];
