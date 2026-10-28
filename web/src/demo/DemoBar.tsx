import {
  ArrowRight,
  CalendarDays,
  CircleSlash,
  RotateCcw,
  SlidersHorizontal,
  Wallet,
  Waypoints,
  WifiOff,
  X,
} from "lucide-react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router";

import type { MemberView, SessionView } from "../api/types";
import { useBudget } from "../app/budget";
import { useMembers } from "../app/queries";
import { budgetLine } from "../chat/failureCopy";
import { firstName, shortOrgName } from "../shell/names";
import { Avatar, cx } from "../shell/ui";
import { useFollowAlert } from "./accounts";
import { alertsByAccount, clearAlertsFor, useNeedsHumanAlerts, type AccountAlerts } from "./alerts";
import { findMember, orgName } from "./cast";
import { HelpButton } from "./HelpOverlay";
import { openDemoControls, openResetConfirm } from "./store";
import { useOthersWaiting } from "./waiting";
import { formatWorldDate, useWorldDate } from "./worldClock";

interface DemoBarProps {
  /** Null before the visitor has started a session. */
  session: SessionView | null;
  /** The API can't be reached (the dispatcher is retrying). */
  offline?: boolean;
  /** The live event stream dropped and hasn't come back yet: what's on screen may be stale. */
  reconnecting?: boolean;
}

/**
 * The slim bar above the cloned app (ARCHITECTURE.md §11.4). It is styled unlike the
 * product on purpose, so demo controls are never mistaken for Claude: who you're acting
 * as, the network view (which watches every side, so it is the demo's, not the
 * product's, and counts the other accounts with an item waiting on them), the world
 * date, budget, cross-account alerts and Reset, and a "?" that says what the demo's key
 * controls do (HelpOverlay.tsx; each control it points at carries `data-help`).
 *
 * The budget, Reset and "?" are pinned right and never leave it. Who and when take the
 * room the alerts leave, measured on their own group: short of it, their labels drop
 * first ("Acting as", the org's full name, "Demo controls"), and only then are they cut
 * at their end. The date is kept small: the scenario's clock, not a control.
 */
export function DemoBar({ session, offline = false, reconnecting = false }: DemoBarProps) {
  const worldDate = useWorldDate(session?.world);
  const alerts = useNeedsHumanAlerts();
  const navigate = useNavigate();
  const othersWaiting = useOthersWaiting(session?.member.id ?? null);

  return (
    <header
      aria-label="Demo controls"
      style={{ height: "var(--demo-bar-height)" }}
      className="flex shrink-0 items-center gap-1 bg-demo px-2.5 text-[12.5px] leading-none text-demo-text"
    >
      {/* Each label drops where the longest of the cast's own ("Dr. Helen Ward · Northlake
          Characterization Core", with a count on Network view) would otherwise be cut. */}
      <div className="@container/who flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
        <span className="mr-1 inline-flex h-5 shrink-0 items-center rounded-[4px] border border-demo-border px-1.5 font-mono text-[10px] font-medium tracking-[0.12em] text-demo-muted">
          DEMO
        </span>
        <BarButton
          label="Demo controls"
          help="demo-controls"
          labelClassName="@max-[640px]/who:hidden"
          onClick={() => {
            openDemoControls();
          }}
          icon={<SlidersHorizontal size={13} strokeWidth={1.75} />}
        />
        {session !== null && (
          <BarButton
            label="Network view"
            help="network"
            labelClassName="@max-[640px]/who:hidden"
            onClick={() => {
              void navigate("/network");
            }}
            icon={<Waypoints size={13} strokeWidth={1.75} />}
            badge={othersWaiting}
            badgeTitle={`${String(othersWaiting)} other ${othersWaiting === 1 ? "account needs" : "accounts need"} a human`}
          />
        )}

        <Divider />

        {session === null ? (
          <span className="truncate px-1.5 text-demo-muted">
            {offline ? "Can't reach Harmony: retrying" : "Not started: open Demo controls to begin"}
          </span>
        ) : (
          <>
            <ActingAs member={session.member} />
            <Divider />
            {worldDate !== null && (
              <span
                className="flex shrink-0 items-center gap-1 px-1.5 text-[11px] text-demo-muted tabular-nums"
                title="World date: the scenario's clock"
              >
                <CalendarDays size={11} strokeWidth={1.75} aria-hidden />
                {formatWorldDate(worldDate)}
              </span>
            )}
          </>
        )}
      </div>

      {/* Gives way only when the alerts alone are wider than the bar's room. */}
      <div className="flex min-w-0 items-center overflow-hidden">
        {reconnecting && (
          <span role="status" className="mr-1 flex shrink-0 items-center gap-1.5 px-1.5 text-demo-muted">
            <WifiOff size={13} strokeWidth={1.75} aria-hidden />
            Live updates paused: reconnecting…
          </span>
        )}
        {alertsByAccount(alerts).map((account) => (
          <AlertChip key={`${account.orgSlug}/${account.memberSlug}`} account={account} />
        ))}
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <Budget />
        {session !== null && (
          <BarButton
            label="Reset"
            help="reset"
            onClick={openResetConfirm}
            icon={<RotateCcw size={13} strokeWidth={1.75} />}
          />
        )}
        <HelpButton />
      </div>
    </header>
  );
}

/** "Acting as Raj Patel · Northlake Characterization Core", down to "Raj Patel · Northlake", then "Raj Patel". */
function ActingAs({ member }: { member: MemberView }) {
  const org = member.org.name;
  const short = shortOrgName(org);
  return (
    <span
      data-help="acting-as"
      className="flex min-w-0 shrink-0 items-center gap-2 px-1.5"
      title="The account you're acting as"
    >
      <Avatar name={member.name} seed={member.org.slug} size={18} />
      <span className="text-demo-muted @max-[836px]/who:hidden">Acting as</span>
      <span className="font-medium">{member.name}</span>
      <span className="text-demo-muted @max-[461px]/who:hidden">
        ·{" "}
        {short === org ? (
          org
        ) : (
          <>
            <span className="@max-[773px]/who:hidden">{org}</span>
            <span className="hidden @max-[773px]/who:inline">{short}</span>
          </>
        )}
      </span>
    </span>
  );
}

/**
 * "Live · $7.40 left", or the line for the cap that stops live turns (LIVE_DEMO_PLAN
 * 3.5). Nothing while hosted caps are off. It opens Demo controls' "Model and spend".
 */
function Budget() {
  const budget = useBudget().data;
  if (budget?.live !== true) return null;
  const line = budgetLine(budget.state);
  const remaining = budget.session?.remaining_usd;
  return (
    <>
      <Divider />
      <button
        type="button"
        data-help="budget"
        title="This session's live-model budget"
        onClick={() => {
          openDemoControls();
        }}
        className={cx(
          "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-[6px] px-1.5 transition-colors hover:bg-demo-surface",
          line !== null && "text-demo-alert-text",
        )}
      >
        {line === null ? (
          <>
            <Wallet size={13} strokeWidth={1.75} className="text-demo-muted" aria-hidden />
            <span className="font-medium tabular-nums">
              {remaining === undefined ? "Live" : `Live · $${remaining.toFixed(2)} left`}
            </span>
          </>
        ) : (
          <>
            <CircleSlash size={13} strokeWidth={1.75} className="text-demo-alert-muted" aria-hidden />
            {line}
          </>
        )}
      </button>
    </>
  );
}

function Divider() {
  return <span aria-hidden className="mx-1 h-4 w-px shrink-0 bg-demo-border" />;
}

function BarButton({
  label,
  help,
  labelClassName,
  onClick,
  icon,
  badge = 0,
  badgeTitle,
}: {
  label: string;
  /** The key "What's on screen" finds it by. */
  help?: string;
  /** Hides the label where the bar is short of room; the icon stays, and the button keeps its name. */
  labelClassName?: string;
  onClick: () => void;
  icon: ReactNode;
  /** A count after the label, shown while above 0, with what it counts as the button's title. */
  badge?: number;
  badgeTitle?: string;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      data-help={help}
      title={badge > 0 ? badgeTitle : undefined}
      onClick={onClick}
      className="inline-flex h-6 shrink-0 items-center gap-1.5 rounded-[6px] px-2 text-demo-text transition-colors hover:bg-demo-surface"
    >
      <span className="text-demo-muted">{icon}</span>
      <span className={labelClassName}>{label}</span>
      {badge > 0 && (
        <span
          data-badge
          className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-white tabular-nums"
        >
          {badge}
        </span>
      )}
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
