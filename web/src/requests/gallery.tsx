/** Requests' gallery sections (/__gallery), over fixtures/requests.ts. */
import { useRef, useState } from "react";

import { useSession } from "../app/queries";
import {
  requestsRoutes,
  requestsWorld,
  routedToBoth,
  stopped,
  THREAD,
  waitingOnThem,
  type RequestsRouteOptions,
  type RequestsWorld,
} from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import type { GallerySection } from "../gallery/types";
import { ShellFrame } from "../shell/AppLayout";
import { Button } from "../shell/ui";
import { RepresentativeSaw } from "./RepresentativeSaw";
import { RequestsPage } from "./RequestsPage";

/** The page in the app's frame, with the sidebar's requests beside it. */
function InApp() {
  const session = useSession();
  return (
    <ShellFrame session={session.data ?? null}>
      <RequestsPage />
    </ShellFrame>
  );
}

/** The drawer over Pacific Beamline's thread: a trusted organization's extra and an automatic release. */
function SawDrawer() {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement | null>(null);
  const representative = requestsWorld().threads.get(THREAD.pacific)?.workspace.representative ?? null;
  return (
    <>
      <Button
        ref={opener}
        onClick={() => {
          setOpen(true);
        }}
      >
        Open “What our representative saw”
      </Button>
      <RepresentativeSaw
        open={open}
        onOpenChange={setOpen}
        us={orgs.northlake}
        them={orgs.pacific}
        representative={representative}
        returnFocus={() => {
          opener.current?.focus();
        }}
      />
    </>
  );
}

const pending = () => new Promise<never>(() => undefined);

/** Each section runs in its own memory router, at the app's own route. */
const PATH = "/requests/:threadId?";
const at = (threadId?: string) => ({ path: PATH, url: threadId === undefined ? "/requests" : `/requests/${threadId}` });
const emptyWorld: RequestsWorld = { threads: new Map() };

/** A section of the view in the app's frame, over its own world. */
function view(
  title: string,
  threadId: string,
  description: string,
  options: RequestsRouteOptions = {},
  height = 860,
): GallerySection {
  return {
    title,
    description,
    layout: "page",
    height,
    fixtures: requestsRoutes(options),
    ...at(threadId),
    render: () => <InApp />,
  };
}

export default [
  view(
    "A request, in the app",
    THREAD.henderson,
    "Helen at Northlake. Claude's draft for Henderson Lab uses Raj's internal reserve block, so the sharing check sent it to her. Approve and send, edit, redraft, decline, reassign or add a note; the fixtures are stateful. Details opens the properties beside the thread.",
  ),
  {
    title: "Requests list",
    description: "Every request Helen can see, searchable and filtered by direction, in the shape of Claude's Chats page.",
    layout: "page",
    height: 860,
    fixtures: requestsRoutes(),
    ...at(),
    render: () => <InApp />,
  },
  {
    title: "Requests list, loading",
    layout: "page",
    height: 480,
    fixtures: { ...requestsRoutes(), "GET /api/inbox": pending },
    ...at(),
    render: () => <RequestsPage />,
  },
  {
    title: "Requests list, empty",
    layout: "page",
    height: 480,
    fixtures: requestsRoutes({ world: emptyWorld }),
    ...at(),
    render: () => <RequestsPage />,
  },
  view(
    "A reply held for review",
    THREAD.vantage,
    "Northlake trusts Vantage on Ask, so every message to them waits for a teammate. Claude's reply declines to name clients; type what to change to send it back to Claude.",
  ),
  view("Shared with you", THREAD.calder, "Raj shared Calder's request with Helen and asked a question.", {}, 720),
  view(
    "Waiting on them",
    THREAD.pacific,
    "Pacific Beamline's side is deciding what its representative may say. Open Details: short of room, the long status truncates before the title does.",
    { world: waitingOnThem(requestsWorld(), THREAD.pacific) },
    720,
  ),
  view(
    "Closed",
    THREAD.ridgeline,
    "Ridgeline closed the thread after Helen approved the answer. The summary is Northlake's own; Ridgeline never sees it.",
  ),
  view(
    "A redraft that never lands",
    THREAD.henderson,
    "Ask for a redraft: the draft stays readable but can't be approved, and after three minutes the view stops waiting and lets you try again.",
    { redraftNeverLands: true },
  ),
  view(
    "Already resolved by a teammate",
    THREAD.henderson,
    "Raj approved first; Helen's approval comes back 409 escalation_not_open.",
    { resolvedFirstBy: members.raj },
  ),
  view(
    "Stopped",
    THREAD.pacific,
    "The demo's live-model budget ran out mid-conversation, so the system stopped the thread. The banner says why, from what the budget says now.",
    { world: stopped(requestsWorld(), THREAD.pacific, "demo_budget"), budget: "demo_exhausted" },
    720,
  ),
  view(
    "A teammate's item",
    THREAD.henderson,
    "Raj opens Henderson's item, which is Helen's. He may still act on it; the card and the composer say whose it is.",
    { as: members.raj },
  ),
  view(
    "Routed to two",
    THREAD.henderson,
    "Claude routed Henderson's question to Helen and Raj, each with their own why. Raj reads his.",
    { as: members.raj, world: routedToBoth(requestsWorld()) },
  ),
  {
    title: "What our representative saw",
    description: "Its frozen prompt, including what Northlake also shares with Pacific Beamline, and the answers released to it.",
    fixtures: requestsRoutes(),
    render: () => <SawDrawer />,
  },
] satisfies GallerySection[];
