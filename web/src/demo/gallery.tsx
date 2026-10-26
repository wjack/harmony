/** The demo layer's gallery sections (/__gallery). */
import { useSession } from "../app/queries";
import { members, needsHumanEnvelope } from "../fixtures/shell";
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

export default [
  {
    title: "Demo bar",
    description: "Acting account, world date and Reset, styled apart from the product.",
    layout: "bare",
    render: () => <LiveDemoBar />,
  },
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
