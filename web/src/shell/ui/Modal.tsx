import { X } from "lucide-react";
import { Dialog } from "radix-ui";
import { useRef, type ReactNode } from "react";

import { cx } from "./cx";

export interface ModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  /** The muted line under the title. */
  description?: ReactNode;
  /** Shown above the title (the spark on the not-available dialog). */
  icon?: ReactNode;
  /** Buttons, right-aligned: a white "Cancel" then the black primary. */
  footer?: ReactNode;
  /** Content width in px; the Claude app's modals are about 480. */
  width?: number;
  children?: ReactNode;
  className?: string;
}

/**
 * Claude's modal shell (UI_REFERENCE.md §9): white, 12px radius, over a dim scrim, with
 * a semibold title, × at top right and a muted description. Focus is trapped and
 * restored, and Escape or a scrim click closes it.
 */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  icon,
  footer,
  width = 480,
  children,
  className,
}: ModalProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-scrim data-[state=open]:animate-fade-in" />
        <Dialog.Content
          ref={contentRef}
          // Focus the dialog itself rather than its first button (the ×), so opening
          // doesn't draw a focus ring; a child with autoFocus still takes focus first.
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            contentRef.current?.focus({ preventScroll: true });
          }}
          style={{ width: `min(${width}px, calc(100vw - 32px))` }}
          className={cx(
            "fixed top-1/2 left-1/2 z-50 flex max-h-[min(88vh,820px)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-menu bg-surface text-ink shadow-modal outline-none data-[state=open]:animate-pop-in",
            className,
          )}
          // A dialog without a description is fine; tell Radix so it doesn't warn.
          {...(description === undefined ? { "aria-describedby": undefined } : {})}
        >
          <div className="flex items-start gap-3 px-7 pt-7">
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              {icon !== undefined && <div className="mb-2.5">{icon}</div>}
              <Dialog.Title className="text-xl leading-7 font-semibold tracking-[-0.01em]">{title}</Dialog.Title>
              {description !== undefined && (
                <Dialog.Description className="text-[15px] leading-[22px] text-muted">{description}</Dialog.Description>
              )}
            </div>
            <Dialog.Close
              aria-label="Close"
              className="-mt-0.5 -mr-2 inline-flex size-8 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:bg-hover hover:text-ink"
            >
              <X size={20} strokeWidth={1.5} />
            </Dialog.Close>
          </div>
          {children !== undefined && children !== null && children !== false && (
            <div className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto px-7 pt-5">{children}</div>
          )}
          {footer !== undefined ? (
            <div className="flex items-center justify-end gap-2 px-7 pt-6 pb-7">{footer}</div>
          ) : (
            <div className="pb-7" />
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** A bordered list box inside a modal, rows divided by hairlines (the "Chat shared" pattern). */
export function ModalList({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx("divide-y divide-border overflow-hidden rounded-menu border border-border", className)}>
      {children}
    </div>
  );
}
