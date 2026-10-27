import { ArrowLeftRight, History, Lock, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router";

import { ApiError, describeError } from "../api/client";
import type { DemoWhose, SessionView } from "../api/types";
import { useMembers, useSession } from "../app/queries";
import { firstName, shortOrgName } from "../shell/names";
import { Button } from "../shell/ui";
import { useSwitchAccount } from "./accounts";
import { allMembers } from "./cast";
import { useWhose, type WhoseKind } from "./whose";

type WhoseOrg = DemoWhose["orgs"][number];

const SECTION: Record<WhoseKind, { noun: string; path: string; back: string }> = {
  chat: { noun: "chat", path: "/chats", back: "Go to your chats" },
  thread: { noun: "request", path: "/requests", back: "Go to your requests" },
};

/**
 * The page for a chat or request the acting member can't open (switching.html): whose
 * it is, with a switch to them, or, for an id this demo's world doesn't have, that it's
 * from an earlier one. The switch is demo chrome (§11.4), styled like the demo bar; it
 * lands back on this item as the person who can open it.
 */
export function WhoseNotFound({ kind, id }: { kind: WhoseKind; id: string }) {
  const whose = useWhose(kind, id);
  const session = useSession();
  if (whose.isPending || session.data === undefined) return <div className="h-full bg-canvas" aria-busy="true" />;
  if (whose.isError) {
    if (whose.error instanceof ApiError && whose.error.status === 404) return <NotInThisDemo kind={kind} />;
    return (
      <Empty icon={Lock} title={`Couldn't open this ${SECTION[kind].noun}`} why={describeError(whose.error)}>
        <Button
          onClick={() => {
            void whose.refetch();
          }}
        >
          Try again
        </Button>
        <BackButton kind={kind} />
      </Empty>
    );
  }
  return <Whose whose={whose.data} acting={session.data.member} />;
}

function Whose({ whose, acting }: { whose: DemoWhose; acting: SessionView["member"] }) {
  const { switchTo, pending } = useSwitchAccount();
  const members = useMembers();
  const { pathname, search } = useLocation();
  // One switch per org, to the first person there who can open it; it comes back here.
  const switches = whose.orgs.flatMap(({ people }) => {
    const first = people.find((person) => person.member_id !== acting.id);
    if (first === undefined) return [];
    const member = allMembers(members.data).find((candidate) => candidate.id === first.member_id);
    return [
      <SwitchButton
        key={first.member_id}
        name={first.name}
        disabled={member === undefined || pending}
        onClick={() => {
          if (member !== undefined) void switchTo(member, { to: pathname + search });
        }}
      />,
    ];
  });

  if (whose.kind === "chat") {
    const owner = whose.orgs[0]?.people[0];
    if (owner === undefined) return <NotInThisDemo kind="chat" />;
    return (
      <Empty
        icon={Lock}
        title={`This chat is ${owner.name}'s`}
        why={`Chats belong to the person who started them. You're acting as ${acting.name} at ${shortOrgName(acting.org.name)}.`}
      >
        {switches}
        <BackButton kind="chat" />
      </Empty>
    );
  }
  return (
    <Empty icon={Lock} title={`${firstName(acting.name)} can't open this request`} why={whoCanOpen(whose.orgs)}>
      {switches}
      <BackButton kind="thread" />
    </Empty>
  );
}

/** "It's between A and B. X and Y at A, and Z at B, can open it." */
function whoCanOpen(orgs: readonly WhoseOrg[]): string {
  const between = `It's between ${listed(orgs.map(({ org }) => org.name))}.`;
  const groups = orgs
    .filter(({ people }) => people.length > 0)
    .map(({ org, people }) => `${listed(people.map((person) => person.name))} at ${shortOrgName(org.name)}`);
  if (groups.length === 0) return `${between} No one in this demo can open it.`;
  if (groups.length === 1) return `${between} ${groups[0] ?? ""} can open it.`;
  return `${between} ${groups.join(", and ")}, can open it.`;
}

/** "A", "A and B", "A, B and C". */
function listed(names: readonly string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1) ?? ""}`;
}

function NotInThisDemo({ kind }: { kind: WhoseKind }) {
  return (
    <Empty
      icon={History}
      title={`This ${SECTION[kind].noun} isn't in this demo`}
      why="It's from before a reset or an earlier visit. Nothing from that demo carries over."
    >
      <BackButton kind={kind} />
    </Empty>
  );
}

/** Demo chrome, styled like the demo bar. */
function SwitchButton({ name, disabled, onClick }: { name: string; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-control border border-demo-border bg-demo px-3 text-ui font-medium whitespace-nowrap text-demo-text transition-colors hover:bg-demo-surface disabled:opacity-60"
    >
      <ArrowLeftRight size={16} strokeWidth={1.75} aria-hidden />
      Switch to {firstName(name)}
    </button>
  );
}

function BackButton({ kind }: { kind: WhoseKind }) {
  const navigate = useNavigate();
  const { path, back } = SECTION[kind];
  return (
    <Button
      onClick={() => {
        void navigate(path);
      }}
    >
      {back}
    </Button>
  );
}

function Empty({ icon: Icon, title, why, children }: { icon: LucideIcon; title: string; why: string; children: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 pb-[12vh] text-center">
      <div className="mb-1 flex size-10 items-center justify-center rounded-[10px] bg-hover text-muted">
        <Icon size={20} strokeWidth={1.75} aria-hidden />
      </div>
      <h1 className="text-ui-title font-medium text-ink">{title}</h1>
      <p className="max-w-[380px] text-ui text-muted">{why}</p>
      <div className="mt-2 flex flex-wrap justify-center gap-2">{children}</div>
    </div>
  );
}
