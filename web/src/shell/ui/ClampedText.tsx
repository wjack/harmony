import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { cx } from "./cx";

interface ClampedTextProps {
  children: ReactNode;
  /** How many lines show while collapsed. */
  lines: number;
  className?: string;
}

function clamp(lines: number): CSSProperties {
  return { display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: lines, overflow: "hidden" };
}

/**
 * Text clamped to `lines` lines, with "View more" / "View less" only when the clamp
 * cuts something off (LIVE_DEMO_PLAN 3.11). That is measured, never guessed from the
 * length: `scrollHeight > clientHeight` while clamped, again whenever the box resizes
 * (a narrower card, a font that loaded late) or the text changes (a clamped box keeps
 * its height as a redraft or a stream fills it).
 */
export function ClampedText({ children, lines, className }: ClampedTextProps) {
  const textId = useId();
  const textRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [cutOff, setCutOff] = useState(false);

  // Only while clamped: expanded, nothing is cut off, and "View less" must stay.
  useEffect(() => {
    const text = textRef.current;
    if (text === null || expanded) return;
    const measure = () => {
      setCutOff(text.scrollHeight > text.clientHeight);
    };
    const resized = new ResizeObserver(measure);
    resized.observe(text);
    const changed = new MutationObserver(measure);
    changed.observe(text, { childList: true, characterData: true, subtree: true });
    return () => {
      resized.disconnect();
      changed.disconnect();
    };
  }, [expanded, lines]);

  return (
    <div className={className}>
      <div id={textId} ref={textRef} style={expanded ? undefined : clamp(lines)}>
        {children}
      </div>
      {(cutOff || expanded) && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={textId}
          onClick={() => {
            setExpanded((value) => !value);
          }}
          className={cx(
            "mt-1 text-[12.5px] font-medium text-secondary underline decoration-border-strong underline-offset-2",
            "hover:text-ink",
          )}
        >
          {expanded ? "View less" : "View more"}
        </button>
      )}
    </div>
  );
}
