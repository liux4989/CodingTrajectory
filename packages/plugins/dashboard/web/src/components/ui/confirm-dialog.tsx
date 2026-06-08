import * as React from "react";

type Props = {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  variant?: "default" | "destructive";
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({ open, title, description, confirmLabel = "Confirm", variant = "default", onConfirm, onCancel }: Props) {
  const ref = React.useRef<HTMLDialogElement>(null);

  React.useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog ref={ref} className="confirm-dialog" onClose={onCancel}>
      <div className="confirm-dialog-body">
        <h3>{title}</h3>
        <p>{description}</p>
        <div className="confirm-dialog-actions">
          <button className="button button-secondary" onClick={onCancel}>Cancel</button>
          <button className={`button ${variant === "destructive" ? "button-destructive" : "button-default"}`} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
