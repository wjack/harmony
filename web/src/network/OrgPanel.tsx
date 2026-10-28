import { ArrowUpRight, Hourglass, X } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import type { MemberView } from "../api/types";
import type { BudgetState } from "../app/budget";
import { firstName } from "../shell/names";
import { Avatar, Button, IconButton, OrgAvatar, cx } from "../shell/ui";
import { formatAgo, formatWait, kindLabel, plural, shortOrgName } from "./format";
import { StatusPill } from "./StatusPill";
import { TONE_STROKE, closingLine, edgeStyle, threadStatus } from "./status";
import type { NetworkEdge, NetworkNode, ParticipantStatus, WorldLens } from "./types";
import type { Wait, WaitAction } from "./waiting";

const ROLE_LABELS: Record<NetworkNode["role"], string> = {
  requester: "Requester",
  recipient: "Recipient",
  bystander: "Not contacted",
};

const FIT_LABELS: Record<NonNullable<NetworkNode["fit"]>, string> = {
  strong: "Strong",
  possible: "Possible",
  no: "Not a fit",
  unknown: "Unknown",
};

/** An org on the canvas, as a thread row names it and states its status. */
export interface CanvasOrg {
  name: string;
  slug: string;
  participant: ParticipantStatus | null;
}

export interface OrgPanelProps {
  node: NetworkNode;
  /** The focused request's threads this org is on. */
  threads: NetworkEdge[];
  /** Every org on the canvas: the other side of each thread, and its recipient's status. */
  orgs: ReadonlyMap<string, CanvasOrg>;
  world: WorldLens;
  /** The acting member's org; its own threads open in Requests. */
  homeOrgId: string | null;
  /** Who this org's thread waits on, and what answering it takes (waiting.ts). */
  wait: Wait | null;
  action: WaitAction | null;
  /** Act as `member`, then open `to` (desk world only). */
  onActAs?: (member: MemberView, to: string) => void;
  actingAs?: boolean;
  /** The org's members, each with Act as: the demo's account switcher (desk world only). */
  people?: readonly MemberView[];
  /** Who the session acts as now. */
  actorId?: string | null;
  onClose: () => void;
  now: number;
  /** The live-model budget's state now, which a stopped thread's line reads by. */
  budget: BudgetState;
}

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-[12px] border border-border bg-surface">
      <header className="flex h-10 items-center justify-between border-b border-border px-3.5">
        <h3 className="text-[13px] font-semibold text-ink">{title}</h3>
        {aside !== undefined && <span className="text-xs text-muted">{aside}</span>}
      </header>
      {children}
    </section>
  );
}

function Property({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center gap-3 px-3.5 py-1.5">
      <dt className="w-[108px] shrink-0 text-[13px] text-muted">{label}</dt>
      <dd className="min-w-0 flex-1 truncate text-[13px] text-ink">{children}</dd>
    </div>
  );
}

function ThreadRow({
  thread,
  counterpart,
  parties,
  recipient,
  openable,
  now,
  budget,
}: {
  thread: NetworkEdge;
  counterpart: CanvasOrg;
  /** The thread's two orgs' names, requester first, as `edgeLabel` takes them. */
  parties: readonly string[];
  /** The thread's recipient's status, in the words its pill uses. */
  recipient: ParticipantStatus | null;
  openable: boolean;
  now: number;
  budget: BudgetState;
}) {
  const status = threadStatus(thread.state, recipient);
  // An ended thread shows our own closing summary if we wrote one, else why it ended; a
  // live one, its newest line.
  const line = edgeStyle(thread.state).ended ? closingLine(thread, parties, budget) : thread.latest_line;
  const body = (
    <>
      <OrgAvatar name={counterpart.name} seed={counterpart.slug} size={24} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-ink">{counterpart.name}</span>
          {openable && <ArrowUpRight size={13} strokeWidth={1.75} className="shrink-0 text-muted" aria-hidden />}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
          <span aria-hidden className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: TONE_STROKE[status.tone] }} />
          <span className="truncate">
            {status.label}
            {thread.turn_count !== null && ` · ${plural(thread.turn_count, "turn")}`}
          </span>
          <span className="ml-auto shrink-0 tabular-nums">{formatAgo(thread.last_activity_at, now)}</span>
        </div>
        {line !== null && <p className="mt-1 line-clamp-2 text-xs leading-[18px] text-secondary">{line}</p>}
      </div>
    </>
  );
  const row = "flex items-start gap-2.5 px-3.5 py-2.5";
  return (
    <li className="border-b border-border last:border-b-0">
      {openable ? (
        <Link to={`/requests/${thread.thread_id}`} className={cx(row, "transition-colors hover:bg-hover")}>
          {body}
        </Link>
      ) : (
        <div className={row}>{body}</div>
      )}
    </li>
  );
}

function WaitTitle({ wait }: { wait: Wait }) {
  if (wait.isYou) return <>Waiting on you</>;
  if (wait.side === "unknown") return <>Waiting on a person</>;
  if (wait.member === null) return <>Waiting on {shortOrgName(wait.org.name)}&rsquo;s team</>;
  return (
    <>
      Waiting on {wait.member.name}
      {wait.side === "requester" && <span className="font-normal text-muted"> · {shortOrgName(wait.org.name)}</span>}
    </>
  );
}

/** Why the thread is held, from the side that holds it. */
function waitStory(wait: Wait, node: NetworkNode, action: WaitAction | null): string {
  const holder = shortOrgName(wait.org.name);
  const counterpart = shortOrgName(node.org.name);
  if (wait.side === "unknown") {
    return `One side of ${counterpart}'s thread is waiting on a person's answer. You're on neither side, so which one, and who, isn't yours to see.`;
  }
  if (wait.side === "requester") {
    if (wait.isYou) return `Your representative needs an answer from you before it can reply to ${counterpart}.`;
    if (wait.member !== null) {
      return `${holder}'s representative asked ${firstName(wait.member.name)} before it replies to ${counterpart}.`;
    }
  } else {
    if (wait.isYou) return `${holder}'s representative asked you before it answers.`;
    if (wait.member !== null) {
      return `${holder}'s representative asked its team and waits for ${firstName(wait.member.name)} to answer.`;
    }
  }
  const admin = action?.kind === "act_as" ? `; ${firstName(action.member.name)}, an admin there, can see the thread` : "";
  return `${holder}'s representative is waiting on its team. Who it asked is ${holder}'s to know${admin}.`;
}

function WaitCard({
  wait,
  node,
  action,
  onActAs,
  actingAs,
  now,
}: {
  wait: Wait;
  node: NetworkNode;
  action: WaitAction | null;
  onActAs?: (member: MemberView, to: string) => void;
  actingAs: boolean;
  now: number;
}) {
  return (
    <div className="rounded-[12px] border border-[color-mix(in_srgb,var(--warning)_30%,var(--border))] bg-warning-soft px-3.5 py-3">
      <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
        <Hourglass size={14} strokeWidth={1.75} className="shrink-0 text-warning" aria-hidden />
        <span className="min-w-0 truncate">
          <WaitTitle wait={wait} />
        </span>
        {wait.since !== null && (
          <span className="ml-auto shrink-0 text-xs font-normal text-muted tabular-nums">{formatWait(wait.since, now)}</span>
        )}
      </div>
      <p className="mt-1.5 text-[13px] leading-5 text-secondary">{waitStory(wait, node, action)}</p>
      {action?.kind === "open" && (
        <Link
          to={action.to}
          className="mt-3 inline-flex h-9 w-full items-center justify-center rounded-control bg-primary px-3 text-sm font-medium text-on-primary transition-colors hover:bg-primary-hover"
        >
          {action.label}
        </Link>
      )}
      {action?.kind === "act_as" && onActAs !== undefined && (
        <Button
          variant="primary"
          size="md"
          className="mt-3 w-full"
          disabled={actingAs}
          onClick={() => {
            onActAs(action.member, action.to);
          }}
        >
          {actingAs ? "Switching…" : `Act as ${firstName(action.member.name)}`}
        </Button>
      )}
    </div>
  );
}

/** The side panel for a selected org (Cofounder's inspector): who it is, its threads, who it waits on. */
export function OrgPanel({
  node,
  threads,
  orgs,
  world,
  homeOrgId,
  wait,
  action,
  onActAs,
  actingAs = false,
  people = [],
  actorId = null,
  onClose,
  now,
  budget,
}: OrgPanelProps) {
  const status = node.participant;
  const isHome = node.org.id === homeOrgId;

  return (
    <aside
      aria-label={`${node.org.name} details`}
      className="flex h-full w-[min(340px,33%)] shrink-0 flex-col border-l border-border bg-sidebar"
    >
      <header className="flex items-start gap-3 px-4 pt-4 pb-3">
        <OrgAvatar name={node.org.name} seed={node.org.slug} size={36} />
        <div className="min-w-0 flex-1 pt-0.5">
          <h2 className="text-[15px] leading-5 font-semibold break-words text-ink">{node.org.name}</h2>
          <p className="truncate text-[13px] leading-5 text-muted">
            {kindLabel(node.kind)}
            {isHome && " · Your organization"}
          </p>
        </div>
        <IconButton label="Close panel" size="sm" onClick={onClose}>
          <X size={16} strokeWidth={1.75} />
        </IconButton>
      </header>

      <div className="scrollbar-quiet flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 pb-4">
        {wait !== null && (
          <WaitCard wait={wait} node={node} action={action} onActAs={onActAs} actingAs={actingAs} now={now} />
        )}

        <Section title="Profile">
          <dl className="py-1">
            <Property label="Category">{kindLabel(node.kind)}</Property>
            <Property label="In this request">{ROLE_LABELS[node.role]}</Property>
            {status !== null && (
              <Property label="Status">
                <StatusPill status={status} size="sm" />
              </Property>
            )}
            {node.fit !== null && <Property label="Screener fit">{FIT_LABELS[node.fit]}</Property>}
            {node.featured && <Property label="Directory">Featured organization</Property>}
          </dl>
        </Section>

        {onActAs !== undefined && people.length > 0 && (
          <Section title="People" aside={String(people.length)}>
            <ul>
              {people.map((member) => (
                <li
                  key={member.id}
                  className="flex min-h-12 items-center gap-2.5 px-3.5 py-2 [&+&]:border-t [&+&]:border-border"
                >
                  <Avatar name={member.name} seed={member.org.slug} size={24} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] leading-5 text-ink">{member.name}</span>
                    {member.title !== null && (
                      <span className="block truncate text-xs leading-4 text-muted">{member.title}</span>
                    )}
                  </span>
                  {member.id === actorId ? (
                    <span className="shrink-0 text-xs text-muted">Acting as</span>
                  ) : (
                    <Button
                      size="sm"
                      aria-label={`Act as ${member.name}`}
                      disabled={actingAs}
                      onClick={() => {
                        onActAs(member, "/");
                      }}
                    >
                      Act as
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </Section>
        )}

        <Section title="Threads" aside={threads.length > 0 ? String(threads.length) : undefined}>
          {threads.length === 0 ? (
            <p className="px-3.5 py-3 text-[13px] leading-5 text-muted">
              {node.role === "bystander"
                ? "Not part of this request. Claude reads each organization's public summary when it screens the network."
                : "No thread yet."}
            </p>
          ) : (
            <ul>
              {threads.map((thread) => {
                const otherId = thread.from_org_id === node.org.id ? thread.to_org_id : thread.from_org_id;
                const counterpart = orgs.get(otherId) ?? {
                  name: "Unknown organization",
                  slug: otherId,
                  participant: null,
                };
                const ours = thread.from_org_id === homeOrgId || thread.to_org_id === homeOrgId;
                return (
                  <ThreadRow
                    key={thread.thread_id}
                    thread={thread}
                    counterpart={counterpart}
                    parties={[thread.from_org_id, thread.to_org_id].flatMap((orgId) => orgs.get(orgId)?.name ?? [])}
                    recipient={orgs.get(thread.to_org_id)?.participant ?? null}
                    openable={world === "desk" && ours}
                    now={now}
                    budget={budget}
                  />
                );
              })}
            </ul>
          )}
        </Section>

      </div>
    </aside>
  );
}
