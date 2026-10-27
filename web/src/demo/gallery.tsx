/** The demo layer's gallery sections (/__gallery). */
import { useSession } from "../app/queries";
import type { BudgetView } from "../app/budget";
import { budgets, members, needsHumanEnvelope } from "../fixtures/shell";
import { useFixtureEmit } from "../gallery/fixtureContext";
import type { GallerySection } from "../gallery/types";
import { Button } from "../shell/ui";
import { DemoBar } from "./DemoBar";
import { DemoControls } from "./DemoControls";
import { DemoCues } from "./DemoCues";
import { ResetDemoDialog } from "./ResetDemoDialog";
import { openDemoControls, openResetConfirm } from "./store";

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

function ControlsLauncher() {
  const session = useSession();
  return (
    <div className="flex flex-wrap gap-2">
      <Button
        variant="primary"
        onClick={() => {
          openDemoControls("overview");
        }}
      >
        Open demo controls
      </Button>
      <Button
        onClick={() => {
          openDemoControls("controls");
        }}
      >
        Open the Controls tab
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
    title: "Demo controls and reset",
    description: "Opens on the first visit: the network in thirty seconds, the cast and three ways in.",
    render: () => <ControlsLauncher />,
  },
] satisfies GallerySection[];
