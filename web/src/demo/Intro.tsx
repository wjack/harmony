import { Button, Modal, Spark } from "../shell/ui";
import { useEnterDemo } from "./accounts";
import { closeIntro, showIntroHelp, useDemoState } from "./store";

/**
 * The first visit's intro (ARCHITECTURE.md §11.4, UI_REFERENCE.md §9): what Harmony is.
 * Next enters the demo as Alex, as Demo controls' "Try the suggested request" does, and
 * then shows "What's on screen" (HelpOverlay.tsx); closing that leaves the suggested
 * message in his composer. It opens by itself once; Demo controls stays in the demo bar.
 */
export function Intro() {
  const open = useDemoState((state) => state.introOpen);
  const enter = useEnterDemo(showIntroHelp);

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) closeIntro();
      }}
      icon={<Spark size={26} />}
      title="Meet Harmony"
      description={
        <>
          Every organization on the network has a Claude representing it.
          <span className="mt-3 block">
            When a user at one organization needs something, their Claude finds out who could help, and their Claudes
            collaborate, escalating for human input when needed.
          </span>
          <span className="mt-3 block">
            You can use this demo to act as people at different organizations on the Harmony Network.
          </span>
        </>
      }
      footer={
        <Button
          variant="primary"
          disabled={enter.isPending}
          onClick={() => {
            enter.mutate();
          }}
        >
          {enter.isPending ? "Starting…" : "Next"}
        </Button>
      }
    />
  );
}
