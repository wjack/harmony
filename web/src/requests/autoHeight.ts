import { useLayoutEffect, useRef, type RefObject } from "react";

/**
 * A ref for a textarea that stays as tall as its text, up to `max` px: when the text
 * changes, when its width does (a narrower window rewraps the text without changing
 * it), and, while it is empty, when its `placeholder` does (an empty field is as tall as
 * the placeholder it shows).
 */
export function useAutoHeight(
  value: string,
  max = Infinity,
  placeholder = "",
): RefObject<HTMLTextAreaElement | null> {
  const field = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = field.current;
    if (element === null) return;
    const fit = () => {
      element.style.height = "auto";
      // The height is the border box (Tailwind's preflight); `scrollHeight` leaves the
      // borders out, so a bordered field would hide its last pixels.
      const borders = element.offsetHeight - element.clientHeight;
      element.style.height = `${String(Math.min(element.scrollHeight + borders, max))}px`;
    };
    fit();
    // Fitting changes the height, never the width, so this can't loop.
    let width = element.clientWidth;
    const observer = new ResizeObserver(() => {
      if (element.clientWidth === width) return;
      width = element.clientWidth;
      fit();
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [value, max, placeholder]);
  return field;
}
