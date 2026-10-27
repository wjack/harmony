import { useEffect, useEffectEvent, type RefObject } from "react";

/**
 * ⌘↵ / Ctrl↵ approves while focus is inside `ref` (the card), and does nothing with
 * focus anywhere else. A key a field inside already handled (`preventDefault`, as the
 * Decline step's reason does) doesn't approve.
 */
export function useApproveShortcut(
  ref: RefObject<HTMLElement | null>,
  onApprove: () => void,
  enabled: boolean,
): void {
  const approve = useEffectEvent(onApprove);
  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey)) return;
      if (event.defaultPrevented || event.isComposing) return;
      const card = ref.current;
      if (card === null || !(event.target instanceof Node) || !card.contains(event.target)) return;
      event.preventDefault();
      approve();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [ref, enabled]);
}
