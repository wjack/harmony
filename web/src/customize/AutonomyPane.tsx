import { ArrowRight, SendHorizontal, TriangleAlert, UserCheck } from "lucide-react";

import { Button, OrgAvatar } from "../shell/ui";
import { byName, useCounterparties } from "./counterparties";
import { Callout, Count, Section, Tag } from "./fields";
import { MODE_INFO, MODES } from "./modes";
import type { PaneProps } from "./pane";
import { RadioList } from "./RadioList";
import type { Autonomy, OrgRef } from "./types";

type SendMode = "review" | "send";

const SEND_MODES = [
  {
    value: "review",
    label: "Review before sending",
    description: "Claude shows the brief and the organizations it chose. Nothing goes out until you approve.",
    icon: <UserCheck size={15} strokeWidth={1.75} aria-hidden />,
    tag: <Tag>Recommended</Tag>,
  },
  {
    value: "send",
    label: "Send without review",
    description:
      "Claude sends requests as soon as it writes them, to the organizations it chose. Nobody checks what is asked or who is asked. Spend limits still apply.",
    icon: <SendHorizontal size={15} strokeWidth={1.75} aria-hidden />,
  },
] as const;

/**
 * Autonomy: the default mode, which trusted organizations have their own (§4.4, §9),
 * and whether requests internal Claude writes go out without a member's review
 * (LIVE_DEMO_PLAN 3.10). Its nav row stays one of the five sections.
 */
export function AutonomyPane({ view, draft, edit, editable, onSection }: PaneProps) {
  const orgs = useCounterparties(view);
  const own = Object.entries(draft.trusted)
    .flatMap(([orgId, row]): { org: OrgRef; mode: Autonomy }[] => {
      const org = orgs.get(orgId);
      return row.autonomy === null || org === undefined ? [] : [{ org, mode: row.autonomy }];
    })
    .sort((a, b) => byName(a.org, b.org));

  return (
    <>
      <Section title="Default mode" description="How your representative works with every organization you haven't set apart.">
        <RadioList
          name="default-mode"
          label="Default mode"
          value={draft.autonomy}
          options={MODES.map(({ value, label, description, Icon, recommended }) => ({
            value,
            label,
            description,
            icon: <Icon size={15} strokeWidth={1.75} aria-hidden />,
            tag: recommended ? <Tag>Recommended</Tag> : undefined,
          }))}
          onChange={(autonomy) => {
            edit({ autonomy });
          }}
          disabled={!editable}
        />
        {draft.autonomy === "skip" && (
          <Callout tone="warning" icon={<TriangleAlert size={15} strokeWidth={1.75} />} className="mt-4">
            <p className="font-medium">Answers go out without a sharing check.</p>
            <p className="text-secondary">
              With every organization, Claude passes on what it judges fine to share, and a teammate sees only what it
              says needs a person. To trust one partner this far, set Skip for them under Sharing instead.
            </p>
          </Callout>
        )}
      </Section>

      <Section
        title={
          <span className="inline-flex items-center gap-2">
            With their own mode <Count>{own.length}</Count>
          </span>
        }
        description="A trusted organization can have its own mode. You set it with what you share with them."
        aside={
          <Button
            size="sm"
            icon={<ArrowRight size={14} strokeWidth={1.75} />}
            onClick={() => {
              onSection("sharing");
            }}
          >
            Open Sharing
          </Button>
        }
      >
        {own.length === 0 ? (
          <p className="text-[13px] leading-5 text-muted">
            No trusted organization has its own mode, so every organization gets the default.
          </p>
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-menu border border-border bg-surface">
            {own.map(({ org, mode }) => {
              const { Icon, label } = MODE_INFO[mode];
              return (
                <li key={org.id} className="flex min-h-11 items-center gap-3 px-4 py-2">
                  <OrgAvatar name={org.name} seed={org.slug} size={24} />
                  <span className="min-w-0 flex-1 truncate text-sm text-ink">{org.name}</span>
                  <span className="inline-flex items-center gap-1.5 text-[13px] text-secondary">
                    <Icon size={15} strokeWidth={1.75} aria-hidden />
                    {label}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <Section
        title="Sending Harmony Network Requests"
        description="For requests Claude sends to other organizations for your members. It doesn't change how your representative answers them."
      >
        <RadioList<SendMode>
          name="send-mode"
          label="Sending Harmony Network Requests"
          value={draft.sendWithoutConfirmation ? "send" : "review"}
          options={SEND_MODES}
          onChange={(mode) => {
            edit({ sendWithoutConfirmation: mode === "send" });
          }}
          disabled={!editable}
        />
        {draft.sendWithoutConfirmation && (
          <Callout tone="warning" icon={<TriangleAlert size={15} strokeWidth={1.75} />} className="mt-4">
            <p className="font-medium">Requests go out before anyone reads them.</p>
            <p className="text-secondary">
              Claude sends only to organizations its search rated a strong or possible fit, at most eight per request.
              What each one is asked, and what it&apos;s told about your members, is up to Claude.
            </p>
          </Callout>
        )}
        <p className="mt-3 text-[13px] leading-5 text-muted">
          Members can change this for one chat from the composer&apos;s + menu.
        </p>
      </Section>
    </>
  );
}
