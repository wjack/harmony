import { Info, X } from "lucide-react";

import { dismissToast, useToasts } from "./toasts";

/** The toast stack: small white pills at top right, under the demo bar (UI_REFERENCE.md §9). */
export function Toaster() {
  const toasts = useToasts();
  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed right-4 z-[60] flex w-[min(420px,calc(100vw-32px))] flex-col items-end gap-2"
      style={{ top: "calc(var(--demo-bar-height) + 12px)" }}
    >
      {toasts.map((item) => (
        <div
          key={item.id}
          className="pointer-events-auto flex max-w-full animate-toast-in items-center gap-2.5 rounded-[14px] border border-border bg-surface py-1.5 pr-2 pl-3.5 text-sm text-ink shadow-card"
        >
          <Info size={17} strokeWidth={1.5} className="shrink-0 text-ink" aria-hidden />
          <p className="min-w-0 py-1 leading-5">
            {item.text}
            {item.action !== undefined && (
              <>
                {" "}
                <button
                  type="button"
                  className="ml-0.5 underline decoration-muted underline-offset-[3px] hover:decoration-ink"
                  onClick={() => {
                    item.action?.onClick();
                    dismissToast(item.id);
                  }}
                >
                  {item.action.label}
                </button>
              </>
            )}
          </p>
          <button
            type="button"
            aria-label="Dismiss"
            className="inline-flex size-7 shrink-0 items-center justify-center rounded-control text-secondary transition-colors hover:bg-hover hover:text-ink"
            onClick={() => {
              dismissToast(item.id);
            }}
          >
            <X size={16} strokeWidth={1.5} />
          </button>
        </div>
      ))}
    </div>
  );
}
