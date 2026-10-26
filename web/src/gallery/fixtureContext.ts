import { createContext, use } from "react";

import type { EventEnvelope } from "../api/types";

/** Delivers an event to the enclosing section's fixture stream. */
export type Emit = (envelope: EventEnvelope) => void;

export const FixtureEmitContext = createContext<Emit | null>(null);

/** In a gallery section: push an event through the section's real dispatcher. */
export function useFixtureEmit(): Emit {
  const emit = use(FixtureEmitContext);
  if (emit === null) throw new Error("useFixtureEmit must be used inside <FixtureProviders>");
  return emit;
}
