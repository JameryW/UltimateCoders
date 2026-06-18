import { useState, useEffect } from "react";

interface ConfirmState {
  title: string;
  message: string;
  resolve: (value: boolean) => void;
}

let _confirmState: ConfirmState | null = null;
const _listeners: Set<() => void> = new Set();

function notify() {
  _listeners.forEach((l) => l());
}

export function confirmAction(title: string, message: string): Promise<boolean> {
  return new Promise((resolve) => {
    _confirmState = { title, message, resolve };
    notify();
  });
}

export function useConfirmDialog() {
  const [, forceUpdate] = useState(0);

  useEffect(() => {
    const listener = () => forceUpdate((n) => n + 1);
    _listeners.add(listener);
    return () => {
      _listeners.delete(listener);
    };
  }, []);

  const state = _confirmState;

  const handleOk = () => {
    state?.resolve(true);
    _confirmState = null;
    notify();
  };

  const handleCancel = () => {
    state?.resolve(false);
    _confirmState = null;
    notify();
  };

  return { state, handleOk, handleCancel };
}

export function ConfirmDialog() {
  const { state, handleOk, handleCancel } = useConfirmDialog();

  if (!state) return null;

  return (
    <div
      className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label={state.title}
      onKeyDown={(e) => {
        if (e.key === "Escape") handleCancel();
      }}
    >
      <div className="bg-[var(--bg-surface)] border border-[var(--border-color)] rounded-xl p-6 max-w-sm w-[90%]">
        <h3 className="text-lg font-semibold text-[var(--text-primary)] mb-2">
          {state.title}
        </h3>
        <p className="text-sm text-[var(--text-secondary)] mb-4">{state.message}</p>
        <div className="flex justify-end gap-3">
          <button
            className="px-4 py-2 text-sm rounded bg-[var(--bg-surface-alt)] text-[var(--text-secondary)] hover:bg-[var(--border-color)]"
            onClick={handleCancel}
          >
            Cancel
          </button>
          <button
            className="px-4 py-2 text-sm rounded bg-red-800 text-red-200 hover:bg-red-700"
            onClick={handleOk}
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
