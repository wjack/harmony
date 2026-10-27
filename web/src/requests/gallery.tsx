/** Requests' gallery sections (/__gallery), over fixtures/requests.ts. */
import { useRef, useState } from "react";

import { useSession } from "../app/queries";
import {
  ESCALATION,
  checking,
  edited,
  expired,
  reopenFailed,
  reopenOffer,
  reopenWorking,
  reopened,
  requestsRoutes,
  requestsWorld,
  rewritten,
  stopped,
  THREAD,
  waitingOnThem,
  type RequestsRouteOptions,
  type RequestsWorld,
} from "../fixtures/requests";
import { members, nonAdmin, orgs } from "../fixtures/shell";
import type { GallerySection } from "../gallery/types";
import { ShellFrame } from "../shell/AppLayout";
import { Button } from "../shell/ui";
import { RepresentativeSaw } from "./RepresentativeSaw";
import { RequestsPage } from "./RequestsPage";
import type { RepresentativeContext } from "./types";

/** The page in the app's frame, with the sidebar's requests beside it. */
function InApp() {
  const session = useSession();
  return (
    <ShellFrame session={session.data ?? null}>
      <RequestsPage />
    </ShellFrame>
  );
}

/** Pacific Beamline's prompt, with two versions of guidance from Northlake's edits delivered since. */
function sawWithGuidance(): RepresentativeContext | null {
  const representative = requestsWorld().threads.get(THREAD.pacific)?.workspace.representative ?? null;
  if (representative === null) return null;
  const at = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
  return {
    ...representative,
    guidance: [
      { version: 1, text: "Quote the broad-ion-beam price only; don't quote FIB time this quarter.", delivered_at: at(40) },
      {
        version: 2,
        text: "Quote the broad-ion-beam price only; don't quote FIB time this quarter. Say nothing is booked until they confirm.",
        delivered_at: at(12),
      },
    ],
  };
}

/** The drawer over Pacific Beamline's thread: a trusted organization's extra, an automatic release and guidance. */
function SawDrawer() {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement | null>(null);
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
        representative={sawWithGuidance()}
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

/** A fresh world with `change` made to it. */
const world = (change: (world: RequestsWorld) => RequestsWorld): RequestsWorld => change(requestsWorld());

/** Helen's Edit on Henderson's open item, as `fields` leaves it. */
const hendersonEdit = (fields: Parameters<typeof edited>[2]) =>
  world((made) => edited(made, THREAD.henderson, { escalation_id: ESCALATION.henderson, draft_rev: 2, ...fields }));

export default [
  view(
    "A request, in the app",
    THREAD.henderson,
    "Helen at Northlake. Claude's draft for Henderson Lab uses the internal reserve block, so the sharing check sent it to her. Change the wording in place, approve and send, decline, reassign, comment, or switch the box to Edit to tell Claude what to do differently; the fixtures are stateful.",
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
    "A message held for review",
    THREAD.vantage,
    "Northlake works with Vantage in Ask mode, so every message to them waits for a teammate. The card says so, with a link to change it.",
  ),
  view("Shared with you", THREAD.calder, "Raj shared Calder's request with Helen and asked a question.", {}, 720),

  // --- Whose move it is -------------------------------------------------------------------
  view(
    "Waiting on them",
    THREAD.pacific,
    "Our message landed, so it's Pacific Beamline's move: one steady row says so, whatever their Claude is doing, and the pill says the same.",
    { world: world((made) => waitingOnThem(made, THREAD.pacific)) },
    720,
  ),
  view(
    "Checking our records",
    THREAD.pacific,
    "Our Claude asked Northlake's own Claude a question and is waiting for the answer.",
    { world: world((made) => checking(made, THREAD.pacific)) },
    720,
  ),
  view(
    "A teammate's item",
    THREAD.henderson,
    "Raj opens Henderson's item, which is Helen's: the last row says whose answer it waits on. He may still act on it; the card says whose it is.",
    { as: members.raj },
  ),
  view(
    "A non-admin's card",
    THREAD.henderson,
    "Helen as a non-admin: she can read, not change, what Northlake shares with Henderson Lab.",
    { as: nonAdmin(members.helen) },
  ),
  view(
    "Already resolved by a teammate",
    THREAD.henderson,
    "Raj approved first; Helen's approval comes back 409 escalation_not_open.",
    { resolvedFirstBy: members.raj },
  ),

  // --- Edit -----------------------------------------------------------------------------
  view(
    "Edit: Claude is rewriting",
    THREAD.henderson,
    "Helen told Claude what to do differently. The draft stays readable but locked, Approve waits for the new draft, and her row says Claude is working on it.",
    { world: hendersonEdit({ status: "working", guidance_outcome: null }), steerHangs: true },
  ),
  view(
    "Edit: rewritten",
    THREAD.henderson,
    "Claude rewrote the draft from Helen's edit and follows it for the rest of the conversation. ‹ 2 of 2 › pages back to its first draft; See guidance opens Details at what Claude now follows.",
    { world: world((made) => rewritten(made, THREAD.henderson)) },
  ),
  view(
    "Edit with nothing waiting",
    THREAD.pacific,
    "No draft is waiting, so the edit changes what Claude does from its next message.",
    { world: world((made) => edited(waitingOnThem(made, THREAD.pacific), THREAD.pacific)) },
    720,
  ),
  view(
    "Edit: still working",
    THREAD.pacific,
    "Claude is still turning Helen's edit into guidance. Tell Claude stays on; a second edit is refused until this one lands.",
    {
      world: world((made) => edited(waitingOnThem(made, THREAD.pacific), THREAD.pacific, { status: "working", guidance_outcome: null })),
    },
    720,
  ),
  view(
    "Edit: both halves failed",
    THREAD.henderson,
    "Claude couldn't be reached, so neither the guidance nor the draft changed. Try again puts the text back in Edit.",
    {
      world: hendersonEdit({
        status: "failed",
        guidance_outcome: "failed",
        draft_outcome: "failed",
        failure_reason: "model_error",
      }),
    },
  ),
  view(
    "Edit: partly failed",
    THREAD.henderson,
    "The guidance changed, but the draft couldn't be rewritten: Helen can change it herself, or try again.",
    {
      world: hendersonEdit({
        status: "failed",
        guidance_outcome: "updated",
        draft_outcome: "failed",
        failure_reason: "model_error",
      }),
    },
  ),
  view(
    "Edit: added details",
    THREAD.henderson,
    "Claude's guidance added a fact Helen didn't give, so the code check refused it and nothing changed.",
    {
      world: hendersonEdit({
        status: "failed",
        guidance_outcome: "failed",
        draft_outcome: null,
        draft_rev: null,
        failure_reason: "added_details",
      }),
    },
  ),

  // --- Closed and Reopen ----------------------------------------------------------------
  view(
    "Closed",
    THREAD.ridgeline,
    "Ridgeline closed the conversation after Helen approved the answer. The summary is Northlake's own; Ridgeline never sees it. Reopen opens a small prompt where the button was.",
  ),
  view(
    "Closed: both sides went quiet",
    THREAD.pacific,
    "Nobody closed it: it ended after two quiet turns. It reads Closed all the same, with the reason.",
    { world: world((made) => expired(made, THREAD.pacific)) },
    720,
  ),
  view(
    "Closed: the budget ran out",
    THREAD.pacific,
    "The demo's live-model budget ran out mid-conversation, so the system ended it. Reopen waits until the budget is back.",
    { world: world((made) => stopped(made, THREAD.pacific, "demo_budget")), budget: "demo_exhausted" },
    720,
  ),
  view(
    "Closed: still wrapping up",
    THREAD.ridgeline,
    "Claude hasn't finished its closing turn, so Reopen waits.",
    { world: world((made) => reopenOffer(made, THREAD.ridgeline, { state: "wrapping_up", block: null })) },
  ),
  view(
    "Closed: only they can reopen it",
    THREAD.ridgeline,
    "Ridgeline's Claude declined to continue, so only Ridgeline can reopen it.",
    { world: world((made) => reopenOffer(made, THREAD.ridgeline, { state: "blocked", block: "refused" })) },
  ),
  view(
    "Closed: seeded history",
    THREAD.survey,
    "A conversation from the seeded history: it never ran here, so there's no Reopen.",
  ),
  view(
    "Reopening",
    THREAD.ridgeline,
    "Helen reopened it, and Claude is picking it up. Ridgeline sees nothing yet.",
    { world: world((made) => reopenWorking(made, THREAD.ridgeline)) },
  ),
  view(
    "Reopened",
    THREAD.ridgeline,
    "Claude turned what Helen typed into guidance and wrote the next message. The first run's summary stays on its Closed row.",
    { world: world((made) => reopened(made, THREAD.ridgeline)) },
  ),
  view(
    "Reopened by them",
    THREAD.ridgeline,
    "Ridgeline reopened it: Northlake sees only that they did, never what they typed.",
    { world: world((made) => reopened(made, THREAD.ridgeline, "them")) },
  ),
  view(
    "Reopen failed",
    THREAD.ridgeline,
    "Claude found nothing new to pick up in what Helen typed. Nothing was sent; Try again opens the prompt with her text.",
    { world: world((made) => reopenFailed(made, THREAD.ridgeline)) },
  ),
  {
    title: "What our representative saw",
    description:
      "Its frozen prompt, including what Northlake also shares with Pacific Beamline, the answers released to it, and each version of guidance it was sent.",
    fixtures: requestsRoutes(),
    render: () => <SawDrawer />,
  },
] satisfies GallerySection[];
