import { useEffect, useRef, useCallback, useState } from "react";
import type { Terminal } from "@xterm/xterm";
import type { FitAddon } from "@xterm/addon-fit";
import { useAuth } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import { useGrpcWeb } from "@/hooks/useGrpcWeb";
import { executeUcCommand, type UcNoticeTone } from "@/lib/ucCommands";
import "@xterm/xterm/css/xterm.css";

type ConnState = "connecting" | "connected" | "disconnected" | "error" | "occupied";

/** Max reconnect delay in milliseconds. */
const RECONNECT_MAX_DELAY = 10_000;
/** Base delay for exponential backoff. */
const RECONNECT_BASE_DELAY = 1_000;
const QUICK_COMMANDS = ["status", "tasks", "workers", "search", "logs", "help"] as const;

export default function TuiPage() {
  const auth = useAuth();
  const { theme } = useTheme();

  // Auth gate
  if (auth.isChecking) {
    return (
      <div className="flex items-center justify-center h-screen text-[var(--text-secondary)]">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-400" />
      </div>
    );
  }
  if (!auth.isAuthenticated) return <TuiLogin auth={auth} />;

  return <TuiTerminal auth={auth} theme={theme} />;
}

function TuiLogin({ auth }: { auth: ReturnType<typeof useAuth> }) {
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password.trim() || submitting) return;
    setSubmitting(true);
    await auth.login(password);
    setSubmitting(false);
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-[var(--bg-primary)] p-4 text-[var(--text-primary)]">
      <section className="w-full max-w-sm rounded-lg border border-[var(--border-color)] bg-[var(--bg-surface)] p-6 shadow-xl">
        <div className="mb-5 flex items-center gap-3">
          <span className="font-mono text-sm font-semibold text-green-400">UC</span>
          <div>
            <h1 className="text-sm font-semibold">UC Terminal</h1>
            <p className="text-xs text-[var(--text-secondary)]">OMP access required</p>
          </div>
        </div>
        {auth.connectionError ? (
          <p className="mb-3 rounded border border-yellow-500/40 bg-yellow-500/10 p-2 text-xs text-yellow-300">
            Gateway unavailable. Start the local Gateway and retry.
          </p>
        ) : (
          <p className="mb-3 text-xs text-[var(--text-secondary)]">
            Enter the configured access token to open the OMP terminal.
          </p>
        )}
        <form className="grid gap-3" onSubmit={onSubmit}>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Access token"
            autoFocus
            className="w-full rounded border border-[var(--border-color)] bg-[var(--bg-primary)] px-3 py-2 text-sm outline-none focus:border-blue-400"
          />
          <button
            type="submit"
            disabled={!password.trim() || submitting}
            className="rounded bg-blue-600 px-3 py-2 text-sm text-white transition-colors hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {submitting ? "Connecting..." : "Open terminal"}
          </button>
        </form>
        {auth.loginError && <p className="mt-3 text-xs text-red-400">{auth.loginError}</p>}
      </section>
    </main>
  );
}

function TuiTerminal({ auth, theme }: { auth: { token: string | null }; theme: string }) {
  const termRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wsStatusRef = useRef<"connected" | "disconnected" | null>(null);
  const disposedRef = useRef(false);
  const sessionBusyRef = useRef(false);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const [connState, setConnState] = useState<ConnState>("connecting");
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [command, setCommand] = useState("");
  const [commandNotice, setCommandNotice] = useState("共享 UC 命令层就绪");
  const [commandTone, setCommandTone] = useState<UcNoticeTone>("info");
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

  const writeTerminalLine = useCallback((line: string, color = "90") => {
    xtermRef.current?.writeln(`\x1b[${color}m${line}\x1b[0m`);
  }, []);

  const runCommand = useCallback(async (rawValue = command) => {
    if (!rawValue.trim()) return;
    const input = rawValue.trim();
    writeTerminalLine(`uc ❯ ${input}`, "32");
    setCommandBusy(true);
    setCommandTone("info");
    setCommandNotice(`执行中 · ${input}`);
    writeTerminalLine(`[UC] executing ${input}`, "33");
    try {
      const result = await executeUcCommand(input, selectedTaskId, {
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
      setCommandTone(result.tone);
      writeTerminalLine(result.message, "37");
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      setCommandNotice(message);
      setCommandTone("error");
      writeTerminalLine(`error: ${message}`, "31");
    } finally {
      setCommandBusy(false);
      setCommand("");
    }
  }, [cancelTask, command, healthCheck, listTasks, pauseTask, resumeTask, selectedTaskId, submitTask, writeTerminalLine]);

  const connect = useCallback((takeover = false) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    if (takeover) sessionBusyRef.current = false;

    // Clear any pending reconnect timer
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    // Build ws URL with auth token
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const query = new URLSearchParams();
    if (auth.token) query.set("token", auth.token);
    if (takeover) query.set("takeover", "1");
    const params = query.toString() ? `?${query.toString()}` : "";
    const url = `${proto}//${location.host}/ws/tui${params}`;

    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    setConnState("connecting");

    ws.onopen = () => {
      if (wsRef.current !== ws) return;
      reconnectAttemptRef.current = 0;
      setConnState("connected");
      if (wsStatusRef.current !== "connected") {
        writeTerminalLine("[OMP] WebSocket connected — waiting for terminal output", "32");
      }
      wsStatusRef.current = "connected";
    };
    ws.onclose = (event) => {
      // StrictMode intentionally mounts the component twice in development.
      // The first instance closes its probe socket during cleanup; that close
      // must not schedule a reconnect after the real instance is mounted.
      if (disposedRef.current) return;
      if (event.code === 4003) {
        sessionBusyRef.current = true;
        setConnState("occupied");
        wsRef.current = null;
        writeTerminalLine("[OMP] Another TUI tab is already attached — use Take over to claim this session", "33");
        return;
      }
      if (event.code === 4004) {
        sessionBusyRef.current = true;
        setConnState("occupied");
        wsRef.current = null;
        writeTerminalLine("[OMP] This session was taken over by another tab", "33");
        return;
      }
      // React StrictMode and a reconnect can leave an older socket closing
      // after a newer one has already taken its place. That stale close must
      // not make the live TUI look disconnected.
      if (wsRef.current !== ws) return;
      setConnState("disconnected");
      wsRef.current = null;
      if (wsStatusRef.current !== "disconnected") {
        writeTerminalLine("[OMP] WebSocket disconnected — reconnecting", "33");
      }
      wsStatusRef.current = "disconnected";
    };
    ws.onerror = () => {
      // Browsers can report a transient proxy error before the socket emits
      // its authoritative close event.  Treating error as a reconnect signal
      // here creates overlapping sockets; the FastAPI bridge closes the old
      // client when the next one arrives, producing an endless connect loop.
      // Let onclose own reconnect state instead.
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
  }, [auth.token, writeTerminalLine]);

  // Init xterm + connect on mount
  useEffect(() => {
    const container = termRef.current;
    if (!container) return;

    let term: Terminal;
    let fitAddon: FitAddon;
    let disposed = false;
    disposedRef.current = false;

    (async () => {
      const { Terminal } = await import("@xterm/xterm");
      const { FitAddon } = await import("@xterm/addon-fit");
      if (disposed) return;

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
      term.writeln("\x1b[1;32mUltimateCoders TUI\x1b[0m");
      term.writeln("\x1b[90mShared UC command layer · gRPC task control · OMP WebSocket bridge\x1b[0m");
      term.writeln("\x1b[90mUse the command bar above to inspect or control tasks.\x1b[0m");
      term.writeln("");
      connect();
    })();

    const onResize = () => fitAddon?.fit();
    window.addEventListener("resize", onResize);

    return () => {
      disposed = true;
      disposedRef.current = true;
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
    if (sessionBusyRef.current) return;
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
    occupied: "text-amber-300",
  };

  const connLabel: Record<ConnState, string> = {
    connecting: "Connecting...",
    connected: "connected",
    disconnected: "disconnected",
    error: "error",
    occupied: "session attached elsewhere",
  };

  return (
    <div className="flex flex-col h-screen bg-[var(--bg-primary)]">
      <header className="flex items-center justify-between px-4 py-2 border-b border-[var(--border-color)] bg-[var(--bg-surface)]">
        <div className="flex items-center gap-3">
          <span className="font-mono text-xs font-semibold text-green-400">UC</span>
          <h1 className="text-sm font-semibold text-[var(--text-primary)]">UC Terminal</h1>
          <span className={`text-xs ${connColor[connState]}`}>
            {connLabel[connState]}
          </span>
        </div>
        <div className="flex items-center gap-3">
          <a className="tui-home-link" href="#/" aria-label="Back to product overview">Overview</a>
          <a className="tui-home-link" href="#/dashboard" aria-label="Open operations dashboard">Dashboard</a>
          {(connState === "disconnected" || connState === "error" || connState === "occupied") && (
            <button
              onClick={() => connect(connState === "occupied")}
              className={`px-3 py-1 text-xs rounded-md text-white transition-colors ${connState === "occupied" ? "bg-amber-600 hover:bg-amber-500" : "bg-blue-600 hover:bg-blue-500"}`}
            >
              {connState === "occupied" ? "Take over" : "Reconnect"}
            </button>
          )}
        </div>
      </header>
      <div className="tui-command-shell">
        <form
          className="flex items-center gap-2 px-3 py-2 bg-[var(--bg-surface-alt)]"
          aria-busy={commandBusy}
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
            {commandBusy ? "执行中" : "执行"}
          </button>
        </form>
        <div className={`tui-command-feedback tui-command-feedback-${commandTone}`} role="status" aria-live="polite">
          <span className="tui-feedback-indicator" aria-hidden="true" />
          <span className="tui-command-feedback-message" title={commandNotice}>
            {commandNotice}
          </span>
          <span className="tui-command-feedback-connection">gRPC {grpcState}</span>
        </div>
        <div className="tui-command-quick-actions" aria-label="Quick UC commands">
          <span>quick</span>
          {QUICK_COMMANDS.map((quickCommand) => (
            <button
              key={quickCommand}
              type="button"
              disabled={commandBusy}
              onClick={() => void runCommand(quickCommand)}
            >
              {quickCommand}
            </button>
          ))}
        </div>
      </div>
      <div ref={termRef} className="flex-1 px-1 py-1" />
    </div>
  );
}
