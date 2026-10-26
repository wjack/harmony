import { ArrowRight, CalendarDays, RotateCcw, SlidersHorizontal, Wallet, WifiOff, X } from "lucide-react";
import type { ReactNode } from "react";

import type { SessionView } from "../api/types";
import { useMembers } from "../app/queries";
import { firstName, shortOrgName } from "../shell/names";
import { Avatar, cx } from "../shell/ui";
import { useFollowAlert } from "./accounts";
import { alertsByAccount, clearAlertsFor, useNeedsHumanAlerts, type AccountAlerts } from "./alerts";
import { findMember, orgName } from "./cast";
import { openDemoControls, openResetConfirm } from "./store";
import { formatWorldDate, useWorldDate } from "./worldClock";

interface DemoBarProps {
  /** Null before the visitor has started a session. */
  session: SessionView | null;
  /** Live budget left, in USD, when the API reports it. */
  budgetRemainingUsd?: number | null;
  /** The API can't be reached (the dispatcher is retrying). */
  offline?: boolean;
  /** The live event stream dropped and hasn't come back yet: what's on screen may be stale. */
  reconnecting?: boolean;
}

/**
 * The slim bar above the cloned app (ARCHITECTURE.md §11.4). It is styled unlike the
 * product on purpose, so demo controls are never mistaken for Claude: who you're acting
 * as, the world date, budget, cross-account alerts and Reset.
 */
export function DemoBar({ session, budgetRemainingUsd = null, offline = false, reconnecting = false }: DemoBarProps) {
  const worldDate = useWorldDate(session?.world);
  const alerts = useNeedsHumanAlerts();

  return (
    <header
      aria-label="Demo controls"
      style={{ height: "var(--demo-bar-height)" }}
      className="flex shrink-0 items-center gap-1 overflow-x-auto bg-demo px-2.5 text-[12.5px] leading-none text-demo-text [scrollbar-width:none]"
    >
      <span className="mr-1 inline-flex h-5 items-center rounded-[4px] border border-demo-border px-1.5 font-mono text-[10px] font-medium tracking-[0.12em] text-demo-muted">
        DEMO
      </span>
      <BarButton
        onClick={() => {
          openDemoControls();
        }}
        icon={<SlidersHorizontal size={13} strokeWidth={1.75} />}>
        Demo controls
      </BarButton>

      <Divider />

      {session === null ? (
        <span className="px-1.5 text-demo-muted">
          {offline ? "Can't reach Harmony: retrying" : "Not started: open Demo controls to begin"}
        </span>
      ) : (
        <>
          <span className="flex min-w-0 shrink-0 items-center gap-2 px-1.5" title="The account you're acting as">
            <Avatar name={session.member.name} seed={session.member.org.slug} size={18} />
            <span className="text-demo-muted">Acting as</span>
            <span className="font-medium">{session.member.name}</span>
            <span className="text-demo-muted">· {session.member.org.name}</span>
          </span>
          <Divider />
          {worldDate !== null && (
            <span className="flex shrink-0 items-center gap-1.5 px-1.5" title="The world's date: the scenario's clock">
              <CalendarDays size={13} strokeWidth={1.75} className="text-demo-muted" aria-hidden />
              <span className="text-demo-muted">World date</span>
              <span className="font-medium tabular-nums">{formatWorldDate(worldDate)}</span>
            </span>
          )}
          {budgetRemainingUsd !== null && (
            <>
              <Divider />
              <span className="flex shrink-0 items-center gap-1.5 px-1.5" title="Live model budget left">
                <Wallet size={13} strokeWidth={1.75} className="text-demo-muted" aria-hidden />
                <span className="text-demo-muted">Budget</span>
                <span className="font-medium tabular-nums">${budgetRemainingUsd.toFixed(2)} left</span>
              </span>
            </>
          )}
        </>
      )}

      <span className="flex-1" />

      {reconnecting && (
        <span role="status" className="mr-1 flex shrink-0 items-center gap-1.5 px-1.5 text-demo-muted">
          <WifiOff size={13} strokeWidth={1.75} aria-hidden />
          Live updates paused: reconnecting…
        </span>
      )}
      {alertsByAccount(alerts).map((account) => (
        <AlertChip key={`${account.orgSlug}/${account.memberSlug}`} account={account} />
      ))}
      {session !== null && (
        <BarButton onClick={openResetConfirm} icon={<RotateCcw size={13} strokeWidth={1.75} />}>
          Reset
        </BarButton>
      )}
    </header>
  );
}

function Divider() {
  return <span aria-hidden className="mx-1 h-4 w-px shrink-0 bg-demo-border" />;
}

function BarButton({
  onClick,
  icon,
  children,
}: {
  onClick: () => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex h-6 shrink-0 items-center gap-1.5 rounded-[6px] px-2 text-demo-text transition-colors hover:bg-demo-surface"
    >
      <span className="text-demo-muted">{icon}</span>
      {children}
    </button>
  );
}

/**
 * "Northlake needs a human → Switch to Helen": one click acts as them and opens the
 * newest item. One chip per account, with a count when several items wait on them.
 */
function AlertChip({ account }: { account: AccountAlerts }) {
  const members = useMembers();
  const { follow, pending } = useFollowAlert();
  const { orgSlug, memberSlug, alerts } = account;
  const [newest] = alerts;
  const assignee = findMember(members.data, memberSlug, orgSlug);
  const org = shortOrgName(orgName(members.data, orgSlug));
  const who = assignee === null ? memberSlug : firstName(assignee.name);

  return (
    <span className="mr-1 inline-flex h-6 shrink-0 items-center rounded-full bg-demo-alert pr-0.5 pl-2 text-demo-alert-text ring-1 ring-demo-alert-ring">
      <span className="mr-1.5 size-1.5 animate-pulse-dot rounded-full bg-accent" aria-hidden />
      <button
        type="button"
        disabled={pending}
        onClick={() => {
          if (newest !== undefined) void follow(newest);
        }}
        className={cx("inline-flex items-center gap-1 hover:text-white", pending && "opacity-60")}
      >
        {org} needs a human{alerts.length > 1 && ` (${String(alerts.length)})`}
        <ArrowRight size={12} strokeWidth={2} aria-hidden />
        <span className="font-medium underline decoration-demo-alert-muted underline-offset-2">Switch to {who}</span>
      </button>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => {
          clearAlertsFor(memberSlug, orgSlug);
        }}
        className="ml-1 inline-flex size-5 items-center justify-center rounded-full text-demo-alert-muted hover:bg-demo-alert-hover hover:text-white"
      >
        <X size={12} strokeWidth={2} />
      </button>
    </span>
  );
}
