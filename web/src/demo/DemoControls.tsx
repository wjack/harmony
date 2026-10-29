import type { ReactNode } from "react";

import type { SessionView } from "../api/types";
import { useBudget } from "../app/budget";
import { budgetLine } from "../chat/failureCopy";
import { Button, Modal, ModalList, Spark, Toggle } from "../shell/ui";
import { useUnavailable } from "../shell/unavailable";
import { useEnterDemo } from "./accounts";
import { useSetSkipApprovals } from "./approvals";
import { closeDemoControls, openResetConfirm, seeUserGuide, useDemoState } from "./store";
import { formatWorldDate, useWorldDate } from "./worldClock";

/**
 * The demo controls modal (ARCHITECTURE.md §11.4, UI_REFERENCE.md §9). It opens from
 * the demo bar, the account menu and the landing's Start: the user guide ("What's on
 * screen", HelpOverlay.tsx), Skip approvals, the world (reset, replay), and which models
 * run. "Try the suggested request" starts the demo, or switches back to the story's
 * member. (A first visit opens the intro instead, Intro.tsx.)
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
  const unavailable = useUnavailable();
  const worldDate = useWorldDate(session?.world);
  const skipApprovals = useSetSkipApprovals();

  return (
    <>
      <Section title="Walkthrough">
        <Button variant="secondary" onClick={seeUserGuide}>
          See user guide
        </Button>
        <p className="mt-2 text-xs leading-4 text-muted">
          Points out what the demo's key controls do, as the ? in the demo bar does.
        </p>
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

      <Models />
    </>
  );
}

/** Which models run where (fixed by the deployment), and what stops them now. Read-only. */
function Models() {
  const budget = useBudget().data;
  const line = budget?.live === true ? budgetLine(budget.state) : null;
  return (
    <Section title="Models">
      <p className="text-[13px] leading-5 text-secondary">
        Opus 5.5 for featured organizations&apos; internal Claude, representative and consult answerer; Haiku 5.5
        for background organizations, screening and the sharing check.
      </p>
      {line !== null && <p className="mt-2 text-xs leading-4 font-medium text-warning">{line}</p>}
      {budget?.live === false && (
        <p className="mt-2 text-xs leading-4 text-muted">
          Live models are off in this deployment, so Claude&apos;s replies are scripted.
        </p>
      )}
    </Section>
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
