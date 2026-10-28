import { TriangleAlert } from "lucide-react";

import { Menu, MenuCheckItem, MenuContent, MenuGroup, MenuLabel, MenuSeparator, MenuTrigger } from "../shell/ui/Menu";
import type { SendMode } from "./types";

/** A send mode as the composer shows it: a new chat's `org_default` is null until it loads. */
export type ShownSendMode = Omit<SendMode, "org_default"> & { org_default: boolean | null };

export interface SendModeProps {
  /** How this chat sends Harmony Network Requests (LIVE_DEMO_PLAN 3.10). */
  sendMode: ShownSendMode;
  /** `true` or `false` overrides the organization's default; `null` follows it. */
  onSendModeChange: (override: boolean | null) => void;
}

function modeName(withoutReview: boolean): string {
  return withoutReview ? "Send without review" : "Review before sending";
}

/** The three choices, in the `+` menu's Harmony submenu and under the chip. */
export function SendModeItems({ sendMode, onSendModeChange }: SendModeProps) {
  const choices: { override: boolean | null; label: string }[] = [
    {
      override: null,
      label:
        sendMode.org_default === null
          ? "Organization default"
          : `Organization default · ${modeName(sendMode.org_default)}`,
    },
    { override: false, label: modeName(false) },
    { override: true, label: modeName(true) },
  ];
  return (
    <MenuGroup>
      <MenuLabel>Sending Harmony Network Requests</MenuLabel>
      {choices.map(({ override, label }) => (
        <MenuCheckItem
          key={label}
          checked={sendMode.override === override}
          onSelect={() => {
            onSendModeChange(override);
          }}
        >
          {label}
        </MenuCheckItem>
      ))}
    </MenuGroup>
  );
}

/**
 * Beside the model label while this chat sends without review, so it is never on
 * unnoticed; it opens the same choices as the `+` menu.
 */
export function SendModeChip({ sendMode, onSendModeChange }: SendModeProps) {
  if (!sendMode.effective) return null;
  return (
    <Menu modal={false}>
      <MenuTrigger
        aria-label="Sends without review"
        className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full bg-caution-soft px-2.5 text-[13px] font-medium text-caution transition-[filter] outline-none hover:brightness-[0.97] focus-visible:ring-2 focus-visible:ring-caution/40"
      >
        <TriangleAlert size={14} strokeWidth={1.8} aria-hidden />
        Sends without review
      </MenuTrigger>
      <MenuContent align="end">
        <SendModeItems sendMode={sendMode} onSendModeChange={onSendModeChange} />
      </MenuContent>
    </Menu>
  );
}

/**
 * The waiting outreach card's line under its title: how this chat sends requests, with
 * Change opening the same choices as the `+` menu, under the line and over the brief.
 * A change applies to Claude's next request; this card still waits.
 */
export function SendModeLine({ sendMode, onSendModeChange }: SendModeProps) {
  return (
    <p className="text-ui-sm text-muted">
      This chat: {sendMode.effective ? "sends without review" : "review before sending"} ·{" "}
      <Menu modal={false}>
        <MenuTrigger
          aria-label="Change how this chat sends requests"
          className="rounded-[4px] whitespace-nowrap text-link outline-none hover:underline focus-visible:underline data-[state=open]:underline"
        >
          Change
        </MenuTrigger>
        <MenuContent align="start" className="w-[340px]">
          <SendModeItems sendMode={sendMode} onSendModeChange={onSendModeChange} />
          <MenuSeparator />
          <p className="px-2 pt-0.5 pb-1 text-ui-xs text-muted">
            Applies from Claude&apos;s next request; this one still waits for you.
          </p>
        </MenuContent>
      </Menu>
    </p>
  );
}
