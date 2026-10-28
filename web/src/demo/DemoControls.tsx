import type { ReactNode } from "react";

import type { SessionView } from "../api/types";
import { useBudget, type BudgetView } from "../app/budget";
import { budgetLine } from "../chat/failureCopy";
import { Button, Modal, ModalList, Spark, Toggle } from "../shell/ui";
import { useUnavailable } from "../shell/unavailable";
import { useEnterDemo } from "./accounts";
import { useSetSkipApprovals } from "./approvals";
import { closeDemoControls, openResetConfirm, setWalkthroughPref, useDemoState } from "./store";
import { formatWorldDate, useWorldDate } from "./worldClock";

/**
 * The demo controls modal (ARCHITECTURE.md §11.4, UI_REFERENCE.md §9). It opens on the
 * first visit, and from the demo bar and the account menu: the walkthrough switches, Skip
 * approvals, the world (reset, replay), and which models run and what this session has
 * spent. "Try the suggested request" starts the demo, or switches back to the story's
 * member.
 */
export function DemoControls({ session }: { session: SessionView | null }) {
  const open = useDemoState((state) => state.controlsOpen);

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) closeDemoControls();
      }}
      width={580}
      icon={<Spark size={26} />}
      title="Demo controls"
      description="Every organization on the network has its own Claude. When one needs something, Claude finds out who can help, and their two Claude representatives look to collaborate."
      footer={
        <>
          <Button variant="secondary" onClick={closeDemoControls}>
            Close
          </Button>
          <EnterButton variant="primary">Try the suggested request</EnterButton>
        </>
      }
    >
      <Controls session={session} />
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

// --- Controls -----------------------------------------------------------------------

function Controls({ session }: { session: SessionView | null }) {
  const showSuggestedMessage = useDemoState((state) => state.showSuggestedMessage);
  const highlightRecipients = useDemoState((state) => state.highlightSuggestedRecipients);
  const unavailable = useUnavailable();
  const worldDate = useWorldDate(session?.world);
  const skipApprovals = useSetSkipApprovals();

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

      <Section title="Approvals">
        <ModalList>
          <ToggleRow
            id="demo-skip-approvals"
            title="Dangerously skip approvals"
            body="Nobody in this world is asked: every organization's Claude sends its answers, messages and requests as it writes them. The sharing check still runs, and an answer it stops is declined. Items already waiting still wait."
            checked={session?.world.skip_approvals ?? false}
            disabled={session === null}
            onChange={(value) => {
              skipApprovals.mutate(value);
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
  disabled,
  onChange,
}: {
  id: string;
  title: string;
  body: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center gap-4 px-4 py-3">
      <label htmlFor={id} className="flex min-w-0 flex-1 cursor-pointer flex-col gap-0.5">
        <span className="text-sm leading-5 font-medium text-ink">{title}</span>
        <span className="text-[13px] leading-[18px] text-muted">{body}</span>
      </label>
      <Toggle id={id} checked={checked} disabled={disabled} onCheckedChange={onChange} />
    </div>
  );
}

function EnterButton({ variant, children }: { variant: "primary" | "secondary"; children: ReactNode }) {
  const enter = useEnterDemo();
  return (
    <Button
      variant={variant}
      disabled={enter.isPending}
      onClick={() => {
        enter.mutate();
      }}
    >
      {enter.isPending ? "Starting…" : children}
    </Button>
  );
}
