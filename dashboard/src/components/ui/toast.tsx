import { useState, useEffect } from "react";

interface Toast {
  id: number;
  message: string;
  type: "success" | "error";
}

let _toasts: Toast[] = [];
let _nextId = 0;
const _listeners: Set<() => void> = new Set();

function notify() {
  _listeners.forEach((l) => l());
}

export function showToast(message: string, type: "success" | "error") {
  const id = _nextId++;
  _toasts = [..._toasts, { id, message, type }];
  notify();
  setTimeout(() => {
    _toasts = _toasts.filter((t) => t.id !== id);
    notify();
  }, 4000);
}

export function ToastContainer() {
  const [, forceUpdate] = useState(0);

  useEffect(() => {
    const listener = () => forceUpdate((n) => n + 1);
    _listeners.add(listener);
    return () => {
      _listeners.delete(listener);
    };
  }, []);

  return (
    <div className="fixed top-4 right-4 z-[200] flex flex-col gap-2" role="status" aria-live="polite">
      {_toasts.map((t) => (
        <div
          key={t.id}
          className={
            t.type === "success"
              ? "toast-success border border-green-500"
              : "toast-error border border-red-500"
          }
          style={{
            padding: "10px 16px",
            borderRadius: "8px",
            fontSize: "13px",
            maxWidth: "360px",
            animation: "slideIn 0.3s ease, fadeOut 0.5s ease 3.5s forwards",
          }}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}
