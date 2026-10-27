import { ClampedText } from "../../shell/ui/ClampedText";
import { cx } from "../../shell/ui/cx";

/**
 * What one organization is asked (LIVE_DEMO_PLAN 3.13 E2, E7): its conversation's
 * subject, which both sides read, and its own ask, which only our representative in that
 * conversation works from. Set off by a hairline, like a quoted message.
 */
export function RecipientAsk({
  title,
  ask,
  className,
}: {
  title: string | null | undefined;
  ask: string | null | undefined;
  className?: string;
}) {
  if (!title && !ask) return null;
  return (
    <div className={cx("border-l-2 border-border pl-3", className)}>
      {title && (
        <ClampedText lines={2} className="text-[13.5px] leading-5 font-medium text-ink">
          {title}
        </ClampedText>
      )}
      {ask && (
        <ClampedText lines={2} className={cx("text-[13px] leading-5 text-secondary", title && "mt-0.5")}>
          {ask}
        </ClampedText>
      )}
    </div>
  );
}
