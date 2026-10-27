import { ArrowRight, Check, Compass, Waypoints, Sparkles, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { MemberView, SessionView } from "../api/types";
import { useBudget, type BudgetView } from "../app/budget";
import { useMembers } from "../app/queries";
import { budgetLine } from "../chat/failureCopy";
import { Avatar, Button, Modal, ModalList, Segmented, Spark, Toggle, cx } from "../shell/ui";
import { useUnavailable } from "../shell/unavailable";
import { useEnterDemo, useSwitchAccount, type DemoEntry } from "./accounts";
import { castLine } from "./cast";
import {
  closeDemoControls,
  openResetConfirm,
  setDemoControlsTab,
  setWalkthroughPref,
  useDemoState,
  type DemoControlsTab,
} from "./store";
import { formatWorldDate, useWorldDate } from "./worldClock";

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "controls", label: "Controls" },
] as const;

/**
 * The demo controls modal (ARCHITECTURE.md §11.4, UI_REFERENCE.md §9). It opens on
 * the first visit: the network in thirty seconds, the cast, and three ways in. Its
 * Controls tab holds the walkthrough switches, the world (reset, replay), and which
 * models run and what this session has spent.
 */
export function DemoControls({ session }: { session: SessionView | null }) {
  const open = useDemoState((state) => state.controlsOpen);
  const tab = useDemoState((state) => state.controlsTab);

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) closeDemoControls();
      }}
      width={580}
      icon={<Spark size={26} />}
      title="The Harmony demo"
      description="Every organization on the network has its own Claude. When one needs something, Claude finds who can help and their representatives work it out, within what each organization chooses to share, asking a person when they should."
      footer={
        <>
          <Button variant="secondary" onClick={closeDemoControls}>
            Close
          </Button>
          <EnterButton entry="suggested" variant="primary">
            Try the suggested request
          </EnterButton>
        </>
      }
    >
      <Segmented<DemoControlsTab>
        label="Demo controls sections"
        value={tab}
        options={TABS}
        onChange={setDemoControlsTab}
        className="mb-5"
      />
      {tab === "overview" ? <Overview session={session} /> : <Controls session={session} />}
    </Modal>
  );
}

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="mb-6 last:mb-0">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] leading-5 font-semibold text-ink">{title}</h3>
        {aside !== undefined && <span className="text-xs text-muted">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

// --- Overview -----------------------------------------------------------------------

const ENTRIES: readonly { entry: DemoEntry; Icon: LucideIcon; title: string; body: string }[] = [
  {
    entry: "suggested",
    Icon: Sparkles,
    title: "Try the suggested request",
    body: "As Alex Chen, ask Claude to find XRD and SEM time for 12 air-sensitive samples.",
  },
  {
    entry: "explore",
    Icon: Compass,
    title: "Explore freely",
    body: "Look around the app as anyone on the network.",
  },
  {
    entry: "network",
    Icon: Waypoints,
    title: "Network view",
    body: "Watch each organization's Claude work your requests, and who needs a human.",
  },
];

function Overview({ session }: { session: SessionView | null }) {
  const enter = useEnterDemo();
  return (
    <>
      <Section title="Start here">
        <ModalList>
          {ENTRIES.map(({ entry, Icon, title, body }) => (
            <button
              key={entry}
              type="button"
              disabled={enter.isPending}
              onClick={() => {
                enter.mutate(entry);
              }}
              className="group flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-hover disabled:opacity-60"
            >
              <Icon size={18} strokeWidth={1.5} className="shrink-0 text-secondary" aria-hidden />
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="text-sm leading-5 font-medium text-ink">{title}</span>
                <span className="text-[13px] leading-[18px] text-muted">{body}</span>
              </span>
              <ArrowRight
                size={16}
                strokeWidth={1.5}
                className="shrink-0 text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-ink"
                aria-hidden
              />
            </button>
          ))}
        </ModalList>
        {enter.error !== null && (
          <p role="alert" className="mt-2 text-[13px] text-danger">
            {enter.error.message}
          </p>
        )}
      </Section>
      <Section title="The cast" aside={session === null ? "Start the demo to switch" : "Click to act as anyone"}>
        <Cast session={session} />
      </Section>
    </>
  );
}

function Cast({ session }: { session: SessionView | null }) {
  const members = useMembers();
  const { switchTo, pending } = useSwitchAccount();
  if (session === null) return <StaticCast />;
  const featured = members.data?.featured ?? [];
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {featured.map((member) => (
        <PersonaCard
          key={member.id}
          member={member}
          active={member.id === session.member.id}
          disabled={pending}
          onSelect={() => {
            if (member.id !== session.member.id) void switchTo(member);
          }}
        />
      ))}
    </div>
  );
}

// Before a session exists there is no member directory to read; introduce the story's
// people from the scenario instead.
const STATIC_CAST: readonly { name: string; seed: string; role: string; line: string }[] = [
  { name: "Alex Chen", seed: "henderson", role: "PhD student · Henderson Lab", line: "Needs XRD and SEM time by Nov 6, and a sealed holder." },
  { name: "Dr. Helen Ward", seed: "northlake", role: "Core director · Northlake", line: "Runs Northlake's scheduling and pricing exceptions." },
  { name: "Raj Patel", seed: "northlake", role: "Staff scientist · Northlake", line: "Runs the instruments and holds a reserve XRD block." },
  { name: "Dana Okafor", seed: "ridgeline", role: "Operations lead · Ridgeline", line: "Quotes sealed holders, given the seal material." },
];

function StaticCast() {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {STATIC_CAST.map((person) => (
        <div key={person.name} className="flex gap-2.5 rounded-control border border-border px-3 py-2.5">
          <Avatar name={person.name} seed={person.seed} size={28} />
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-[13.5px] leading-[18px] font-medium">{person.name}</span>
            <span className="truncate text-xs leading-4 text-muted">{person.role}</span>
            <span className="mt-1 text-xs leading-4 text-secondary">{person.line}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function PersonaCard({
  member,
  active,
  disabled,
  onSelect,
}: {
  member: MemberView;
  active: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  const line = castLine(member);
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={disabled}
      aria-pressed={active}
      className={cx(
        "flex gap-2.5 rounded-control border px-3 py-2.5 text-left transition-colors disabled:opacity-70",
        active ? "border-ink bg-surface" : "border-border hover:border-border-strong hover:bg-hover",
      )}
    >
      <Avatar name={member.name} seed={member.org.slug} size={28} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[13.5px] leading-[18px] font-medium text-ink">{member.name}</span>
          {active && <Check size={14} strokeWidth={2} className="shrink-0 text-ink" aria-label="Acting as" />}
        </span>
        <span className="truncate text-xs leading-4 text-muted">
          {member.title ?? member.role} · {member.org.name}
        </span>
        {line !== null && <span className="mt-1 text-xs leading-4 text-secondary">{line}</span>}
      </span>
    </button>
  );
}

// --- Controls -----------------------------------------------------------------------

function Controls({ session }: { session: SessionView | null }) {
  const showSuggestedMessage = useDemoState((state) => state.showSuggestedMessage);
  const highlightRecipients = useDemoState((state) => state.highlightSuggestedRecipients);
  const unavailable = useUnavailable();
  const worldDate = useWorldDate(session?.world);

  return (
    <>
      <Section title="Walkthrough">
        <ModalList>
          <ToggleRow
            id="demo-suggested-message"
            title="Show the suggested message"
            body="A chip under Alex's composer fills in the story's opening request."
            checked={showSuggestedMessage}
            onChange={(value) => {
              setWalkthroughPref("showSuggestedMessage", value);
            }}
          />
          <ToggleRow
            id="demo-suggested-recipients"
            title="Highlight suggested recipients"
            body="The network view rings the organizations worth watching."
            checked={highlightRecipients}
            onChange={(value) => {
              setWalkthroughPref("highlightSuggestedRecipients", value);
            }}
          />
        </ModalList>
      </Section>

      <Section
        title="World"
        aside={worldDate === null ? undefined : `World date ${formatWorldDate(worldDate)}`}
      >
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            disabled={session === null}
            onClick={() => {
              closeDemoControls();
              openResetConfirm();
            }}
          >
            Reset demo
          </Button>
          <Button
            variant="secondary"
            onClick={() => {
              unavailable("Replaying the big run");
            }}
          >
            Replay the big run
          </Button>
          <Button
            variant="secondary"
            onClick={() => {
              unavailable("Forking a world");
            }}
          >
            Fork this world
          </Button>
        </div>
        <p className="mt-2 text-xs leading-4 text-muted">
          Reset starts a fresh copy of the network. Nothing you do here is shared with other visitors.
        </p>
      </Section>

      <ModelAndSpend session={session} />
    </>
  );
}

/** Which models run where (fixed by the deployment), and this session's spend. Read-only. */
function ModelAndSpend({ session }: { session: SessionView | null }) {
  const budget = useBudget().data;
  const live = budget?.live === true;
  const spend = live ? (budget.session ?? null) : null;
  const line = live ? budgetLine(budget.state) : null;
  return (
    <Section title="Model and spend">
      <p className="text-[13px] leading-5 text-secondary">
        Opus 5.5 for featured organizations&apos; internal Claude, representative and consult answerer; Haiku 5.5
        for background organizations, screening and the sharing check.
      </p>
      {spend !== null && <Spend spend={spend} />}
      {line !== null && <p className="mt-2 text-xs leading-4 font-medium text-warning">{line}</p>}
      <p className="mt-2 text-xs leading-4 text-muted">
        {session === null
          ? "Each session gets its own live-model budget once the demo starts."
          : budget === undefined
            ? "Loading this session's spend…"
            : budget.live
              ? "Every session has its own live-model budget. Reset doesn't refill it."
              : "Spend caps are off in this deployment, so nothing is metered here."}
      </p>
    </Section>
  );
}

const usd = (amount: number) => `$${amount.toFixed(2)}`;

/** This session's spend, laid out like a row of Claude's Settings → Usage. */
function Spend({ spend }: { spend: NonNullable<BudgetView["session"]> }) {
  const used = spend.cap_usd > 0 ? Math.min(1, (spend.spent_usd + spend.reserved_usd) / spend.cap_usd) : 1;
  return (
    <div className="mt-3">
      <div className="flex items-center gap-4">
        <div className="w-[136px] shrink-0">
          <p className="text-sm leading-5 text-ink">This session</p>
          <p className="text-xs leading-4 text-muted tabular-nums">
            {usd(spend.remaining_usd)} left of {usd(spend.cap_usd)}
          </p>
        </div>
        <div
          role="progressbar"
          aria-label="This session's live-model budget used"
          aria-valuemin={0}
          aria-valuemax={spend.cap_usd}
          aria-valuenow={Math.min(spend.cap_usd, spend.spent_usd + spend.reserved_usd)}
          aria-valuetext={`${usd(spend.spent_usd)} spent, ${usd(spend.remaining_usd)} left`}
          className="h-2 min-w-0 flex-1 overflow-hidden rounded-full border border-border-strong bg-surface"
        >
          <div className="h-full rounded-full bg-toggle" style={{ width: `${String(used * 100)}%` }} />
        </div>
        <span className="shrink-0 text-[13px] leading-5 text-secondary tabular-nums">{usd(spend.spent_usd)} spent</span>
      </div>
      {spend.reserved_usd > 0 && (
        <p className="mt-1.5 text-xs leading-4 text-muted">
          {usd(spend.reserved_usd)} is held for calls in progress until they finish.
        </p>
      )}
    </div>
  );
}

function ToggleRow({
  id,
  title,
  body,
  checked,
  onChange,
}: {
  id: string;
  title: string;
  body: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center gap-4 px-4 py-3">
      <label htmlFor={id} className="flex min-w-0 flex-1 cursor-pointer flex-col gap-0.5">
        <span className="text-sm leading-5 font-medium text-ink">{title}</span>
        <span className="text-[13px] leading-[18px] text-muted">{body}</span>
      </label>
      <Toggle id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function EnterButton({
  entry,
  variant,
  children,
}: {
  entry: DemoEntry;
  variant: "primary" | "secondary";
  children: ReactNode;
}) {
  const enter = useEnterDemo();
  return (
    <Button
      variant={variant}
      disabled={enter.isPending}
      onClick={() => {
        enter.mutate(entry);
      }}
    >
      {enter.isPending ? "Starting…" : children}
    </Button>
  );
}
