// Dialog primitives.
//
// The three dialogs in this app were hand-rolled divs: no role="dialog", no
// aria-modal, no focus trap, no Escape handler. Tabbing out of "Edit draft"
// silently landed you on the draft rows behind the overlay, and Escape did
// nothing. Radix supplies focus trapping, Escape, scroll lock, ARIA wiring and
// portalling; the styling below stays ours.

import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/cn";

type BaseProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  /**
   * Asked before the dialog closes via Escape or a backdrop click. Return false
   * to keep it open. Used to guard unsaved edits — Escape closing a dialog
   * holding a 14,000-character body would otherwise be a new way to lose work.
   */
  onRequestClose?: () => boolean;
};

function guard(onRequestClose: (() => boolean) | undefined, e: Event) {
  if (onRequestClose && !onRequestClose()) e.preventDefault();
}

/**
 * Standard dashboard dialog: themed panel, title bar, close button.
 */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  footer,
  children,
  className,
  onRequestClose,
}: BaseProps & {
  title: string;
  description?: string;
  footer?: ReactNode;
  className?: string;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
        <Dialog.Content
          onEscapeKeyDown={(e) => guard(onRequestClose, e)}
          onPointerDownOutside={(e) => guard(onRequestClose, e)}
          className={cn(
            "fixed z-50 left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2",
            "w-[calc(100vw-1.5rem)] sm:w-full sm:max-w-3xl",
            "max-h-[92vh] flex flex-col",
            "bg-surface border border-border-strong rounded-lg shadow-2xl",
            "focus:outline-none",
            className,
          )}
        >
          <div className="p-3 border-b border-border flex items-start justify-between gap-3">
            <div className="min-w-0">
              <Dialog.Title className="font-medium">{title}</Dialog.Title>
              {description ? (
                <Dialog.Description className="text-xs text-fg-subtle mt-0.5">
                  {description}
                </Dialog.Description>
              ) : (
                // Radix warns when Content has no Description; this keeps the
                // announcement correct without rendering visible chrome.
                <Dialog.Description className="sr-only">{title}</Dialog.Description>
              )}
            </div>
            <Dialog.Close
              aria-label="Close dialog"
              className="shrink-0 rounded p-1 text-fg-muted hover:text-fg hover:bg-surface-2"
            >
              <X size={18} aria-hidden="true" />
            </Dialog.Close>
          </div>

          <div className="p-3 space-y-3 overflow-auto">{children}</div>

          {footer && (
            <div className="p-3 border-t border-border flex flex-wrap justify-end gap-2">
              {footer}
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * Overlay + focus management with no panel styling — the caller renders its
 * own surface. Exists for the Craigslist preview, which must stay
 * light-on-white in both themes and therefore cannot use the themed panel.
 */
export function RawModal({
  open,
  onOpenChange,
  children,
  className,
  label,
  onRequestClose,
}: BaseProps & { className?: string; label: string }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/80 z-40" />
        <Dialog.Content
          aria-label={label}
          onEscapeKeyDown={(e) => guard(onRequestClose, e)}
          onPointerDownOutside={(e) => guard(onRequestClose, e)}
          className={cn(
            "fixed z-50 inset-0 overflow-auto focus:outline-none",
            "flex items-start justify-center p-3 sm:p-4",
            className,
          )}
        >
          <Dialog.Title className="sr-only">{label}</Dialog.Title>
          <Dialog.Description className="sr-only">{label}</Dialog.Description>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export const ModalClose = Dialog.Close;

/**
 * Confirmation for destructive actions. Delete sat immediately beside four
 * benign buttons and fired on a single click with no undo.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel = "Delete",
  onConfirm,
  busy,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  busy?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
        <Dialog.Content
          className={cn(
            "fixed z-50 left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2",
            "w-[calc(100vw-1.5rem)] sm:w-full sm:max-w-md",
            "bg-surface border border-border-strong rounded-lg shadow-2xl p-4 space-y-3",
            "focus:outline-none",
          )}
        >
          <Dialog.Title className="font-medium">{title}</Dialog.Title>
          <Dialog.Description asChild>
            <div className="text-sm text-fg-muted">{body}</div>
          </Dialog.Description>
          <div className="flex justify-end gap-2 pt-1">
            <Dialog.Close className="px-3 py-1.5 rounded text-sm text-fg-muted hover:bg-surface-2">
              Cancel
            </Dialog.Close>
            <button
              autoFocus
              disabled={busy}
              onClick={onConfirm}
              className="px-3 py-1.5 rounded text-sm bg-danger-solid text-on-solid hover:opacity-90 disabled:opacity-40"
            >
              {confirmLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
