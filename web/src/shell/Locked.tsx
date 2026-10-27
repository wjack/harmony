import { useLocation } from "react-router";

import { gatePath } from "../api/client";
import { Button, Spark } from "./ui";

/**
 * While the tab leaves for the passcode page (`RealtimeDispatcher.lock`). Nothing from
 * behind the gate stays on screen, and the button is there if the navigation stalls.
 */
export function Locked() {
  const { pathname, search, hash } = useLocation();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 bg-canvas px-6 pb-[12vh] text-center">
      <Spark size={28} />
      <h1 className="font-serif text-[26px] leading-9 tracking-[-0.015em] text-ink">Harmony is locked</h1>
      <p className="max-w-[480px] text-sm leading-6 text-muted">
        Your demo is kept. Enter the passcode to pick up where you left off.
      </p>
      <Button
        variant="secondary"
        className="mt-1"
        onClick={() => {
          window.location.assign(gatePath(pathname + search + hash));
        }}
      >
        Enter passcode
      </Button>
    </div>
  );
}
