"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

interface ConfirmProps {
  /** The button that opens the dialog. */
  label: ReactNode;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  /** If set, the operator must type this exact text to enable confirm. */
  typeToConfirm?: string;
  danger?: boolean;
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
  onConfirm: () => Promise<void> | void;
}

/** A button that asks before acting. Uses the native <dialog> element, so
 * focus is trapped, Escape cancels, and screen readers announce it as a
 * modal. */
export default function ConfirmButton(p: ConfirmProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    const reset = () => {
      setTyped("");
      setError(null);
    };
    d.addEventListener("close", reset);
    return () => d.removeEventListener("close", reset);
  }, []);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await p.onConfirm();
      ref.current?.close();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  const blocked = p.typeToConfirm !== undefined && typed !== p.typeToConfirm;
  return (
    <>
      <button
        type="button"
        className={p.className ?? (p.danger ? "btn btn-danger btn-sm" : "btn btn-ghost btn-sm")}
        disabled={p.disabled}
        aria-label={p.ariaLabel}
        onClick={() => ref.current?.showModal()}
      >
        {p.label}
      </button>
      <dialog ref={ref} className="dialog" aria-labelledby={titleId}>
        <h2 id={titleId} className="dialog-title">
          {p.title}
        </h2>
        <div className="dialog-body">{p.body}</div>
        {p.typeToConfirm !== undefined && (
          <div className="form-row" style={{ marginTop: 12 }}>
            <label htmlFor={`${titleId}-typed`}>
              Type <b className="mono">{p.typeToConfirm}</b> to confirm
            </label>
            <input id={`${titleId}-typed`} value={typed} autoComplete="off" onChange={(e) => setTyped(e.target.value)} />
          </div>
        )}
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        <div className="btn-row dialog-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => ref.current?.close()}>
            Cancel
          </button>
          <button type="button" className={p.danger ? "btn btn-danger btn-sm" : "btn btn-sm"} disabled={busy || blocked} onClick={confirm}>
            {busy ? "Working…" : p.confirmLabel ?? "Confirm"}
          </button>
        </div>
      </dialog>
    </>
  );
}
