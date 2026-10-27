import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { matchPath, useLocation, useNavigate } from "react-router";

import { describeError, unwrap } from "../api/client";
import { queryKeys } from "../api/queryKeys";
import type { DemoEvent, EventEnvelope, Inbox, MemberDirectory, MemberView, SessionView } from "../api/types";
import { useApi, useDispatcher } from "../app/context";
import { useActAs, useMembers, useSession, useStartSession } from "../app/queries";
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

interface SwitchOptions {
  /** Navigate here after the switch (e.g. the item's inbox). */
  to?: string;
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
      try {
        await mutateAsync(member.id);
      } catch (error) {
        toast({ text: `Couldn't switch to ${member.name}: ${describeError(error)}`, key: "account-switch" });
        return false;
      }
      clearAlertsFor(member.slug, member.org.slug);
      dismissToastWithKey(cueToastKey(member.org.slug, member.slug));
      // "… needs you" was addressed to the member we were acting as.
      if (previous !== undefined) dismissToastWithKey(cueToastKey(previous.org.slug, previous.slug));
      const to = options.to ?? sectionForMemberPage(pathname);
      if (to !== null) void navigate(to);
      toast({ text: `Switched to ${member.name}`, key: "account-switch" });
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
 * so the dispatcher rebuilds from a fresh snapshot as for an account switch.
 */
export function useResetDemo() {
  const api = useApi();
  const dispatcher = useDispatcher();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/worlds/reset")),
    onSuccess: (result) => {
      // Every cue pointed into the retired world.
      clearAllAlerts();
      dismissToastsWithKeyPrefix(CUE_TOAST_PREFIX);
      closeResetConfirm();
      void dispatcher.memberChanged(result.session.epoch);
      void navigate("/");
      toast({ text: "Demo reset", key: "demo-reset" });
    },
  });
}

export type DemoEntry = "suggested" | "explore" | "network";

/**
 * The demo controls' three ways in (§11.4). Each starts the visitor's session if there
 * isn't one yet (worlds are created on first interaction, never on page view).
 */
export function useEnterDemo() {
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const start = useStartSession();
  const session = useSession();
  const { switchTo } = useSwitchAccount();

  const enter = useMutation({
    /** Resolves to null when the switch to the story's member failed (its toast says why). */
    mutationFn: async (entry: DemoEntry): Promise<DemoEntry | null> => {
      const current: SessionView = session.data ?? (await start.mutateAsync());
      if (entry === "suggested" && current.member.slug !== STORY_MEMBER_SLUG) {
        const directory = await queryClient.query({
          queryKey: queryKeys.members,
          queryFn: () => unwrap(api.GET("/api/members")),
        });
        const storyMember = findMember(directory, STORY_MEMBER_SLUG);
        if (storyMember !== null && !(await switchTo(storyMember, { to: "/" }))) return null;
      }
      return entry;
    },
    onSuccess: (entry) => {
      if (entry === null) return;
      closeDemoControls();
      if (entry === "suggested") void navigate("/");
      if (entry === "network") void navigate("/network");
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
