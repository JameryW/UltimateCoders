import { useEffect, useRef, useCallback, useState } from "react";
import type { Terminal } from "@xterm/xterm";
import type { FitAddon } from "@xterm/addon-fit";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import "@xterm/xterm/css/xterm.css";

type ConnState = "connecting" | "connected" | "disconnected" | "error";

/** Max reconnect delay in milliseconds. */
const RECONNECT_MAX_DELAY = 10_000;
/** Base delay for exponential backoff. */
const RECONNECT_BASE_DELAY = 1_000;

export default function TuiPage() {
  const auth = useAuth();
  const { theme } = useTheme();
  const [redirect, setRedirect] = useState(false);

  // ponytail: F68 — the effect MUST run before any early return (React hook
  // order: skipping it while isChecking added a 4th hook once checking
  // finished → "Rendered more hooks than during the previous render" crash on
  // every #/tui load). Also gate on !isChecking: isAuthenticated is false
  // during the check, so an ungated effect redirected authenticated users to
  // login before their token was validated.
  useEffect(() => {
    if (!auth.isChecking && !auth.isAuthenticated) setRedirect(true);
  }, [auth.isChecking, auth.isAuthenticated]);

  // Auth gate
  if (auth.isChecking) {
    return (
      <div className="flex items-center justify-center h-screen text-[var(--text-secondary)]">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-400" />
      </div>
    );
  }
  if (redirect) {
    return (
      <div className="flex items-center justify-center h-screen text-[var(--text-secondary)]">
        <p>Redirecting to login...</p>
        <a href="#/" className="ml-2 text-blue-400 underline">click here</a>
      </div>
    );
  }

  return <TuiTerminal auth={auth} theme={theme} />;
}

function TuiTerminal({ auth, theme }: { auth: { token: string | null }; theme: string }) {
  const termRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const [connState, setConnState] = useState<ConnState>("disconnected");
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    // Clear any pending reconnect timer
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    // Build ws URL with auth token
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const params = auth.token ? `?token=${encodeURIComponent(auth.token)}` : "";
    const url = `${proto}//${location.host}/ws/tui${params}`;

    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    setConnState("connecting");

    ws.onopen = () => {
      reconnectAttemptRef.current = 0;
      setConnState("connected");
    };
    ws.onclose = () => {
      setConnState("disconnected");
      wsRef.current = null;
    };
    ws.onerror = () => setConnState("error");

    ws.onmessage = (ev) => {
      const term = xtermRef.current;
      if (!term) return;
      if (ev.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(ev.data));
      } else {
        term.write(ev.data);
      }
    };
  }, [auth.token]);

  // Init xterm + connect on mount
  useEffect(() => {
    const container = termRef.current;
    if (!container) return;

    let term: Terminal;
    let fitAddon: FitAddon;

    (async () => {
      const { Terminal } = await import("@xterm/xterm");
      const { FitAddon } = await import("@xterm/addon-fit");

      term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: "Menlo, Monaco, 'Courier New', monospace",
        theme: theme === "dark"
          ? { background: "#1a1b26", foreground: "#c0caf5" }
          : { background: "#f8f5f0", foreground: "#343b58" },
        scrollback: 5000,
        convertEol: true,
      });

      fitAddon = new FitAddon();
      term.loadAddon(fitAddon);
      term.open(container);
      fitAddon.fit();

      term.onData((data: string) => {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(data);
        }
      });

      xtermRef.current = term;
      fitAddonRef.current = fitAddon;
      connect();
    })();

    const onResize = () => fitAddon?.fit();
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
      }
      term?.dispose();
      wsRef.current?.close();
    };
  }, [connect, theme]);

  // Auto-reconnect with exponential backoff
  useEffect(() => {
    if (connState === "disconnected" || connState === "error") {
      const attempt = reconnectAttemptRef.current;
      const delay = Math.min(RECONNECT_BASE_DELAY * Math.pow(2, attempt), RECONNECT_MAX_DELAY);
      reconnectAttemptRef.current = attempt + 1;
      reconnectTimerRef.current = setTimeout(() => {
        connect();
      }, delay);
    }
    if (connState === "connected") {
      reconnectAttemptRef.current = 0;
    }
  }, [connect, connState]);

  const connColor: Record<ConnState, string> = {
    connecting: "text-yellow-400",
    connected: "text-green-400",
    disconnected: "text-gray-400",
    error: "text-red-400",
  };

  return (
    <div className="flex flex-col h-screen bg-[var(--bg-primary)]">
      <header className="flex items-center justify-between px-4 py-2 border-b border-[var(--border-color)] bg-[var(--bg-surface)]">
        <div className="flex items-center gap-3">
          <a
            href="#/"
            className="p-1.5 rounded-md border border-[var(--border-color)] hover:bg-[var(--bg-surface-alt)] transition-colors"
            title="Back to Dashboard"
          >
            <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 text-[var(--text-secondary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
            </svg>
          </a>
          <h1 className="text-sm font-semibold text-[var(--text-primary)]">UC Terminal</h1>
          <span className={`text-xs ${connColor[connState]}`}>
            {connState === "connecting" ? "Connecting..." : connState}
          </span>
        </div>
        {(connState === "disconnected" || connState === "error") && (
          <button
            onClick={connect}
            className="px-3 py-1 text-xs rounded-md bg-blue-600 text-white hover:bg-blue-500 transition-colors"
          >
            Reconnect
          </button>
        )}
      </header>
      <div ref={termRef} className="flex-1 px-1 py-1" />
    </div>
  );
}
