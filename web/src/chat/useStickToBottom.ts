/**
 * Keeps the transcript pinned to its newest message while the member is at the bottom,
 * as Claude does, and lets go when they scroll up to read.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** How close to the bottom still counts as at the bottom. */
export const STICK_TO_BOTTOM_PX = 96;

/** How long after a wheel, touch or key a scroll still counts as the member's. */
const GESTURE_MS = 300;

const SCROLL_KEYS: ReadonlySet<string> = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

export interface ScrollPosition {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/**
 * Whether the view stays pinned after a scroll event. Only the member scrolling up
 * lets go. Other scrolls can leave the view far from the bottom without anyone moving
 * it: content that grows between a scroll to the bottom and the event it fires, or the
 * browser's scroll anchoring when a card above loads. The next resize puts it back.
 */
export function staysPinned(
  pinned: boolean,
  previousTop: number,
  position: ScrollPosition,
  byMember: boolean,
): boolean {
  const gap = position.scrollHeight - position.scrollTop - position.clientHeight;
  if (gap < STICK_TO_BOTTOM_PX) return true;
  return pinned && !(byMember && position.scrollTop < previousTop);
}

function isEditable(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
  );
}

export function useStickToBottom(deps: readonly unknown[]) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const pinned = useRef(true);
  const lastTop = useRef(0);
  const gestureAt = useRef(Number.NEGATIVE_INFINITY);
  /** A pointer is down in the transcript: dragging its scrollbar, or selecting text. */
  const pointerDown = useRef(false);
  const [atBottom, setAtBottom] = useState(true);

  const toBottom = (behavior: "instant" | "smooth" = "instant") => {
    const element = scrollRef.current;
    if (element !== null) element.scrollTo({ top: element.scrollHeight, behavior });
  };

  useLayoutEffect(() => {
    if (pinned.current) toBottom();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the caller lists what changes the content
  }, deps);

  // Content that grows (a card's data arriving) and a viewport that shrinks (a banner
  // opening over the composer) both move the bottom; follow it while pinned.
  useEffect(() => {
    const content = contentRef.current;
    const viewport = scrollRef.current;
    if (content === null || viewport === null) return;
    const observer = new ResizeObserver(() => {
      if (pinned.current) toBottom();
    });
    observer.observe(content);
    observer.observe(viewport);
    return () => {
      observer.disconnect();
    };
  }, []);

  // What the member does to scroll, so their scrolls can be told from everyone else's.
  useEffect(() => {
    const viewport = scrollRef.current;
    if (viewport === null) return;
    const gesture = () => {
      gestureAt.current = performance.now();
    };
    const key = (event: KeyboardEvent) => {
      if (SCROLL_KEYS.has(event.key) && !isEditable(event.target)) gesture();
    };
    const hold = () => {
      pointerDown.current = true;
    };
    const release = () => {
      pointerDown.current = false;
    };
    viewport.addEventListener("wheel", gesture, { passive: true });
    viewport.addEventListener("touchmove", gesture, { passive: true });
    viewport.addEventListener("pointerdown", hold);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    window.addEventListener("keydown", key);
    return () => {
      viewport.removeEventListener("wheel", gesture);
      viewport.removeEventListener("touchmove", gesture);
      viewport.removeEventListener("pointerdown", hold);
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
      window.removeEventListener("keydown", key);
    };
  }, []);

  const onScroll = () => {
    const element = scrollRef.current;
    if (element === null) return;
    const byMember = pointerDown.current || performance.now() - gestureAt.current < GESTURE_MS;
    pinned.current = staysPinned(pinned.current, lastTop.current, element, byMember);
    lastTop.current = element.scrollTop;
    setAtBottom(pinned.current);
  };

  return { scrollRef, contentRef, atBottom, onScroll, toBottom };
}
