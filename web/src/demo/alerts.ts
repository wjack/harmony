import { createStore, useStore } from "zustand";

import type { DemoNeedsHuman } from "../api/types";

/** "Northlake needs a human → switch to Helen": an escalation waiting on another account. */
export interface NeedsHumanAlert {
  /** The escalation (the cue's `entity_id`). */
  escalationId: string;
  orgSlug: string;
  memberSlug: string;
  receivedAt: number;
}

interface AlertState {
  alerts: NeedsHumanAlert[];
}

const MAX_ALERTS = 4;

/** Like toasts, alerts outlive an account switch: they point at other accounts. */
export const alertStore = createStore<AlertState>(() => ({ alerts: [] }));

export function addNeedsHumanAlert(cue: DemoNeedsHuman, now: number = Date.now()): NeedsHumanAlert | null {
  const { entity_id: escalationId, org_slug: orgSlug, member_slug: memberSlug } = cue;
  if (escalationId === null || orgSlug === null || memberSlug === null) return null;
  const alert: NeedsHumanAlert = { escalationId, orgSlug, memberSlug, receivedAt: now };
  alertStore.setState((state) => ({
    alerts: [alert, ...state.alerts.filter((existing) => existing.escalationId !== escalationId)].slice(
      0,
      MAX_ALERTS,
    ),
  }));
  return alert;
}

/**
 * Acting as a member answers their alerts: they're now looking at their own inbox.
 * Member slugs are per org, so both must match. Returns the alerts it cleared.
 */
export function clearAlertsFor(memberSlug: string, orgSlug: string): NeedsHumanAlert[] {
  const isFor = (alert: NeedsHumanAlert) => alert.memberSlug === memberSlug && alert.orgSlug === orgSlug;
  const cleared = alertStore.getState().alerts.filter(isFor);
  if (cleared.length > 0) {
    alertStore.setState((state) => ({ alerts: state.alerts.filter((alert) => !isFor(alert)) }));
  }
  return cleared;
}

/** One account's alerts: a single demo-bar chip, however many items wait on them. */
export interface AccountAlerts {
  orgSlug: string;
  memberSlug: string;
  /** Newest first; the chip opens the newest. */
  alerts: NeedsHumanAlert[];
}

/** `alerts` grouped by account, in the order each account's newest alert arrived. */
export function alertsByAccount(alerts: readonly NeedsHumanAlert[]): AccountAlerts[] {
  const groups = new Map<string, AccountAlerts>();
  for (const alert of alerts) {
    const key = `${alert.orgSlug}/${alert.memberSlug}`;
    const group = groups.get(key);
    if (group === undefined) groups.set(key, { orgSlug: alert.orgSlug, memberSlug: alert.memberSlug, alerts: [alert] });
    else group.alerts.push(alert);
  }
  return [...groups.values()];
}

export function clearAllAlerts(): void {
  alertStore.setState({ alerts: [] });
}

export function useNeedsHumanAlerts(): NeedsHumanAlert[] {
  return useStore(alertStore, (state) => state.alerts);
}
