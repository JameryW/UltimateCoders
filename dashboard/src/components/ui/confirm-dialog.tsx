import { useState, useCallback } from "react";

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

  // Subscribe to confirm state changes
  if (!_listeners.size) {
    // only subscribe once (react strict mode runs twice but set dedupes)
  }
  // Use effect-like subscription pattern
  const listener = useCallback(() => forceUpdate((n) => n + 1), []);
  // Simplified: just read global state
  void listener; // used indirectly through re-render

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
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center">
      <div className="bg-dark-800 border border-dark-700 rounded-xl p-6 max-w-sm w-[90%]">
        <h3 className="text-lg font-semibold text-white mb-2">
          {state.title}
        </h3>
        <p className="text-sm text-gray-300 mb-4">{state.message}</p>
        <div className="flex justify-end gap-3">
          <button
            className="px-4 py-2 text-sm rounded bg-gray-700 text-gray-300 hover:bg-gray-600"
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
