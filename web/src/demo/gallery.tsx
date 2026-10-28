/** The demo layer's gallery sections (/__gallery). */
import type { ReactNode } from "react";

import { unwrap } from "../api/client";
import { useApi } from "../app/context";
import { useSession } from "../app/queries";
import type { BudgetView } from "../app/budget";
import { budgets, members, needsHumanEnvelope, shellRoutes } from "../fixtures/shell";
import { useFixtureEmit } from "../gallery/fixtureContext";
import type { GallerySection } from "../gallery/types";
import { ShellFrame } from "../shell/AppLayout";
import { Button } from "../shell/ui";
import { DemoBar } from "./DemoBar";
import { DemoControls } from "./DemoControls";
import { DemoCues } from "./DemoCues";
import { ALEX_CHAT, EARLIER_ID, OTHERS_THREAD, memberChangedEnvelope, whoseRoutes } from "./fixtures";
import { ResetDemoDialog } from "./ResetDemoDialog";
import { openDemoControls, openResetConfirm } from "./store";
import { setUnsaved } from "./unsaved";
import { WhoseNotFound } from "./WhoseNotFound";

let nextSeq = 100;

function LiveDemoBar() {
  const session = useSession();
  return <DemoBar session={session.data ?? null} />;
}

/** The real cue path: an event through the section's dispatcher, to a toast and a chip. */
function NeedsHumanCue() {
  const emit = useFixtureEmit();
  return (
    <div className="flex flex-col gap-3">
      <DemoCues />
      <LiveDemoBar />
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => {
            nextSeq += 1;
            emit(needsHumanEnvelope(nextSeq, members.helen.slug, `0b1d7a3e-0000-4000-8000-0000000003e${nextSeq % 10}`));
          }}
        >
          Send demo.needs_human for Helen
        </Button>
        <Button
          onClick={() => {
            nextSeq += 1;
            emit(needsHumanEnvelope(nextSeq, members.alex.slug, "0b1d7a3e-0000-4000-8000-0000000003f1", "henderson"));
          }}
        >
          …for Alex (the acting member)
        </Button>
      </div>
    </div>
  );
}

/**
 * Another tab switches to Helen while this one holds unsaved Customize edits: this tab
 * follows in place, and says the edits went (FEEDBACK_R2_PLAN §2.6).
 */
function SwitchElsewhere() {
  const api = useApi();
  const emit = useFixtureEmit();
  return (
    <div className="flex flex-col gap-3">
      <DemoCues />
      <LiveDemoBar />
      <div>
        <Button
          onClick={() => {
            setUnsaved("customize", true);
            void unwrap(api.POST("/api/session/act-as", { body: { member_id: members.helen.id } })).then((session) => {
              nextSeq += 1;
              emit(memberChangedEnvelope(nextSeq, session));
            });
          }}
        >
          Switch to Helen in another tab, with unsaved Customize edits here
        </Button>
      </div>
    </div>
  );
}

/** A not-found page in the app frame, acting as Helen. */
function InFrame({ children }: { children: ReactNode }) {
  const session = useSession();
  return <ShellFrame session={session.data ?? null}>{children}</ShellFrame>;
}

const AS_HELEN = { ...shellRoutes(members.helen), ...whoseRoutes };

function ControlsLauncher() {
  const session = useSession();
  return (
    <div className="flex flex-wrap gap-2">
      <Button variant="primary" onClick={openDemoControls}>
        Open demo controls
      </Button>
      <Button onClick={openResetConfirm}>Reset demo…</Button>
      <DemoControls session={session.data ?? null} />
      <ResetDemoDialog />
    </div>
  );
}

/** The demo bar over one `GET /api/budget` answer. */
function budgetBar(title: string, description: string, budget: BudgetView): GallerySection {
  return { title, description, layout: "bare", fixtures: { "GET /api/budget": budget }, render: () => <LiveDemoBar /> };
}

export default [
  {
    title: "Demo bar",
    description: "Acting account, world date, this session's live budget and Reset, styled apart from the product.",
    layout: "bare",
    render: () => <LiveDemoBar />,
  },
  budgetBar("Demo bar: session budget used up", "This session's cap is spent; Reset doesn't refill it.", budgets.sessionExhausted),
  budgetBar("Demo bar: daily budget used up", "Today's cap, or this client's, is spent until 00:00 UTC.", budgets.dailyExhausted),
  budgetBar("Demo bar: demo budget used up", "The demo's global cap is spent.", budgets.demoExhausted),
  budgetBar(
    "Demo bar: provider block",
    "The model provider refused (billing or a usage limit): the same line as the global cap.",
    budgets.providerBlocked,
  ),
  budgetBar("Demo bar: paused", "The operator paused live models.", budgets.paused),
  budgetBar("Demo bar: caps off", "Hosted caps are off (local development): no budget to show.", budgets.off),
  {
    title: "Demo bar before a session",
    layout: "bare",
    render: () => <DemoBar session={null} />,
  },
  {
    title: "Cross-account alerts",
    description: "A demo.needs_human cue for another account adds a chip and a toast; one click acts as them.",
    render: () => <NeedsHumanCue />,
  },
  {
    title: "Not found: someone else's chat",
    description: "A pasted link or Back to Alex's chat, as Helen: whose it is, and a demo-chrome switch to them.",
    layout: "page",
    height: 560,
    fixtures: AS_HELEN,
    path: "/chat/:id",
    url: `/chat/${ALEX_CHAT}`,
    render: () => (
      <InFrame>
        <WhoseNotFound kind="chat" id={ALEX_CHAT} />
      </InFrame>
    ),
  },
  {
    title: "Not found: another org's request",
    description: "A Henderson–Ridgeline request, as Helen: both sides, who can open it, one switch per side.",
    layout: "page",
    height: 560,
    fixtures: AS_HELEN,
    path: "/requests/:threadId",
    url: `/requests/${OTHERS_THREAD}`,
    render: () => (
      <InFrame>
        <WhoseNotFound kind="thread" id={OTHERS_THREAD} />
      </InFrame>
    ),
  },
  {
    title: "Not found: from an earlier demo",
    description: "An id this world doesn't have (before a reset, or an earlier visit): never read as a permissions problem.",
    layout: "page",
    height: 560,
    fixtures: AS_HELEN,
    path: "/requests/:threadId",
    url: `/requests/${EARLIER_ID}`,
    render: () => (
      <InFrame>
        <WhoseNotFound kind="thread" id={EARLIER_ID} />
      </InFrame>
    ),
  },
  {
    title: "Unsaved edits dropped by another tab's switch",
    description: "This tab follows the switch in place, without a reload, and says what the switch dropped.",
    render: () => <SwitchElsewhere />,
  },
  {
    title: "Demo controls and reset",
    description: "Opens from the demo bar and the account menu (a first visit opens the intro instead).",
    render: () => <ControlsLauncher />,
  },
] satisfies GallerySection[];
