import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import type { MemberView } from "../api/types";
import { useDemoStory } from "../app/queries";
import { useSwitchAccount } from "../demo/accounts";
import { Button, Spark } from "../shell/ui";
import {
  isMissingWorld,
  useLensSession,
  useNetworkView,
  useRequestConversation,
  useWorldMembers,
} from "./api";
import { useNow } from "./clock";
import { ringedOrgIds } from "./layout";
import { useNetworkLive } from "./lens";
import { useRailWhilePanelOpen } from "./narrow";
import { NetworkCanvas } from "./NetworkCanvas";
import { NetworkHeader } from "./NetworkHeader";
import { OrgPanel } from "./OrgPanel";
import { countParticipants, noRequestText } from "./status";
import type { NetworkView, WorldLens } from "./types";
import { readableRecipients, resolveWait, waitAction, type Wait } from "./waiting";

function CenteredNote({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <div className="flex max-w-[400px] flex-col items-center text-center">
        <Spark size={28} />
        <h2 className="mt-4 font-serif text-[22px] leading-7 tracking-[-0.01em] text-ink">{title}</h2>
        {children}
      </div>
    </div>
  );
}

/**
 * Shown above the canvas when no request is in focus. It takes its own row, so the
 * canvas frames the cards below it rather than under it.
 */
function NoRequestCard({ world, earlier }: { world: WorldLens; earlier: boolean }) {
  return (
    <div className="flex shrink-0 justify-center px-6 pt-5" data-no-request>
      <div className="flex max-w-[540px] items-center gap-3.5 rounded-[12px] border border-border bg-surface py-2.5 pr-2.5 pl-4 shadow-menu">
        <Spark size={20} className="shrink-0" />
        <p className="min-w-0 flex-1 text-[13px] leading-5 text-secondary">{noRequestText(world, earlier)}</p>
        {world === "desk" && (
          <Link
            to="/"
            className="inline-flex h-8 shrink-0 items-center rounded-control bg-primary px-3 text-sm font-medium text-on-primary transition-colors hover:bg-primary-hover"
          >
            New chat
          </Link>
        )}
      </div>
    </div>
  );
}

export interface NetworkScreenProps {
  world: WorldLens;
  /** The focused request; null lets the API pick the latest one. */
  requestId: string | null;
  onWorldChange: (world: WorldLens) => void;
  onRequestChange: (requestId: string) => void;
  /** Open with this org's panel showing (the gallery and screenshots). */
  initialSelectedOrgId?: string;
}

/** The network view (UI_REFERENCE.md §8, ARCHITECTURE.md §11.6), driven by its props. */
export function NetworkScreen({
  world,
  requestId,
  onWorldChange,
  onRequestChange,
  initialSelectedOrgId,
}: NetworkScreenProps) {
  const network = useNetworkView(world, requestId);
  const lensSession = useLensSession(world);
  const story = useDemoStory();
  const now = useNow();
  const [selected, setSelected] = useState<string | null>(initialSelectedOrgId ?? null);
  const { switchTo, pending: switching } = useSwitchAccount();

  const view: NetworkView | undefined = network.data?.view;
  const members = useWorldMembers(world, view !== undefined);
  const activity = useNetworkLive({
    world,
    worldId: view?.world_id ?? null,
    cursor: network.data?.cursor ?? null,
    epoch: lensSession.data?.epoch ?? null,
    enabled: network.isSuccess,
  });

  const actor = lensSession.data?.member ?? null;
  const homeOrgId = actor?.org.id ?? null;
  const suggestedOrgIds = useMemo(() => {
    const highlighted = story.data === undefined ? null : new Set(story.data.suggested_recipients.map((org) => org.id));
    return ringedOrgIds(world, view, highlighted);
  }, [world, story.data, view]);
  const counts = useMemo(() => (view === undefined ? null : countParticipants(view.nodes)), [view]);
  const orgs = useMemo(
    () =>
      new Map(
        (view?.nodes ?? []).map((node) => [
          node.org.id,
          { name: node.org.name, slug: node.org.slug, participant: node.participant },
        ]),
      ),
    [view],
  );

  const requester = view?.requests.find((request) => request.id === view.request_id)?.requester ?? null;
  const waits = useMemo(() => {
    const found = new Map<string, Wait>();
    const readable = readableRecipients(view?.edges ?? []);
    for (const node of view?.nodes ?? []) {
      const wait = resolveWait(node, { requester, actor, directory: members.data ?? [], readable });
      if (wait !== null) found.set(node.org.id, wait);
    }
    return found;
  }, [view, requester, actor, members.data]);
  const needsChat = world === "desk" && [...waits.values()].some((wait) => wait.side === "requester");
  const conversation = useRequestConversation(view?.request_id ?? null, needsChat);

  const selectedNode = view?.nodes.find((node) => node.org.id === selected) ?? null;
  useRailWhilePanelOpen(selectedNode !== null);
  const selectedThreads = useMemo(
    () =>
      selected === null
        ? []
        : (view?.edges ?? []).filter((edge) => edge.from_org_id === selected || edge.to_org_id === selected),
    [view, selected],
  );
  const selectedWait = selectedNode === null ? null : (waits.get(selectedNode.org.id) ?? null);
  const selectedAction =
    selectedWait === null || selectedNode === null
      ? null
      : waitAction(selectedWait, {
          world,
          threadId:
            selectedThreads.find(
              (edge) => edge.to_org_id === selectedNode.org.id && edge.from_org_id === requester?.id,
            )?.thread_id ?? null,
          conversationId: conversation.data ?? null,
        });

  const actAs = useCallback(
    (member: MemberView, to: string) => {
      void switchTo(member, { to });
    },
    [switchTo],
  );

  let body: ReactNode;
  if (view !== undefined) {
    body = (
      <div className="absolute inset-0 flex flex-col">
        {view.request_id === null && <NoRequestCard world={world} earlier={view.requests.length > 0} />}
        <div className="relative min-h-0 flex-1">
          <NetworkCanvas
            key={`${world}:${view.request_id ?? "none"}`}
            view={view}
            suggestedOrgIds={suggestedOrgIds}
            waits={waits}
            homeOrgId={homeOrgId}
            activity={activity}
            selectedOrgId={selectedNode === null ? null : selected}
            onSelectOrg={setSelected}
            now={now}
          />
        </div>
      </div>
    );
  } else if (network.isPending) {
    body = (
      <div className="absolute inset-0 flex items-center justify-center" aria-busy="true">
        <Spark size={24} animated label="Loading the network" />
      </div>
    );
  } else if (world === "network" && isMissingWorld(network.error)) {
    body = (
      <CenteredNote title="The big run comes later">
        <p className="mt-2 text-sm leading-6 text-muted">
          One request fanned out to the whole network, in a world of its own, isn&apos;t part of this
          prototype yet. Your desk shows your own requests.
        </p>
        <Button
          className="mt-5"
          onClick={() => {
            onWorldChange("desk");
          }}
        >
          Back to your desk
        </Button>
      </CenteredNote>
    );
  } else {
    const error = network.error;
    body = (
      <CenteredNote title="The network view couldn't load">
        <p className="mt-2 text-sm leading-6 text-muted">
          {error instanceof Error ? error.message : "Something went wrong."}
        </p>
        <Button
          className="mt-5"
          onClick={() => {
            void network.refetch();
          }}
        >
          Try again
        </Button>
      </CenteredNote>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <NetworkHeader
        world={world}
        onWorldChange={(next) => {
          setSelected(null);
          onWorldChange(next);
        }}
        requests={view?.requests ?? []}
        requestId={view?.request_id ?? null}
        onRequestChange={(next) => {
          setSelected(null);
          onRequestChange(next);
        }}
        counts={counts}
        mode={lensSession.data?.world.mode ?? null}
        now={now}
      />
      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1 bg-canvas">{body}</div>
        {selectedNode !== null && (
          <OrgPanel
            key={selectedNode.org.id}
            node={selectedNode}
            threads={selectedThreads}
            orgs={orgs}
            world={world}
            homeOrgId={homeOrgId}
            wait={selectedWait}
            action={selectedAction}
            onActAs={actAs}
            actingAs={switching}
            onClose={() => {
              setSelected(null);
            }}
            now={now}
          />
        )}
      </div>
    </div>
  );
}

/** `/network`: the world and focused request live in the URL (`?world=network&request=…`). */
export function NetworkPage() {
  const [params, setParams] = useSearchParams();
  const world: WorldLens = params.get("world") === "network" ? "network" : "desk";
  const requestId = params.get("request");

  return (
    <NetworkScreen
      world={world}
      requestId={requestId}
      onWorldChange={(next) => {
        setParams(next === "network" ? { world: next } : {});
      }}
      onRequestChange={(next) => {
        setParams(world === "network" ? { world, request: next } : { request: next });
      }}
    />
  );
}
