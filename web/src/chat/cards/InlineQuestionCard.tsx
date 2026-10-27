import { Check, ChevronDown, CircleAlert, MessageCircleQuestion } from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router";

import { clockTime, possessive } from "../../requests/format";
import { firstName, shortOrgName } from "../../shell/names";
import { Button } from "../../shell/ui/Button";
import { cx } from "../../shell/ui/cx";
import { OrgAvatar, Pill, type PillTone } from "../parts";
import type { InlineQuestionItem, OrgRef, QuestionDelivery, ResolveEscalation } from "../types";

export interface InlineQuestionCardProps {
  item: InlineQuestionItem;
  /** Our organization's name: "within what Henderson Lab shares". */
  us: string;
  busy: boolean;
  /** Why live turns can't start now (`budgetLine`); the answer holds until it clears. */
  blocked: string | null;
  error: string | null;
  onAnswer: (body: ResolveEscalation) => void;
}

/** How a settled item reads: what the member did, and what the words under it are. */
interface Settled {
  pill: string;
  tone: PillTone;
  caption: string | null;
}

const SETTLED: Record<NonNullable<InlineQuestionItem["action"]>, Settled> = {
  approve: { pill: "Answered", tone: "success", caption: null },
  decline: { pill: "Declined", tone: "muted", caption: "Your note for the representative" },
  take_over: { pill: "You replied directly", tone: "success", caption: "What you sent" },
};

/**
 * A question the member's own representative routed to them, in the chat the request
 * came from: a counterparty's question the brief doesn't answer, or terms to agree to.
 * When other conversations on the request ask the same thing, they join this card as
 * its askers, and one answer serves them all: the shown asker gets the member's words
 * as written, each other ticked asker a version fitted to its question and checked for
 * that organization. Later askers on the request get it the same way, if ticked.
 *
 * The answer hands the move to the representative, whose turn a spent cap or a pause
 * would refuse, ending the thread for good: while the budget isn't `ok` it holds, and
 * says why, and the thread waits on the member as it did (LIVE_DEMO_PLAN 3.4).
 */
export function InlineQuestionCard({ item, us, busy, blocked, error, onAnswer }: InlineQuestionCardProps) {
  const titleId = useId();
  const open = item.status === "open";
  const settled = item.status === "resolved" && item.action !== null ? SETTLED[item.action] : null;
  const caption = captionOf(item, settled);
  const group = [item.from_org, ...item.askers.map((asker) => asker.from_org)];

  return (
    <section aria-labelledby={titleId} className="rounded-menu border border-border bg-surface shadow-card">
      <header className="flex items-center gap-2.5 px-4 pt-3.5">
        <span className="flex shrink-0">
          {group.map((org, index) => (
            <span key={org.id} className={cx(index > 0 && "-ml-1.5 rounded-[7px] ring-2 ring-surface")}>
              <OrgAvatar org={org} size={22} />
            </span>
          ))}
        </span>
        <p id={titleId} className="min-w-0 flex-1 truncate text-[13.5px] text-muted">
          {group.length > 1 ? (
            <>
              <span className="font-medium text-ink">Same question from {group.length}</span> ·{" "}
              {group.map((org) => shortOrgName(org.name)).join(", ")}
            </>
          ) : (
            <>
              Your representative asks about <span className="text-secondary">{item.from_org.name}</span>
            </>
          )}
        </p>
        {open ? (
          <Pill tone="accent" icon={<MessageCircleQuestion size={12} strokeWidth={2} />}>
            Needs your answer
          </Pill>
        ) : settled !== null ? (
          <Pill tone={settled.tone}>{settled.pill}</Pill>
        ) : item.status === "resolved" ? (
          <Pill tone="muted">Resolved</Pill>
        ) : (
          <Pill tone="muted">No longer needed</Pill>
        )}
      </header>
      <p className="px-4 pt-2.5 font-serif text-[16.5px] leading-[1.5] text-ink">{item.question}</p>
      {open ? (
        <AnswerBox item={item} us={us} busy={busy} blocked={blocked} error={error} onAnswer={onAnswer} />
      ) : (
        <div className="px-4 pt-2.5 pb-4">
          {item.answer !== null && (
            <>
              {caption !== null && <p className="pb-1 text-[12.5px] text-muted">{caption}</p>}
              <p className="rounded-control bg-bubble px-3 py-2 text-[14px] leading-[1.5] whitespace-pre-wrap text-secondary">
                {item.answer}
              </p>
            </>
          )}
          {item.action === "approve" && item.goes_to_org_ids.length > 0 && (
            <p className="mt-2.5 text-[12.5px] leading-5 text-muted">
              Answer goes to:{" "}
              <span className="text-secondary">{goesTo(item.recipients, new Set(item.goes_to_org_ids))}</span>
            </p>
          )}
          {item.deliveries.length > 0 && (
            <ul aria-label="Where your answer went" className="mt-3 border-t border-border">
              {item.deliveries.map((delivery) => (
                <DeliveryRow key={delivery.thread_id} delivery={delivery} us={us} />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function AnswerBox({ item, us, busy, blocked, error, onAnswer }: InlineQuestionCardProps) {
  const [answer, setAnswer] = useState("");
  const [showAsks, setShowAsks] = useState(true);
  const [picking, setPicking] = useState(false);
  // Every organization on the request starts ticked: "anyone on this request who asks".
  const [ticked, setTicked] = useState<ReadonlySet<string>>(() => new Set(item.recipients.map((org) => org.id)));

  const asked = new Set([item.from_org.id, ...item.askers.map((asker) => asker.from_org.id)]);
  // The other askers still waiting and ticked get a version fitted to their question.
  const fitted = item.askers.filter((asker) => asker.status === "open" && ticked.has(asker.from_org.id));
  // The shown asker gets the words as written, so they mustn't name anyone else on the request.
  const kept = [item.from_org.name, us];
  const named = item.recipients.filter((org) => org.id !== item.from_org.id && namesOrg(answer, org.name, kept));
  const ready = answer.trim() !== "" && !busy && blocked === null && named.length === 0;

  const submit = () => {
    if (!ready) return;
    onAnswer({
      action: "approve",
      text: answer.trim(),
      also_for: fitted.map((asker) => asker.escalation_id),
      goes_to_org_ids: item.recipients.filter((org) => ticked.has(org.id)).map((org) => org.id),
    });
  };
  const toggle = (orgId: string) => {
    setTicked((current) => {
      const next = new Set(current);
      if (next.has(orgId)) next.delete(orgId);
      else next.add(orgId);
      return next;
    });
  };

  const hint =
    named.length > 0
      ? namesHint(
          named.map((org) => shortOrgName(org.name)),
          shortOrgName(item.from_org.name),
        )
      : fittedHint(item.from_org, fitted.map((asker) => asker.from_org));

  return (
    <>
      {item.askers.length > 0 && (
        <div className="px-4 pt-2 text-[13px] leading-[18px] text-muted">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span>
              Also asked by <span className="text-secondary">{listed(item.askers.map((asker) => asker.from_org.name))}</span>
            </span>
            <button
              type="button"
              aria-expanded={showAsks}
              onClick={() => {
                setShowAsks((value) => !value);
              }}
              className="inline-flex items-center gap-0.5 whitespace-nowrap text-secondary hover:text-ink"
            >
              <ChevronDown size={14} strokeWidth={1.75} aria-hidden className={cx(showAsks && "rotate-180")} />
              How each asked
            </button>
          </p>
          {showAsks && (
            <ul className="mt-2 flex flex-col gap-1.5 border-l-2 border-border-strong py-0.5 pl-3 text-secondary">
              {item.askers.map((asker) => (
                <li key={asker.escalation_id}>
                  <span className="font-medium text-ink">{asker.from_org.name}</span>: {asker.question}
                  {asker.status === "cancelled" && <span className="text-muted"> · Conversation ended</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="px-4 pt-3 pb-4">
        <textarea
          rows={2}
          value={answer}
          disabled={busy}
          placeholder="Your answer…"
          aria-label={`Your answer about ${item.from_org.name}`}
          onChange={(event) => {
            setAnswer(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              submit();
            }
          }}
          className="block min-h-[64px] w-full resize-y rounded-control border border-border-strong bg-surface px-3 py-2 text-[14.5px] leading-[1.5] text-ink outline-none transition-colors placeholder:text-faint focus:border-accent"
        />
        <div className="mt-2.5 flex items-center gap-3">
          {blocked === null ? (
            <p className="min-w-0 flex-1 text-[12.5px] leading-5 text-muted">
              Answer goes to: <span className="text-secondary">{goesTo(item.recipients, ticked)}</span> ·{" "}
              <button
                type="button"
                aria-expanded={picking}
                onClick={() => {
                  setPicking((value) => !value);
                }}
                className={cx("whitespace-nowrap text-link", picking ? "underline" : "hover:underline")}
              >
                Change
              </button>
            </p>
          ) : (
            <p role="status" className="flex min-w-0 flex-1 items-center gap-2 text-[13px] leading-5 text-secondary">
              <CircleAlert size={14} strokeWidth={1.7} className="shrink-0 text-warning" aria-hidden />
              <span className="min-w-0">{blocked}</span>
            </p>
          )}
          <Button variant="primary" disabled={!ready} onClick={submit}>
            {busy ? "Sending…" : "Send answer"}
          </Button>
        </div>
        {picking && blocked === null && (
          <fieldset className="mt-2.5 rounded-[10px] border border-border-strong bg-canvas p-1.5">
            <legend className="float-left w-full px-2 pt-1 pb-1 text-ui-xs text-muted">Answer goes to</legend>
            {item.recipients.map((org) => (
              <label
                key={org.id}
                className="flex min-w-0 cursor-pointer items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-[13.5px] leading-5 hover:bg-hover"
              >
                <input
                  type="checkbox"
                  checked={ticked.has(org.id)}
                  // The question shown is the one being answered.
                  disabled={org.id === item.from_org.id}
                  onChange={() => {
                    toggle(org.id);
                  }}
                  className="size-4 shrink-0 accent-ink"
                />
                <OrgAvatar org={org} size={22} />
                <span className="min-w-0 flex-1 truncate text-ink">{org.name}</span>
                <span className="text-ui-xs whitespace-nowrap text-muted">{asked.has(org.id) ? "asked" : "if it asks"}</span>
              </label>
            ))}
            <p className="mt-1 border-t border-border px-2 pt-2 pb-1 text-ui-xs text-muted">
              Only the ones that ask get it, now or later on this request. Each answer first passes {possessive(us)}{" "}
              checks for that organization. Untick one and it never gets it.
            </p>
          </fieldset>
        )}
        {hint !== null && blocked === null && (
          <p
            role={named.length > 0 ? "alert" : undefined}
            className={cx("mt-1.5 text-right text-ui-xs", named.length > 0 ? "text-warning" : "text-muted")}
          >
            {hint}
          </p>
        )}
        {error !== null && (
          <p role="alert" className="mt-2 text-[13px] text-danger">
            {error}
          </p>
        )}
      </div>
    </>
  );
}

function DeliveryRow({ delivery, us }: { delivery: QuestionDelivery; us: string }) {
  const name = shortOrgName(delivery.org.name);
  const at = delivery.at === null ? "" : ` · ${clockTime(delivery.at)}`;
  const within = `within what ${us} shares`;
  const line: Record<QuestionDelivery["state"], string> = {
    as_written: `Sent as you wrote it${at}`,
    fitted: `Sent · fitted to ${possessive(name)} question, ${within}${at}`,
    later: `Used for ${name}${at} · asked later, fitted to ${possessive(name)} question, ${within}`,
    drafting: `Fitting your answer to ${possessive(name)} question…`,
    held: "Held for review",
    not_sent: "Not sent",
    ended: "Conversation ended",
  };
  const sent = delivery.state === "as_written" || delivery.state === "fitted" || delivery.state === "later";
  const review = delivery.state === "held" || delivery.state === "not_sent";
  return (
    <li className="grid grid-cols-[22px_minmax(0,1fr)_auto] items-start gap-x-2.5 border-b border-dashed border-border py-2.5 last:border-b-0">
      <OrgAvatar org={delivery.org} size={22} />
      <div className="min-w-0">
        <p className="truncate text-[14px] leading-5 text-ink">{delivery.org.name}</p>
        <p className={cx("text-[13px] leading-[18px]", delivery.state === "held" ? "text-warning" : "text-muted")}>
          {line[delivery.state]}
        </p>
        {delivery.state === "held" && delivery.reason !== null && (
          <p className="mt-0.5 text-[13px] leading-[18px] text-secondary">{delivery.reason}</p>
        )}
      </div>
      {sent && <Check size={16} strokeWidth={2} aria-hidden className="mt-0.5 text-success" />}
      {review && (
        <Link
          to={`/requests/${delivery.thread_id}`}
          aria-label={`Review ${delivery.org.name}`}
          className="inline-flex h-7 items-center rounded-control border border-border-strong bg-surface px-2.5 text-[13px] font-medium text-ink transition-colors hover:bg-hover"
        >
          Review
        </Link>
      )}
    </li>
  );
}

function captionOf(item: InlineQuestionItem, settled: Settled | null): string | null {
  if (item.action === "approve" && item.answered_by !== null && item.answered_at !== null) {
    return `Your answer · ${firstName(item.answered_by.name)}, ${clockTime(item.answered_at)}`;
  }
  return settled?.caption ?? null;
}

/** "anyone on this request who asks", less the organizations unticked. */
function goesTo(recipients: readonly OrgRef[], ticked: ReadonlySet<string>): string {
  const left = recipients.filter((org) => !ticked.has(org.id)).map((org) => shortOrgName(org.name));
  return left.length === 0 ? "anyone on this request who asks" : `anyone on this request who asks, except ${listed(left)}`;
}

/** "Northlake gets your words as written. Kestrel and Aurora get them fitted to their question." */
function fittedHint(shown: OrgRef, fitted: readonly OrgRef[]): string | null {
  if (fitted.length === 0) return null;
  const others = listed(fitted.map((org) => shortOrgName(org.name)));
  const verb = fitted.length === 1 ? "gets them fitted to its question" : "get them fitted to their question";
  return `${shortOrgName(shown.name)} gets your words as written. ${others} ${verb}.`;
}

/** The shown asker gets the words as written, so no other organization's name may be in them. */
function namesHint(names: readonly string[], shown: string): string {
  const leave = names.length === 1 ? "Leave the name out." : "Leave the names out.";
  return `Your answer names ${listed(names)}, and ${shown} gets your words as written. ${leave}`;
}

/** Whether `text` names the org `name`: in full, in any case, on word boundaries, with any
 * whitespace between its words; or by the first word of a longer name as written ("Northlake"),
 * unless a name in `kept` starts with it. The server refuses such an answer the same way. */
function namesOrg(text: string, name: string, kept: readonly string[]): boolean {
  const words = name.trim().split(/\s+/).filter((word) => word !== "");
  const [first] = words;
  if (first === undefined) return false;
  if (wordsIn(text, words, "iu")) return true;
  const short =
    words.length > 1 &&
    /^\p{Lu}/u.test(first) &&
    !COMMON_FIRST_WORDS.has(first) &&
    !kept.some((other) => other.trim().split(/\s+/)[0] === first);
  return short && wordsIn(text, [first], "u");
}

/** Capitalised first words too common to stand for an organization on their own. */
const COMMON_FIRST_WORDS = new Set(["A", "An", "The"]);

function wordsIn(text: string, words: readonly string[], flags: string): boolean {
  const pattern = words.map((word) => word.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")).join("\\s+");
  return new RegExp(`(?<![\\p{L}\\p{N}_])${pattern}(?![\\p{L}\\p{N}_])`, flags).test(text);
}

/** "A", "A and B", "A, B and C". */
function listed(names: readonly string[]): string {
  return names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names.at(-1) ?? ""}`;
}
