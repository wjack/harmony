/**
 * Keeps the transcript pinned to its newest message while the member is at the bottom,
 * as Claude does, and lets go when they scroll up to read, or when following the bottom
 * would scroll away what they just unfolded (View more, a row's details).
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** How close to the bottom still counts as at the bottom. */
export const STICK_TO_BOTTOM_PX = 96;

/** The room left above something brought into view from its top. */
const REVEAL_MARGIN_PX = 16;

/**
 * How long after a wheel, touch or key a scroll still counts as the member's, and after
 * a click on a disclosure a resize still counts as its unfolding.
 */
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

/**
 * Whether following the bottom now would scroll something whose top is `top` px below
 * the viewport's top out of view: the follow moves everything up by the gap left under
 * the view.
 */
export function followHides(top: number, position: ScrollPosition): boolean {
  return top < position.scrollHeight - position.scrollTop - position.clientHeight;
}

/**
 * What a click unfolds: the region a closed disclosure controls, or the disclosure
 * itself, whose content opens under it. Null for any other click.
 */
function unfoldedBy(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) return null;
  const toggle = target.closest<HTMLElement>('[aria-expanded="false"]');
  if (toggle === null) return null;
  const controls = toggle.getAttribute("aria-controls");
  return (controls === null ? null : document.getElementById(controls)) ?? toggle;
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
  /** What the member just unfolded in the transcript, and when. */
  const unfolded = useRef<{ target: HTMLElement; at: number } | null>(null);
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
  // opening over the composer) both move the bottom; follow it while pinned. Growth the
  // member asked for by unfolding something is theirs to read: if following would
  // scroll its top away, let go there instead, as if they had scrolled up to read it.
  useEffect(() => {
    const content = contentRef.current;
    const viewport = scrollRef.current;
    if (content === null || viewport === null) return;
    const unfold = (event: MouseEvent) => {
      const target = unfoldedBy(event.target);
      if (target !== null && content.contains(target)) unfolded.current = { target, at: performance.now() };
    };
    const observer = new ResizeObserver(() => {
      const unfolding = unfolded.current;
      unfolded.current = null;
      if (!pinned.current) return;
      if (unfolding !== null && performance.now() - unfolding.at < GESTURE_MS) {
        const top = unfolding.target.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
        if (followHides(top, viewport)) {
          pinned.current = false;
          lastTop.current = viewport.scrollTop;
          setAtBottom(false);
          return;
        }
      }
      toBottom();
    });
    viewport.addEventListener("click", unfold, { capture: true });
    observer.observe(content);
    observer.observe(viewport);
    return () => {
      viewport.removeEventListener("click", unfold, { capture: true });
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

  /**
   * While following the bottom, shows `target` from its top if the bottom would hide
   * its top, and stops following there, as if the member had scrolled up to read it.
   */
  const revealTop = (target: HTMLElement) => {
    const viewport = scrollRef.current;
    if (viewport === null || !pinned.current) return;
    const top =
      target.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop - REVEAL_MARGIN_PX;
    if (top >= viewport.scrollHeight - viewport.clientHeight) return;
    pinned.current = false;
    viewport.scrollTop = top;
    lastTop.current = viewport.scrollTop;
    setAtBottom(false);
  };

  return { scrollRef, contentRef, atBottom, onScroll, toBottom, revealTop };
}
