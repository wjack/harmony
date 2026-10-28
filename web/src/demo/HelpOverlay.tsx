import { CircleQuestionMark } from "lucide-react";
import { Dialog } from "radix-ui";
import { useLayoutEffect, useRef, useState, type RefObject } from "react";

import { useRealtime } from "../app/context";
import type { RealtimeStatus } from "../realtime/dispatcher";
import { grow, placeCallouts, RING, type Box, type Placement, type Side, type Size } from "./helpLayout";
import { finishIntroHelp, useDemoState } from "./store";

interface Callout {
  /** The `data-help` value on the control it explains. */
  key: string;
  side: Side;
  text: string;
  /** Read out first, where the line doesn't name its control. */
  name?: string;
  /** Less central: it gives way first when the window is short of room. */
  extra?: boolean;
}

/** Along the demo bar, then the sidebar: the order they're read in. */
const CALLOUTS: readonly Callout[] = [
  {
    key: "demo-controls",
    side: "below",
    name: "Demo controls",
    text: "You can turn on dangerous permission skipping to stop agents from asking for human intervention.",
  },
  {
    key: "network",
    side: "below",
    text: "Network view lets you see what's going on in the Harmony Network and switch accounts. It's a demo-only feature.",
  },
  {
    key: "acting-as",
    side: "below",
    name: "Acting as",
    text: "This is the person and organization you're currently acting as.",
  },
  {
    key: "budget",
    side: "below",
    extra: true,
    name: "Live budget",
    text: "What's left of this session's budget for live models. Reset doesn't refill it.",
  },
  {
    key: "reset",
    side: "below",
    extra: true,
    text: "Reset starts a fresh copy of the network, back at the start of the story.",
  },
  { key: "requests", side: "right", text: "Requests is an inbox where you receive messages from other Claude agents." },
];

/** A frame up as the session's member; during a start or a switch the one on screen is on its way out. */
const LIVE: ReadonlySet<RealtimeStatus> = new Set(["connecting", "open", "reconnecting"]);

/**
 * The demo bar's "?" (ARCHITECTURE.md §11.4): "What's on screen", a cheat sheet over the
 * whole app. A light scrim, a ring on each of the demo's key controls and a callout with
 * an arrow saying what it does, all at once; a control that isn't on screen goes without
 * one. The intro's Next opens it too, once the demo is up as Alex, and closing that one
 * leaves the suggested message in New chat's composer.
 */
export function HelpButton() {
  const [open, setOpen] = useState(false);
  const live = useRealtime((state) => LIVE.has(state.status));
  const fromIntro = useDemoState((state) => state.introHelp) && live;
  const trigger = useRef<HTMLButtonElement>(null);
  // Set as it closes: the intro's turn hands focus to the composer instead of the "?".
  const handingOff = useRef(false);

  const close = () => {
    setOpen(false);
    handingOff.current = fromIntro;
    if (fromIntro) finishIntroHelp();
  };

  return (
    <Dialog.Root
      open={open || fromIntro}
      onOpenChange={(next) => {
        if (next) setOpen(true);
        else close();
      }}
    >
      <Dialog.Trigger asChild>
        <button
          ref={trigger}
          type="button"
          aria-label="What's on screen"
          title="What's on screen"
          className="inline-flex size-6 shrink-0 items-center justify-center rounded-full text-demo-muted transition-colors hover:bg-demo-surface hover:text-demo-text"
        >
          <CircleQuestionMark size={15} strokeWidth={1.75} />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Content
          onCloseAutoFocus={(event) => {
            if (handingOff.current) event.preventDefault();
          }}
          // Anywhere closes it, the scrim and the "?" under it included.
          onClick={close}
          className="fixed inset-0 z-50 outline-none data-[state=open]:animate-fade-in"
        >
          <Overlay trigger={trigger} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * The scrim, with a ring cut out around each control, the arrows and the callouts. It
 * measures the controls and places the callouts (helpLayout.ts) as it opens, and again
 * whenever the window, a scroll or the frame under it moves them.
 */
function Overlay({ trigger }: { trigger: RefObject<HTMLButtonElement | null> }) {
  const list = useRef<HTMLUListElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [placements, setPlacements] = useState<Placement[]>([]);

  useLayoutEffect(() => {
    // The frame the "?" is in (the gallery shows several), else the page.
    const scope = trigger.current?.closest("[data-help-scope]") ?? document;
    let shown = "";
    let frame = 0;
    const measure = () => {
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      const specs = CALLOUTS.flatMap(({ key, side, extra }) => {
        const callout = list.current?.querySelector<HTMLElement>(`[data-callout="${key}"]`) ?? null;
        const target = firstOnScreen(scope.querySelectorAll(`[data-help="${key}"]`), viewport);
        if (callout === null || target === null) return [];
        return [{ key, side, extra, target, width: callout.offsetWidth, height: callout.offsetHeight }];
      });
      const obstacles = [trigger.current, panel.current].flatMap((element) => {
        const box = element === null ? null : onScreen(element, viewport);
        return box === null ? [] : [box];
      });
      const next = placeCallouts(specs, viewport, obstacles);
      // The frame re-renders under it all the time; most of that moves nothing.
      const layout = JSON.stringify(next);
      if (layout === shown) return;
      shown = layout;
      setPlacements(next);
    };
    const remeasure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    measure();
    // Controls come and go while it's up: the budget loads, a label drops as the bar
    // narrows, the sidebar folds to its rail.
    const observer = new MutationObserver(remeasure);
    observer.observe(scope, { childList: true, subtree: true, characterData: true });
    scope.addEventListener("transitionend", remeasure, true);
    window.addEventListener("resize", remeasure);
    // Capturing: the sidebar and the page scroll in boxes of their own.
    window.addEventListener("scroll", remeasure, true);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      scope.removeEventListener("transitionend", remeasure, true);
      window.removeEventListener("resize", remeasure);
      window.removeEventListener("scroll", remeasure, true);
    };
  }, [trigger]);

  return (
    <>
      <svg aria-hidden className="absolute inset-0 size-full">
        <defs>
          <mask id="help-spotlight">
            <rect width="100%" height="100%" fill="white" />
            {placements.map(({ key, target }) => (
              <rect key={key} {...ringRect(target)} fill="black" />
            ))}
          </mask>
          <marker
            id="help-arrowhead"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto"
          >
            <path d="M 1 1 L 9 5 L 1 9" strokeWidth={1.5} strokeLinecap="round" className="fill-none stroke-accent" />
          </marker>
        </defs>
        <rect width="100%" height="100%" mask="url(#help-spotlight)" className="fill-scrim/60" />
        {placements.map(({ key, target, arrow }) => (
          <g key={key} strokeWidth={1.5} className="fill-none stroke-accent">
            <rect {...ringRect(target)} />
            <path d={arrow} strokeDasharray="4 4" markerEnd="url(#help-arrowhead)" />
          </g>
        ))}
      </svg>

      <ul ref={list}>
        {CALLOUTS.map(({ key, name, text }) => {
          const placed = placements.find((placement) => placement.key === key);
          return (
            <li
              key={key}
              data-callout={key}
              // Laid out unseen first, to be measured; one with no room or no control stays so.
              aria-hidden={placed === undefined}
              style={placed === undefined ? undefined : { left: placed.box.left, top: placed.box.top }}
              className={`absolute top-0 left-0 w-[216px] rounded-[8px] bg-demo px-3 py-2.5 text-[13px] leading-[18px] text-demo-text shadow-menu ring-1 ring-demo-border ${placed === undefined ? "invisible" : ""}`}
            >
              {name !== undefined && <span className="sr-only">{name}: </span>}
              <span>{text}</span>
            </li>
          );
        })}
      </ul>

      <div
        ref={panel}
        className="absolute bottom-6 left-1/2 flex -translate-x-1/2 items-center gap-3 rounded-full bg-demo py-1.5 pr-1.5 pl-4 text-[13px] whitespace-nowrap text-demo-text shadow-modal ring-1 ring-demo-border"
      >
        <Dialog.Title className="font-medium">What&apos;s on screen</Dialog.Title>
        <Dialog.Description className="text-demo-muted">Click anywhere or press Esc to close.</Dialog.Description>
        <Dialog.Close className="inline-flex h-7 items-center rounded-full bg-demo-text px-3 font-medium text-demo transition-colors hover:bg-white">
          Got it
        </Dialog.Close>
      </div>
    </>
  );
}

function ringRect(target: Box) {
  const { left, top, right, bottom } = grow(target, RING);
  return { x: left, y: top, width: right - left, height: bottom - top, rx: 8 };
}

function firstOnScreen(elements: NodeListOf<Element>, viewport: Size): Box | null {
  for (const element of elements) {
    const box = onScreen(element, viewport);
    if (box !== null) return box;
  }
  return null;
}

const CLIPS = new Set(["hidden", "clip", "auto", "scroll"]);
/** Less than this much of a control showing isn't worth a callout. */
const MIN_SHOWING = 8;

/** What of `element` is on screen: cut by each ancestor that clips its overflow, and by the viewport. */
function onScreen(element: Element, viewport: Size): Box | null {
  let { left, top, right, bottom } = element.getBoundingClientRect();
  for (let clip = element.parentElement; clip !== null; clip = clip.parentElement) {
    const { overflowX, overflowY, position } = getComputedStyle(clip);
    if (CLIPS.has(overflowX) || CLIPS.has(overflowY)) {
      const edges = clip.getBoundingClientRect();
      left = Math.max(left, edges.left);
      top = Math.max(top, edges.top);
      right = Math.min(right, edges.right);
      bottom = Math.min(bottom, edges.bottom);
    }
    // Nothing above a fixed box clips it.
    if (position === "fixed") break;
  }
  left = Math.max(left, 0);
  top = Math.max(top, 0);
  right = Math.min(right, viewport.width);
  bottom = Math.min(bottom, viewport.height);
  return right - left >= MIN_SHOWING && bottom - top >= MIN_SHOWING ? { left, top, right, bottom } : null;
}
