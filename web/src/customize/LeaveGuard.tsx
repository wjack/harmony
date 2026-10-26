import { useEffect } from "react";
import { useBlocker } from "react-router";

import { Button, Modal } from "../shell/ui";

/**
 * Asks before unsaved settings are lost. The draft lives only in the page's state, so
 * leaving the route (the sidebar, Back, an inbox link) or the tab drops it. Switching
 * panes stays on the route and keeps it, so that never asks.
 */
export function LeaveGuard({ dirty }: { dirty: boolean }) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname,
  );

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => {
      window.removeEventListener("beforeunload", warn);
    };
  }, [dirty]);

  return (
    <Modal
      open={blocker.state === "blocked"}
      onOpenChange={(open) => {
        if (!open) blocker.reset?.();
      }}
      width={440}
      title="Discard unsaved changes?"
      description="Your changes to these settings aren't saved. If you leave now, they're lost."
      footer={
        <>
          <Button variant="secondary" onClick={() => blocker.reset?.()}>
            Keep editing
          </Button>
          <Button variant="primary" onClick={() => blocker.proceed?.()}>
            Discard changes
          </Button>
        </>
      }
    />
  );
}
