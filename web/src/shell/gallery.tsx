/** The shell's gallery sections (/__gallery). */
import { ArrowUp, Plus, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { useSession } from "../app/queries";
import { members, shellRoutes } from "../fixtures/shell";
import type { GallerySection } from "../gallery/types";
import { ShellFrame, type FrameStatus } from "./AppLayout";
import { ChatsPage } from "./ChatsPage";
import { Landing } from "./Landing";
import { Locked } from "./Locked";
import { firstName } from "./names";
import { toast } from "./toasts";
import { Avatar, Button, IconButton, Modal, ModalList, OrgAvatar, Segmented, Spark, Toggle } from "./ui";
import { showUnavailable } from "./unavailable";

function CurrentShell({
  children,
  sidebar,
  status,
}: {
  children: ReactNode;
  sidebar?: { collapsed?: boolean; defaultMenuOpen?: boolean };
  status?: FrameStatus;
}) {
  const session = useSession();
  return (
    <ShellFrame session={session.data ?? null} sidebar={sidebar} status={status}>
      {children}
    </ShellFrame>
  );
}

/** Stands in for the chat's new-chat screen (U2's), so the shell reads as the app. */
function HomeStandIn() {
  const session = useSession();
  const name = session.data === undefined ? "" : `, ${firstName(session.data.member.name)}`;
  return (
    <div className="flex h-full flex-col items-center justify-center pb-[10vh]">
      <h1 className="flex items-center gap-3 font-serif text-[40px] leading-tight tracking-[-0.02em]">
        <Spark size={34} /> Good morning{name}
      </h1>
      <div className="mt-7 flex h-[104px] w-[min(640px,90%)] flex-col justify-between rounded-composer border border-border bg-surface px-4 pt-4 pb-3 shadow-composer">
        <span className="text-[15px] text-muted">How can I help you today?</span>
        <span className="flex items-center justify-between">
          <Plus size={18} strokeWidth={1.5} className="text-secondary" />
          <span className="flex items-center gap-3 text-[13px] text-secondary">
            Opus 5.5
            <span className="inline-flex size-8 items-center justify-center rounded-control bg-accent-strong/40 text-white">
              <ArrowUp size={16} strokeWidth={2} />
            </span>
          </span>
        </span>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3 py-2">
      <span className="w-28 shrink-0 text-xs text-muted">{label}</span>
      {children}
    </div>
  );
}

function Primitives() {
  const [segment, setSegment] = useState<"ask" | "auto" | "skip">("auto");
  const [on, setOn] = useState(true);
  const [modal, setModal] = useState(false);
  return (
    <div className="flex flex-col">
      <Row label="Buttons">
        <Button variant="primary">Save changes</Button>
        <Button variant="secondary">Cancel</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="danger">Decline</Button>
        <Button variant="primary" size="sm">
          Small
        </Button>
        <Button variant="primary" size="lg" icon={<Plus size={16} strokeWidth={2} />}>
          New chat
        </Button>
        <Button variant="primary" disabled>
          Disabled
        </Button>
      </Row>
      <Row label="Icon buttons">
        <IconButton label="Close">
          <X size={18} strokeWidth={1.5} />
        </IconButton>
        <IconButton label="Add" size="sm">
          <Plus size={16} strokeWidth={1.5} />
        </IconButton>
      </Row>
      <Row label="Segmented">
        <Segmented
          label="Autonomy"
          value={segment}
          onChange={setSegment}
          options={[
            { value: "ask", label: "Ask" },
            { value: "auto", label: "Auto" },
            { value: "skip", label: "Skip" },
          ]}
        />
      </Row>
      <Row label="Toggle">
        <Toggle checked={on} onCheckedChange={setOn} label="Example toggle" />
        <Toggle checked={!on} onCheckedChange={(value) => { setOn(!value); }} label="Example toggle, inverse" />
      </Row>
      <Row label="Avatars">
        <Avatar name={members.alex.name} seed={members.alex.org.slug} />
        <Avatar name={members.helen.name} seed={members.helen.org.slug} />
        <Avatar name={members.dana.name} seed={members.dana.org.slug} size={24} />
        <OrgAvatar name={members.helen.org.name} seed={members.helen.org.slug} />
        <OrgAvatar name={members.dana.org.name} seed={members.dana.org.slug} />
        <OrgAvatar name={members.priya.org.name} seed={members.priya.org.slug} size={36} />
      </Row>
      <Row label="Spark">
        <Spark size={20} />
        <Spark size={32} />
        <Spark size={20} animated label="Claude is responding" />
      </Row>
      <Row label="Modal">
        <Button
          onClick={() => {
            setModal(true);
          }}
        >
          Open a modal
        </Button>
        <Modal
          open={modal}
          onOpenChange={setModal}
          title="Share this thread"
          description="Teammates you share with can see the thread and its internal notes."
          footer={
            <>
              <Button
                onClick={() => {
                  setModal(false);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  setModal(false);
                }}
              >
                Share
              </Button>
            </>
          }
        >
          <ModalList>
            {[members.helen, members.raj].map((member) => (
              <div key={member.id} className="flex items-center gap-3 px-4 py-3">
                <Avatar name={member.name} seed={member.org.slug} size={28} />
                <span className="flex flex-col">
                  <span className="text-sm font-medium">{member.name}</span>
                  <span className="text-[13px] text-muted">{member.title}</span>
                </span>
              </div>
            ))}
          </ModalList>
        </Modal>
      </Row>
    </div>
  );
}

function ToastTriggers() {
  return (
    <div className="flex flex-wrap gap-2">
      <Button
        onClick={() => {
          toast({ text: "Switched to Dr. Helen Ward", key: "account-switch" });
        }}
      >
        “Switched to Dr. Helen Ward”
      </Button>
      <Button
        onClick={() => {
          toast({
            text: "Northlake needs a human.",
            action: {
              label: "Switch to Helen",
              onClick: () => undefined,
            },
          });
        }}
      >
        “Northlake needs a human”
      </Button>
      <Button
        onClick={() => {
          toast("Demo reset");
        }}
      >
        “Demo reset”
      </Button>
      <Button
        onClick={() => {
          showUnavailable("Projects");
        }}
      >
        Not-available dialog
      </Button>
    </div>
  );
}

export default [
  {
    title: "App shell",
    description: "Demo bar, sidebar (Recents from /api/conversations) and the page, acting as Alex.",
    layout: "page",
    height: 640,
    render: () => (
      <CurrentShell>
        <HomeStandIn />
      </CurrentShell>
    ),
  },
  {
    title: "Chats page",
    description: "/chats: the chat history list, with search.",
    layout: "page",
    height: 560,
    render: () => (
      <CurrentShell>
        <ChatsPage />
      </CurrentShell>
    ),
  },
  {
    title: "Account menu",
    description:
      "Switch account (featured personas, everyone else in a submenu), demo controls, reset, then Claude's own items, and Lock Harmony.",
    layout: "page",
    height: 900,
    render: () => (
      <CurrentShell sidebar={{ defaultMenuOpen: true }}>
        <div />
      </CurrentShell>
    ),
  },
  {
    title: "Acting as Helen",
    description: "The Requests badge counts the items that need the acting member.",
    layout: "page",
    height: 420,
    fixtures: shellRoutes(members.helen),
    render: () => (
      <CurrentShell>
        <div />
      </CurrentShell>
    ),
  },
  {
    title: "Collapsed sidebar",
    description: "The 48px icon rail, with tooltips.",
    layout: "page",
    height: 560,
    fixtures: shellRoutes(members.helen),
    render: () => (
      <CurrentShell sidebar={{ collapsed: true }}>
        <HomeStandIn />
      </CurrentShell>
    ),
  },
  {
    title: "Switching accounts",
    description:
      "Between act-as and the new member's snapshot: the previous member stays on screen, inert, and dims if the switch is slow.",
    layout: "page",
    height: 560,
    render: () => (
      <CurrentShell status="switching">
        <HomeStandIn />
      </CurrentShell>
    ),
  },
  {
    title: "Can't reach Harmony",
    description: "The session snapshot keeps failing (API down or erroring); the dispatcher retries with backoff.",
    layout: "page",
    height: 560,
    render: () => (
      <ShellFrame session={null} status="offline">
        <div />
      </ShellFrame>
    ),
  },
  {
    title: "Before the demo starts",
    description: "No session yet: nothing is created until the visitor chooses to start.",
    layout: "page",
    height: 520,
    render: () => (
      <ShellFrame session={null}>
        <Landing />
      </ShellFrame>
    ),
  },
  {
    title: "Harmony is locked",
    description:
      "The passcode gate refused a request, or the visitor chose Lock Harmony: shown only while the tab leaves for the passcode page.",
    layout: "page",
    height: 520,
    render: () => <Locked />,
  },
  { title: "Toasts and the not-available dialog", render: () => <ToastTriggers /> },
  { title: "Primitives", render: () => <Primitives /> },
] satisfies GallerySection[];
