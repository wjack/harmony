import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { matchPath, useLocation, useNavigate, type NavigateFunction } from "react-router";
import { createStore, useStore } from "zustand";

import { describeError, unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { DemoEvent, EventEnvelope, Inbox, MemberDirectory, MemberView, SessionView } from "../api/types";
import { useApi, useDispatcher } from "../app/context";
import { useActAs, useMembers, useSession, useStartSession } from "../app/queries";
import type { MemberChange, RealtimeDispatcher } from "../realtime/dispatcher";
import { firstName, shortOrgName } from "../shell/names";
import { answeringChat, escalationPath, itemForEscalation } from "../shell/escalationPlace";
import { dismissToastWithKey, dismissToastsWithKeyPrefix, toast } from "../shell/toasts";
import {
  addNeedsHumanAlert,
  claimCue,
  clearAlertsFor,
  clearAllAlerts,
  isAlertUp,
  withdrawNeedsHumanAlert,
  type NeedsHumanAlert,
} from "./alerts";
import { STORY_MEMBER_SLUG, findMember, orgName } from "./cast";
import { closeDemoControls, closeResetConfirm } from "./store";
import { clearUnsaved, unsavedSources } from "./unsaved";

const CUE_TOAST_PREFIX = "needs-human:";

/**
 * One toast per account, like its demo-bar chip: a newer cue for the same person
 * replaces it, and acting as them removes it.
 */
function cueToastKey(orgSlug: string, memberSlug: string): string {
  return `${CUE_TOAST_PREFIX}${orgSlug}/${memberSlug}`;
}

/**
 * Where an alert's item opens once you act as its assignee. The shell's requests route
 * resolves it to the item's thread, or to the chat it is answered in
 * (shell/OpenEscalation.tsx).
 */
export function requestsPathFor(escalationId: string): string {
  return `/requests?escalation=${encodeURIComponent(escalationId)}`;
}

// Pages that show one member's own item. After a switch the new member can't open it,
// so the switch lands on the section instead.
const MEMBER_PAGES: readonly (readonly [pattern: string, section: string])[] = [
  ["/chat/:id", "/"],
  ["/requests/:threadId", "/requests"],
];

/** The section to land on when `pathname` is the previous member's own item, else null. */
export function sectionForMemberPage(pathname: string): string | null {
  for (const [pattern, section] of MEMBER_PAGES) {
    if (matchPath(pattern, pathname) !== null) return section;
  }
  return null;
}

/**
 * The member page (its location key) a switch is taking this tab off, until it has left.
 * The way off can land after the new member's frame mounts (a lazy route loads, then the
 * router commits in a transition), so the frame shows nothing there meanwhile: the
 * previous member's item, fetched as the new member, would be a 404.
 */
const leavingStore = createStore<{ key: string | null }>(() => ({ key: null }));

/** Whether a switch is taking this tab off the page at `key`. */
export function useLeaving(key: string): boolean {
  return useStore(leavingStore, (state) => state.key === key);
}

interface SwitchOptions {
  /** Navigate here after the switch (e.g. the item's inbox). */
  to?: string;
}

const SWITCH_TOAST = "account-switch";

/**
 * A switch this tab asked for, from just before its request until something reports
 * it: the response, this tab's stream or another tab, whichever comes first. That one
 * lands it, once (FEEDBACK_R2_PLAN §2.6). Module state: each tab answers for its own.
 */
interface SwitchIntent {
  /** Who it switches to; null for a reset, which lands on whoever the world starts as. */
  memberId: string | null;
  /** Where landing goes; null to stay. */
  to: string | null;
  /** What landing says and tidies up. */
  onLand: () => void;
  landed: boolean;
}

let intent: SwitchIntent | null = null;

function expectSwitch(next: SwitchIntent): void {
  intent = next;
}

function forgetSwitch(pending: SwitchIntent): void {
  if (intent === pending) intent = null;
}

function land(pending: SwitchIntent, navigate: NavigateFunction): void {
  if (pending.landed) return;
  pending.landed = true;
  forgetSwitch(pending);
  if (pending.to !== null) void navigate(pending.to);
  pending.onLand();
}

/** This tab's own switch that `change` reports, if one is waiting. */
function intentFor(change: MemberChange): SwitchIntent | null {
  if (intent === null) return null;
  // A refused stream only shows that the epoch moved; with a switch of this tab's own
  // in flight, that's the one.
  const ours = change.memberId === null || intent.memberId === null || intent.memberId === change.memberId;
  return ours ? intent : null;
}

/** Acting as `member` settles their cue, and the "… needs you" addressed to `previous`. */
function settleCues(member: MemberView, previous: MemberView | undefined): void {
  clearAlertsFor(member.slug, member.org.slug);
  dismissToastWithKey(cueToastKey(member.org.slug, member.slug));
  if (previous !== undefined) dismissToastWithKey(cueToastKey(previous.org.slug, previous.slug));
}

/** A reset leaves the world every cue pointed into. */
function dropAllCues(): void {
  clearAllAlerts();
  dismissToastsWithKeyPrefix(CUE_TOAST_PREFIX);
}

/**
 * Act as another member (ARCHITECTURE.md §11.3): the API switches the session, the
 * dispatcher reopens the stream and swaps the cache, and the shell remounts as them.
 *
 * `switchTo` never rejects, so callers can fire and forget it: a failure (a retired or
 * replay world, a network error) is shown as a toast, and it resolves to false.
 */
export function useSwitchAccount() {
  const actAs = useActAs();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { mutateAsync } = actAs;

  const switchTo = useCallback(
    async (member: MemberView, options: SwitchOptions = {}): Promise<boolean> => {
      const previous = queryClient.getQueryData<SessionView>(queryKeys.session)?.member;
      const pending: SwitchIntent = {
        memberId: member.id,
        to: options.to ?? sectionForMemberPage(pathname),
        onLand: () => {
          settleCues(member, previous);
          toast({ text: `Switched to ${member.name}`, key: SWITCH_TOAST });
        },
        landed: false,
      };
      // Recorded before the request: the switch's event can beat its response.
      expectSwitch(pending);
      try {
        await mutateAsync(member.id);
      } catch (error) {
        forgetSwitch(pending);
        // Reported by the stream already, so it happened, whatever became of the response.
        if (pending.landed) return true;
        toast({ text: `Couldn't switch to ${member.name}: ${describeError(error)}`, key: SWITCH_TOAST });
        return false;
      }
      land(pending, navigate);
      return true;
    },
    [mutateAsync, navigate, pathname, queryClient],
  );

  return { switchTo, pending: actAs.isPending, error: actAs.error };
}

/** Act as an alert's assignee and open the item in their inbox. */
export function useFollowAlert() {
  const { switchTo, pending } = useSwitchAccount();
  const members = useMembers();
  const follow = useCallback(
    async (alert: NeedsHumanAlert): Promise<boolean> => {
      const member = findMember(members.data, alert.memberSlug, alert.orgSlug);
      if (member === null) {
        toast(`${alert.memberSlug} isn't in this world any more.`);
        return false;
      }
      return switchTo(member, { to: requestsPathFor(alert.escalationId) });
    },
    [members.data, switchTo],
  );
  return { follow, pending };
}

/**
 * Reset forks a fresh world and retires the old one (§11.1); the session's epoch moves,
 * so the dispatcher rebuilds from a fresh snapshot as for an account switch, and it
 * lands like one: on New chat, once.
 */
export function useResetDemo() {
  const api = useApi();
  const dispatcher = useDispatcher();
  const navigate = useNavigate();
  return useMutation({
    onMutate: (): SwitchIntent => {
      const pending: SwitchIntent = {
        memberId: null,
        to: "/",
        onLand: () => {
          dropAllCues();
          toast({ text: "Demo reset", key: "demo-reset" });
        },
        landed: false,
      };
      expectSwitch(pending);
      return pending;
    },
    mutationFn: () => unwrap(api.POST("/api/worlds/reset")),
    onSuccess: ({ session }, _variables, pending) => {
      closeResetConfirm();
      void dispatcher.memberChanged(session.epoch, { sessionId: session.session_id, memberId: session.member.id });
      land(pending, navigate);
    },
    onError: (_error, _variables, pending) => {
      if (pending !== undefined) forgetSwitch(pending);
    },
  });
}

/**
 * Follows every switch this tab hears of, before the new member renders. Mount once,
 * next to `useDemoCues`, in the part of the shell that outlives switches.
 *
 * This tab's own switch lands where it asked. Any other (another tab's switch or
 * reset, the storyrun CLI) leaves the previous member's own item for its section,
 * replacing the URL, so the new member never meets it (`useLeaving` holds the page back
 * until the URL has changed); then, once the new member's session is in, it settles the
 * cues they or the new world made stale and says if unsaved Customize edits went with
 * the old frame.
 */
export function useFollowSwitch(): void {
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const locationRef = useRef(location);

  useEffect(() => {
    locationRef.current = location;
    const { key } = leavingStore.getState();
    if (key !== null && key !== location.key) leavingStore.setState({ key: null });
  }, [location]);

  useEffect(
    () =>
      dispatcher.onMemberChanged((change) => {
        const { pathname, key } = locationRef.current;
        const section = sectionForMemberPage(pathname);
        const pending = intentFor(change);
        // Every switch leaves the previous member's own page (to its section, or where
        // this tab's switch asked to go).
        if (section !== null && pending?.to !== null) leavingStore.setState({ key });
        if (pending !== null) {
          land(pending, navigate);
          return;
        }
        if (section !== null) void navigate(section, { replace: true });
        const dropped = unsavedSources().length > 0;
        clearUnsaved();
        const before = queryClient.getQueryData<SessionView>(queryKeys.session);
        afterRebuild(dispatcher, () => {
          const after = queryClient.getQueryData<SessionView>(queryKeys.session);
          if (after === undefined) return;
          if (before !== undefined && before.world_id !== after.world_id) dropAllCues();
          else settleCues(after.member, before?.member);
          if (dropped) {
            toast({
              text: `Your unsaved Customize changes were dropped when you switched to ${after.member.name} in another tab.`,
              key: "unsaved-dropped",
              durationMs: 8_000,
            });
          }
        });
      }),
    [dispatcher, navigate, queryClient],
  );
}

/** Runs `then` once the dispatcher has rebuilt as the new member; never if it can't. */
function afterRebuild(dispatcher: RealtimeDispatcher, then: () => void): void {
  const unsubscribe = dispatcher.store.subscribe((state, previous) => {
    if (state.generation !== previous.generation) {
      unsubscribe();
      then();
    } else if (state.status === "no-session" || state.status === "locked" || state.status === "stopped") {
      unsubscribe();
    }
  });
}

/**
 * Demo controls' "Try the suggested request" and the intro's Next (§11.4): starts the
 * visitor's session if there isn't one yet (worlds are created on first interaction,
 * never on page view), acts as the story's member and opens a new chat. `then` runs
 * once it has: the intro shows "What's on screen" next.
 */
export function useEnterDemo(then?: () => void) {
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const start = useStartSession();
  const session = useSession();
  const { switchTo } = useSwitchAccount();

  const enter = useMutation({
    /** Resolves to false when the switch to the story's member failed (its toast says why). */
    mutationFn: async (): Promise<boolean> => {
      const current: SessionView = session.data ?? (await start.mutateAsync());
      if (current.member.slug !== STORY_MEMBER_SLUG) {
        const directory = await queryClient.query({
          queryKey: queryKeys.members,
          queryFn: () => unwrap(api.GET("/api/members")),
        });
        const storyMember = findMember(directory, STORY_MEMBER_SLUG);
        if (storyMember !== null && !(await switchTo(storyMember, { to: "/" }))) return false;
      }
      return true;
    },
    onSuccess: (entered) => {
      if (!entered) return;
      closeDemoControls();
      then?.();
      void navigate("/");
    },
  });
  return enter;
}

/**
 * Turns `demo.needs_human` cues into toasts and demo-bar chips (§11.4). Mount once, in
 * the part of the shell that outlives account switches: it reads the acting session
 * from the cache when a cue arrives, so it never holds a previous account's view.
 */
export function useDemoCues(): void {
  const api = useApi();
  const dispatcher = useDispatcher();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { switchTo } = useSwitchAccount();
  const switchRef = useRef(switchTo);

  useEffect(() => {
    switchRef.current = switchTo;
  }, [switchTo]);

  useEffect(() => {
    // Names for the toast; the directory is usually cached, and a cue never waits on it
    // to raise the chip.
    const directory = (): Promise<MemberDirectory | undefined> =>
      queryClient
        .query({
          queryKey: queryKeys.members,
          queryFn: () => unwrap(api.GET("/api/members")),
          staleTime: 5 * 60_000,
        })
        .catch(() => undefined);
    // The item the cue is about; the cached inbox may not have it yet.
    const freshInbox = (): Promise<Inbox | undefined> =>
      queryClient
        .query({ queryKey: queryKeys.inbox, queryFn: () => unwrap(api.GET("/api/inbox")), staleTime: 0 })
        .catch(() => undefined);

    return dispatcher.onDemo((event: DemoEvent, envelope: EventEnvelope) => {
      if (event.type !== "demo.needs_human") return;
      const acting = queryClient.getQueryData<SessionView>(queryKeys.session)?.member;
      // Member slugs are per org: "admin" at one org isn't "admin" at another.
      const toActing = acting?.slug === event.member_slug && acting.org.slug === event.org_slug;
      // An item routed to two people cues both; it gets one toast and one chip (E5).
      if (event.entity_id !== null && !claimCue(event.entity_id, envelope.created_at ?? "", toActing)) return;

      if (acting !== undefined && toActing) {
        // Already acting as an assignee: point at the item where they answer it, never
        // at a co-assignee whose cue came first.
        const escalationId = event.entity_id ?? "";
        const pointedAway = withdrawNeedsHumanAlert(escalationId);
        if (pointedAway !== null) dismissToastWithKey(cueToastKey(pointedAway.orgSlug, pointedAway.memberSlug));
        void Promise.all([directory(), freshInbox()]).then(([members, inbox]) => {
          const item = inbox === undefined ? null : itemForEscalation(inbox, escalationId);
          const key = cueToastKey(acting.org.slug, acting.slug);
          if (item !== null && answeringChat(item) !== null) {
            toast({
              text: `${shortOrgName(item.counterparty.name)}'s question needs your answer.`,
              key,
              action: {
                label: "Answer in the chat",
                onClick: () => {
                  void navigate(escalationPath(item));
                },
              },
            });
            return;
          }
          toast({
            text: `${shortOrgName(orgName(members, event.org_slug ?? ""))} needs you.`,
            key,
            action: {
              label: "Open Requests",
              onClick: () => {
                void navigate(requestsPathFor(escalationId));
              },
            },
          });
        });
        return;
      }

      const alert = addNeedsHumanAlert(event);
      if (alert === null) return;
      void directory().then((members) => {
        // The acting member's own cue, or a switch, may have taken the chip down meanwhile.
        if (!isAlertUp(alert)) return;
        const assignee = findMember(members, alert.memberSlug, alert.orgSlug);
        toast({
          text: `${shortOrgName(orgName(members, alert.orgSlug))} needs a human.`,
          key: cueToastKey(alert.orgSlug, alert.memberSlug),
          durationMs: 8_000,
          action: {
            label: `Switch to ${assignee === null ? alert.memberSlug : firstName(assignee.name)}`,
            onClick: () => {
              if (assignee !== null) {
                void switchRef.current(assignee, { to: requestsPathFor(alert.escalationId) });
              }
            },
          },
        });
      });
    });
  }, [api, dispatcher, queryClient, navigate]);
}
