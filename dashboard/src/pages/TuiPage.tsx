import { useEffect, useRef, useCallback, useState } from "react";
import type { Terminal } from "@xterm/xterm";
import type { FitAddon } from "@xterm/addon-fit";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import { executeUcCommand } from "@/lib/ucCommands";
import "@xterm/xterm/css/xterm.css";

type ConnState = "connecting" | "connected" | "disconnected" | "error";

/** Max reconnect delay in milliseconds. */
const RECONNECT_MAX_DELAY = 10_000;
/** Base delay for exponential backoff. */
const RECONNECT_BASE_DELAY = 1_000;

export default function TuiPage() {
  const auth = useAuth();
  const { theme } = useTheme();
  // Derive the auth redirect instead of setting state inside an effect. This
  // keeps the hook order stable and avoids a cascading render during auth.
  const redirect = !auth.isChecking && !auth.isAuthenticated;

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
  const [command, setCommand] = useState("");
  const [commandNotice, setCommandNotice] = useState("共享 UC 命令层就绪");
  const [commandBusy, setCommandBusy] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const {
    connectionState: grpcState,
    submitTask,
    healthCheck,
    listTasks,
    pauseTask,
    resumeTask,
    cancelTask,
  } = useGrpcWeb({ enabled: true });

  const runCommand = useCallback(async (rawValue = command) => {
    if (!rawValue.trim()) return;
    setCommandBusy(true);
    try {
      const result = await executeUcCommand(rawValue, selectedTaskId, {
        refresh: async () => {
          await healthCheck();
          const data = await listTasks();
          setSelectedTaskId(data.tasks[0]?.id ?? null);
        },
        submit: async (description) => {
          const result = await submitTask(description, "");
          if (result.success) setSelectedTaskId(result.taskId);
          return result;
        },
        pause: pauseTask,
        resume: resumeTask,
        cancel: cancelTask,
      });
      if (result.submitResult?.taskId) setSelectedTaskId(result.submitResult.taskId);
      if (result.taskActionResult?.taskId) setSelectedTaskId(result.taskActionResult.taskId);
      setCommandNotice(result.message);
    } finally {
      setCommandBusy(false);
      setCommand("");
    }
  }, [cancelTask, command, healthCheck, listTasks, pauseTask, resumeTask, selectedTaskId, submitTask]);

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
      if (wsRef.current !== ws) return;
      reconnectAttemptRef.current = 0;
      setConnState("connected");
    };
    ws.onclose = () => {
      // React StrictMode and a reconnect can leave an older socket closing
      // after a newer one has already taken its place. That stale close must
      // not make the live TUI look disconnected.
      if (wsRef.current !== ws) return;
      setConnState("disconnected");
      wsRef.current = null;
    };
    ws.onerror = () => {
      if (wsRef.current === ws) setConnState("error");
    };

    ws.onmessage = (ev) => {
      if (wsRef.current !== ws) return;
      // Some reverse proxies deliver the first PTY frame before the browser
      // surfaces onopen. A real PTY frame is authoritative proof that the
      // current WebSocket is live, so never leave the status stuck at
      // "Connecting..." once output has arrived.
      setConnState("connected");
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
      <form
        className="flex items-center gap-2 px-3 py-2 border-b border-[var(--border-color)] bg-[var(--bg-surface-alt)]"
        onSubmit={(event) => {
          event.preventDefault();
          void runCommand();
        }}
      >
        <span className="text-xs font-mono text-[var(--terminal-green)]">uc ❯</span>
        <input
          value={command}
          onChange={(event) => setCommand(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void runCommand();
            }
          }}
          placeholder='run "fix flaky heartbeat test"'
          aria-label="tui uc command"
          className="min-w-0 flex-1 bg-transparent text-xs font-mono text-[var(--text-primary)] outline-none"
        />
        <button
          type="submit"
          disabled={!command.trim() || commandBusy}
          className="px-3 py-1 text-xs rounded border border-[var(--border-color)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] disabled:opacity-40"
        >
          执行
        </button>
        <span className="hidden md:block max-w-[42%] truncate text-[10px] text-[var(--text-secondary)]" title={commandNotice}>
          {commandNotice} · gRPC {grpcState}
        </span>
      </form>
      <div ref={termRef} className="flex-1 px-1 py-1" />
    </div>
  );
}
