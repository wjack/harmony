/**
 * The three autonomy modes (REVISION_UNSTRUCTURED.md §4.4), in the words the settings
 * page explains them with.
 */
import { CircleCheck, FastForward, Hand, type LucideIcon } from "lucide-react";

import type { Autonomy } from "./types";

export interface ModeInfo {
  value: Autonomy;
  label: string;
  Icon: LucideIcon;
  description: string;
  recommended: boolean;
}

export const MODE_INFO: Readonly<Record<Autonomy, ModeInfo>> = {
  ask: {
    value: "ask",
    label: "Ask",
    Icon: Hand,
    description: "A teammate approves every answer and message before it goes to the other organization.",
    recommended: false,
  },
  auto: {
    value: "auto",
    label: "Auto",
    Icon: CircleCheck,
    description:
      "Claude handles the conversation. A teammate sees anything beyond what you share, and any decision that needs a person.",
    recommended: true,
  },
  skip: {
    value: "skip",
    label: "Skip",
    Icon: FastForward,
    description:
      "Claude handles everything it can. A teammate sees only what Claude says needs a person. For partners you trust fully.",
    recommended: false,
  },
};

/** Least to most autonomous: the order every mode control shows them in. */
export const MODES: readonly ModeInfo[] = [MODE_INFO.ask, MODE_INFO.auto, MODE_INFO.skip];
