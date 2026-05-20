import type React from 'react';
import { createPortal } from 'react-dom';

export interface ModalDialogProps {
  isOpen: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  /** Tailwind max-width class, e.g. max-w-3xl */
  maxWidthClass?: string;
}

/**
 * Full-screen dimmed overlay with a centered panel (view / detail flows).
 * Click backdrop to close.
 */
export const ModalDialog: React.FC<ModalDialogProps> = ({
  isOpen,
  title,
  onClose,
  children,
  maxWidthClass = 'max-w-3xl',
}) => {
  if (!isOpen) return null;

  const dialog = (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        className={`flex max-h-[90vh] w-full flex-col overflow-hidden rounded-xl border border-border/70 bg-elevated shadow-2xl animate-in fade-in zoom-in duration-200 ${maxWidthClass}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-dialog-title"
      >
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-5 py-4">
          <h2 id="modal-dialog-title" className="text-lg font-medium leading-snug text-foreground">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-secondary-text transition-colors hover:bg-hover hover:text-foreground"
          >
            关闭
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  );

  return createPortal(dialog, document.body);
};
