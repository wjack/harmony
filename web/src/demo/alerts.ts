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

/** The routing an escalation was last cued for, and whether that cue was the acting member's own. */
interface Cued {
  routing: string;
  toActing: boolean;
}

interface AlertState {
  alerts: NeedsHumanAlert[];
  /** Each escalation this world has cued, with the routing that cued it last. */
  cued: ReadonlyMap<string, Cued>;
}

const MAX_ALERTS = 4;

/** Like toasts, alerts outlive an account switch: they point at other accounts. */
export const alertStore = createStore<AlertState>(() => ({ alerts: [], cued: new Map() }));

/**
 * Claims the cue for one routing of an escalation; false once that routing's cue has
 * been claimed. A routing cues each new assignee in one commit, so its cues share
 * `routing` (the envelope's `created_at`): an item routed to two people is one item, and
 * the first assignee's cue stands. A later routing, such as a reassign, cues again.
 *
 * The acting member's own cue (`toActing`) still gets through after a teammate's claimed
 * the routing: the item is theirs to answer, so it replaces the cue that pointed at the
 * teammate.
 */
export function claimCue(escalationId: string, routing: string, toActing: boolean): boolean {
  const claimed = alertStore.getState().cued.get(escalationId);
  if (claimed?.routing === routing && (claimed.toActing || !toActing)) return false;
  alertStore.setState((state) => ({ cued: new Map(state.cued).set(escalationId, { routing, toActing }) }));
  return true;
}

/** Raise the chip for a cue, in place of its item's earlier one; null when it names no one. */
export function addNeedsHumanAlert(cue: DemoNeedsHuman, now: number = Date.now()): NeedsHumanAlert | null {
  const { entity_id: escalationId, org_slug: orgSlug, member_slug: memberSlug } = cue;
  if (escalationId === null || orgSlug === null || memberSlug === null) return null;
  const alert: NeedsHumanAlert = { escalationId, orgSlug, memberSlug, receivedAt: now };
  alertStore.setState((state) => ({
    alerts: [alert, ...state.alerts.filter((existing) => existing.escalationId !== escalationId)].slice(0, MAX_ALERTS),
  }));
  return alert;
}

/** Take down an escalation's chip, if it has one; returns it. */
export function withdrawNeedsHumanAlert(escalationId: string): NeedsHumanAlert | null {
  const withdrawn = alertStore.getState().alerts.find((alert) => alert.escalationId === escalationId) ?? null;
  if (withdrawn !== null) {
    alertStore.setState((state) => ({ alerts: state.alerts.filter((alert) => alert !== withdrawn) }));
  }
  return withdrawn;
}

/** Whether `alert` is still up: not cleared by a switch, nor replaced or withdrawn. */
export function isAlertUp(alert: NeedsHumanAlert): boolean {
  return alertStore.getState().alerts.includes(alert);
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

/** A new world (or a locked tab): every cue pointed at items that are gone. */
export function clearAllAlerts(): void {
  alertStore.setState({ alerts: [], cued: new Map() });
}

export function useNeedsHumanAlerts(): NeedsHumanAlert[] {
  return useStore(alertStore, (state) => state.alerts);
}
