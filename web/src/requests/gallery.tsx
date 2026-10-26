/** The Requests inbox's gallery sections (/__gallery), over fixtures/requests.ts. */
import { useState } from "react";

import { useSession } from "../app/queries";
import { requestsRoutes, requestsWorld, THREAD, type RequestsWorld } from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import type { GallerySection } from "../gallery/types";
import { ShellFrame } from "../shell/AppLayout";
import { Button } from "../shell/ui";
import { RepresentativeSaw } from "./RepresentativeSaw";
import { RequestsPage } from "./RequestsPage";

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
  const representative = requestsWorld().threads.get(THREAD.pacific)?.workspace.representative ?? null;
  return (
    <>
      <Button
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
      />
    </>
  );
}

const pending = () => new Promise<never>(() => undefined);

/** Each section runs in its own memory router, at the app's own route. */
const PATH = "/requests/:threadId?";
const at = (threadId?: string) => ({ path: PATH, url: threadId === undefined ? "/requests" : `/requests/${threadId}` });
const emptyWorld: RequestsWorld = { threads: new Map() };

export default [
  {
    title: "Requests, in the app",
    description:
      "Helen at Northlake. Claude's draft for Henderson Lab uses Raj's internal reserve block, so the sharing check sent it to her. Approve & send, edit, redraft, decline or reassign; the fixtures are stateful.",
    layout: "page",
    height: 860,
    fixtures: requestsRoutes(),
    ...at(THREAD.henderson),
    render: () => <InApp />,
  },
  {
    title: "Nothing selected",
    layout: "page",
    height: 600,
    fixtures: requestsRoutes(),
    ...at(),
    render: () => <RequestsPage />,
  },
  {
    title: "A reply held for review",
    description:
      "Northlake trusts Vantage on Ask, so every message to them waits for a teammate. Claude's reply declines to name clients.",
    layout: "page",
    height: 760,
    fixtures: requestsRoutes(),
    ...at(THREAD.vantage),
    render: () => <RequestsPage />,
  },
  {
    title: "Shared with you",
    description: "Raj shared Calder's request with Helen and asked a question.",
    layout: "page",
    height: 640,
    fixtures: requestsRoutes(),
    ...at(THREAD.calder),
    render: () => <RequestsPage />,
  },
  {
    title: "Closed",
    description:
      "Ridgeline closed the thread after Helen approved the answer. The summary is Northlake's own; Ridgeline never sees it.",
    layout: "page",
    height: 720,
    fixtures: requestsRoutes(),
    ...at(THREAD.ridgeline),
    render: () => <RequestsPage />,
  },
  {
    title: "A redraft that never lands",
    description:
      "Ask for a redraft: the draft stays readable but can't be approved, and after a minute the panel stops waiting and lets you try again.",
    layout: "page",
    height: 860,
    fixtures: requestsRoutes({ redraftNeverLands: true }),
    ...at(THREAD.henderson),
    render: () => <RequestsPage />,
  },
  {
    title: "Already resolved by a teammate",
    description: "Raj approved first; Helen's approval comes back 409 escalation_not_open.",
    layout: "page",
    height: 780,
    fixtures: requestsRoutes({ resolvedFirstBy: members.raj }),
    ...at(THREAD.henderson),
    render: () => <RequestsPage />,
  },
  {
    title: "Loading",
    layout: "page",
    height: 420,
    fixtures: { ...requestsRoutes(), "GET /api/inbox": pending },
    ...at(),
    render: () => <RequestsPage />,
  },
  {
    title: "Empty inbox",
    layout: "page",
    height: 420,
    fixtures: requestsRoutes({ world: emptyWorld }),
    ...at(),
    render: () => <RequestsPage />,
  },
  {
    title: "What our representative saw",
    description: "Its frozen prompt, including what Northlake also shares with Pacific Beamline, and the answers released to it.",
    fixtures: requestsRoutes(),
    render: () => <SawDrawer />,
  },
] satisfies GallerySection[];
