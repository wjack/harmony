import { useEffect, useState, type ReactNode } from "react";
import { Outlet, useLocation } from "react-router";

import type { SessionView } from "../api/types";
import { useRealtime } from "../app/context";
import { useSession } from "../app/queries";
import { DemoBar } from "../demo/DemoBar";
import { DemoControls } from "../demo/DemoControls";
import { ResetDemoDialog } from "../demo/ResetDemoDialog";
import { openDemoControls, useDemoState } from "../demo/store";
import { Landing } from "./Landing";
import { Locked } from "./Locked";
import { Sidebar } from "./Sidebar";
import { cx } from "./ui";
import { Unreachable } from "./Unreachable";

/**
 * The app frame for every product route (ARCHITECTURE.md §12): the demo bar above the
 * cloned UI, then Claude's sidebar and the page.
 *
 * Everything that reads the acting member is keyed on the dispatcher's `generation`.
 * On an account switch or a reset, the dispatcher keeps the previous member's cache
 * while it fetches the new snapshot, so the frame goes on showing them (inert) until
 * the generation bumps; then the whole frame remounts and refetches as the new member.
 * Modal and sidebar state live in stores, so they survive the remount.
 */
export function AppLayout() {
  const status = useRealtime((state) => state.status);
  const generation = useRealtime((state) => state.generation);
  const failures = useRealtime((state) => state.failures);
  const welcomed = useDemoState((state) => state.welcomed);

  // The demo controls introduce the demo on a visitor's first visit.
  useEffect(() => {
    if (!welcomed && (status === "no-session" || status === "open")) openDemoControls("overview");
  }, [welcomed, status]);

  // Once this tab has shown the landing, keep it up while the session starts, rather
  // than blanking the screen (and the demo controls) between the two.
  const [sawLanding, setSawLanding] = useState(false);
  if (status === "no-session" && !sawLanding) setSawLanding(true);

  const landing = (
    <ShellFrame session={null}>
      <Landing />
      <DemoControls session={null} />
    </ShellFrame>
  );
  if (status === "locked") return <Locked />;
  if (status === "no-session") return landing;
  // Before the run's first snapshot there is no member to show: the first load, or a
  // fresh start after the session went away (the cache was cleared with it).
  if (generation === 0 || status === "starting") {
    if (failures > 0) {
      return (
        <ShellFrame session={null} status="offline">
          <DemoControls session={null} />
        </ShellFrame>
      );
    }
    return sawLanding ? landing : <div className="h-full bg-canvas" aria-busy="true" />;
  }
  return <LiveShell key={generation} />;
}

/**
 * How long the live stream may be down before the demo bar says so. The browser
 * reconnects by itself, so a blip (a restart, a network hiccup) passes unremarked.
 */
export const RECONNECT_GRACE_MS = 5_000;

/** The live stream has been down for longer than `RECONNECT_GRACE_MS`. */
function useStreamDown(): boolean {
  const down = useRealtime((state) => state.status === "reconnecting");
  const [overdue, setOverdue] = useState(false);
  useEffect(() => {
    if (!down) return;
    const timer = window.setTimeout(() => {
      setOverdue(true);
    }, RECONNECT_GRACE_MS);
    return () => {
      window.clearTimeout(timer);
      setOverdue(false);
    };
  }, [down]);
  return down && overdue;
}

function LiveShell() {
  const session = useSession();
  const resetting = useRealtime((state) => state.status === "resetting");
  const failing = useRealtime((state) => state.failures > 0);
  const streamDown = useStreamDown();
  const current = session.data ?? null;
  // A switch leaves the previous member's own item before the new member's frame mounts.
  // A page this frame first meets then would fetch as them after the server has moved
  // on (a 404 for their organization's settings), so it waits for the new frame.
  const { key } = useLocation();
  const [shownKey, setShownKey] = useState(key);
  if (!resetting && shownKey !== key) setShownKey(key);
  const leaving = resetting && shownKey !== key;
  return (
    <ShellFrame
      session={current}
      status={resetting ? (failing ? "offline" : "switching") : "live"}
      streamDown={streamDown}
    >
      {leaving ? <div className="h-full" aria-busy="true" /> : <Outlet />}
      <DemoControls session={current} />
      <ResetDemoDialog />
    </ShellFrame>
  );
}

/**
 * - `live`: the acting member's app.
 * - `switching`: an account switch or reset is under way; the previous member stays on
 *   screen, inert, so nothing is done as them after the server has moved on.
 * - `offline`: the API can't be reached; the frame says so while the dispatcher retries.
 */
export type FrameStatus = "live" | "switching" | "offline";

interface ShellFrameProps {
  session: SessionView | null;
  status?: FrameStatus;
  /** The live stream dropped a while ago and is reconnecting; the app stays usable. */
  streamDown?: boolean;
  /** The page; dialogs may ride along (they portal out). */
  children: ReactNode;
  /** Gallery-only: pin the sidebar's state. */
  sidebar?: { collapsed?: boolean; defaultMenuOpen?: boolean };
}

/** Demo bar, sidebar and the page area. The gallery renders it around fixtures. */
export function ShellFrame({ session, status = "live", streamDown = false, children, sidebar }: ShellFrameProps) {
  const live = status === "live";
  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      <DemoBar session={session} offline={status === "offline"} reconnecting={live && streamDown} />
      <div className="relative flex min-h-0 flex-1">
        <div
          inert={!live}
          aria-busy={live ? undefined : true}
          className={cx(
            // `min-w-0`: the page shrinks with a narrow window instead of pushing the
            // composer and Share past its right edge.
            "flex min-h-0 min-w-0 flex-1",
            // Dim only if the switch takes long enough to notice.
            status === "switching" && "opacity-60 transition-opacity delay-200 duration-300",
          )}
        >
          <Sidebar member={session?.member ?? null} offline={status === "offline"} {...sidebar} />
          <main id="main" className="relative min-w-0 flex-1 overflow-y-auto bg-canvas">
            {children}
          </main>
        </div>
        {status === "offline" && <Unreachable />}
      </div>
    </div>
  );
}
