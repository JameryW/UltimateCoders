/**
 * useGrpcClient hook - manages gRPC connection state and provides client methods.
 *
 * Features:
 * - Auto-connect on mount
 * - Expose connection state (disconnected/connecting/connected/error)
 * - Exponential backoff reconnection (max 5 retries, intervals 1/2/4/8/16s)
 * - Reconnect on disconnect or manual Ctrl+R
 * - Return client methods (submitTask, getTask, listTasks, pauseTask, resumeTask)
 * - Expose connection diagnostics (lastError, retryCount, nextRetryAt)
 * - Configurable server address via GRPC_SERVER_ADDR env var
 * - Clean up client on unmount
 */

import {useState, useEffect, useCallback, useRef} from 'react';
import {
  createTaskServiceClient,
  TaskServiceClient,
} from '../grpc/client.js';
import type {
  ConnectionState,
  SubmitTaskRequest,
  SubmitTaskResponse,
  GetTaskRequest,
  GetTaskResponse,
  ListTasksRequest,
  ListTasksResponse,
  PauseTaskRequest,
  PauseTaskResponse,
  ResumeTaskRequest,
  ResumeTaskResponse,
} from '../grpc/types.js';

// ── Constants ───────────────────────────────────────────────

/** gRPC status code for UNAVAILABLE (server not reachable). */
const GRPC_UNAVAILABLE = 14;

/** Timeout for gRPC connectivity probe (ms). */
const CONNECT_TIMEOUT = 3000;

/** Maximum number of automatic reconnection attempts. */
const MAX_RETRY_COUNT = 5;

/** Base retry intervals (ms) for exponential backoff: 1s, 2s, 4s, 8s, 16s. */
const RETRY_INTERVALS = [1000, 2000, 4000, 8000, 16000];

/** Server address from env. */
const SERVER_ADDR = process.env.GRPC_SERVER_ADDR ?? 'localhost:50051';

// ── Hook Return Type ────────────────────────────────────────

export interface UseGrpcClientReturn {
  /** Current connection state. */
  connectionState: ConnectionState;

  /** Submit a new task. Returns the response or null on error. */
  submitTask: (request: SubmitTaskRequest) => Promise<SubmitTaskResponse | null>;

  /** Get a task by ID. */
  getTask: (request: GetTaskRequest) => Promise<GetTaskResponse | null>;

  /** List all tasks. */
  listTasks: (request: ListTasksRequest) => Promise<ListTasksResponse | null>;

  /** Pause a task. */
  pauseTask: (request: PauseTaskRequest) => Promise<PauseTaskResponse | null>;

  /** Resume a task. */
  resumeTask: (request: ResumeTaskRequest) => Promise<ResumeTaskResponse | null>;

  /** Manually reconnect (interrupts backoff, immediately retries). */
  reconnect: () => void;

  /** The underlying client (null if not connected). */
  client: TaskServiceClient | null;

  /** Last error message from connection/API failure. */
  lastError: string | null;

  /** Current retry attempt count (0 = not retrying). */
  retryCount: number;

  /** Timestamp of next scheduled retry (null if not retrying). */
  nextRetryAt: number | null;

  /** Server address in use. */
  serverAddr: string;
}

// ── Hook Implementation ─────────────────────────────────────

/**
 * Check if a gRPC error indicates the server is unavailable.
 */
export function isUnavailableError(err: any): boolean {
  return typeof err?.code === 'number' && err.code === GRPC_UNAVAILABLE;
}

/**
 * Extract a short error message from a gRPC error.
 */
export function getErrorMessage(err: any): string {
  if (typeof err?.message === 'string') {
    return err.message.slice(0, 100);
  }
  if (typeof err?.code === 'number') {
    return `gRPC error code ${err.code}`;
  }
  return String(err).slice(0, 100);
}

export function useGrpcClient(): UseGrpcClientReturn {
  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
  const [client, setClient] = useState<TaskServiceClient | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const [nextRetryAt, setNextRetryAt] = useState<number | null>(null);

  // Track the latest client in a ref so we can close it on unmount
  // even if `client` state has been updated since the effect ran.
  const clientRef = useRef<TaskServiceClient | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Keep the ref in sync with the state
  useEffect(() => {
    clientRef.current = client;
  }, [client]);

  // ── Schedule next retry with exponential backoff ──────
  const scheduleRetry = useCallback((currentRetryCount: number) => {
    if (currentRetryCount >= MAX_RETRY_COUNT) {
      setNextRetryAt(null);
      return; // Max retries reached
    }

    const interval = RETRY_INTERVALS[currentRetryCount] ?? RETRY_INTERVALS[RETRY_INTERVALS.length - 1];
    const retryAt = Date.now() + interval;
    setNextRetryAt(retryAt);

    retryTimerRef.current = setTimeout(() => {
      setRetryCount(currentRetryCount + 1);
      setNextRetryAt(null);
      // connect() will be triggered by the retryCount change effect
    }, interval);
  }, []);

  // ── Connect to server ─────────────────────────────────
  const connect = useCallback(() => {
    // Clear any pending retry timer
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }

    setConnectionState('connecting');

    try {
      const newClient = createTaskServiceClient();
      setClient(newClient);

      // The gRPC channel connects lazily. Verify server reachability
      // with a listTasks probe + timeout. If it fails or times out,
      // mark as error so the UI shows offline/demo mode immediately.
      let probeTimedOut = false;
      const probeTimeout = setTimeout(() => {
        probeTimedOut = true;
        setConnectionState('error');
        setLastError(`Connection timeout to ${SERVER_ADDR}`);
        // Schedule retry with exponential backoff
        setRetryCount((prev) => {
          scheduleRetry(prev);
          return prev;
        });
      }, CONNECT_TIMEOUT);

      newClient.listTasks({}).then(() => {
        if (probeTimedOut) return; // probe already timed out — ignore late success
        clearTimeout(probeTimeout);
        setConnectionState('connected');
        setLastError(null);
        setRetryCount(0);
        setNextRetryAt(null);
      }).catch((err: any) => {
        if (probeTimedOut) return; // probe already timed out — ignore late error
        clearTimeout(probeTimeout);
        if (isUnavailableError(err)) {
          setConnectionState('error');
          setLastError(getErrorMessage(err));
          // Schedule retry with exponential backoff
          setRetryCount((prev) => {
            scheduleRetry(prev);
            return prev;
          });
        } else {
          // Non-UNAVAILABLE errors (e.g. permission) still mean
          // the server is reachable — treat as connected.
          setConnectionState('connected');
          setLastError(null);
          setRetryCount(0);
          setNextRetryAt(null);
        }
      });
    } catch (err: any) {
      setConnectionState('error');
      setLastError(getErrorMessage(err));
      setClient(null);
      // Schedule retry with exponential backoff
      setRetryCount((prev) => {
        scheduleRetry(prev);
        return prev;
      });
    }
  }, [scheduleRetry]);

  // Connect on mount; close client on unmount
  useEffect(() => {
    connect();

    return () => {
      if (clientRef.current) {
        clientRef.current.close();
        clientRef.current = null;
      }
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    };
  }, [connect]);

  // ── Reconnect when retryCount increases (from backoff timer) ──
  const prevRetryCountRef = useRef(0);
  useEffect(() => {
    if (retryCount > prevRetryCountRef.current && retryCount > 0) {
      connect();
    }
    prevRetryCountRef.current = retryCount;
  }, [retryCount, connect]);

  // ── API methods ───────────────────────────────────────

  const submitTask = useCallback(async (request: SubmitTaskRequest): Promise<SubmitTaskResponse | null> => {
    if (!client) return null;
    try {
      return await client.submitTask(request);
    } catch (err: any) {
      if (isUnavailableError(err)) {
        setConnectionState('error');
        setLastError(getErrorMessage(err));
      }
      return null;
    }
  }, [client]);

  const getTask = useCallback(async (request: GetTaskRequest): Promise<GetTaskResponse | null> => {
    if (!client) return null;
    try {
      return await client.getTask(request);
    } catch (err: any) {
      if (isUnavailableError(err)) {
        setConnectionState('error');
        setLastError(getErrorMessage(err));
      }
      return null;
    }
  }, [client]);

  const listTasks = useCallback(async (request: ListTasksRequest): Promise<ListTasksResponse | null> => {
    if (!client) return null;
    try {
      return await client.listTasks(request);
    } catch (err: any) {
      if (isUnavailableError(err)) {
        setConnectionState('error');
        setLastError(getErrorMessage(err));
      }
      return null;
    }
  }, [client]);

  const pauseTask = useCallback(async (request: PauseTaskRequest): Promise<PauseTaskResponse | null> => {
    if (!client) return null;
    try {
      return await client.pauseTask(request);
    } catch (err: any) {
      if (isUnavailableError(err)) {
        setConnectionState('error');
        setLastError(getErrorMessage(err));
      }
      return null;
    }
  }, [client]);

  const resumeTask = useCallback(async (request: ResumeTaskRequest): Promise<ResumeTaskResponse | null> => {
    if (!client) return null;
    try {
      return await client.resumeTask(request);
    } catch (err: any) {
      if (isUnavailableError(err)) {
        setConnectionState('error');
        setLastError(getErrorMessage(err));
      }
      return null;
    }
  }, [client]);

  // ── Manual reconnect: interrupts backoff, immediately retries ──
  // ponytail: read client from ref so reconnect has stable identity (no client dep)
  const reconnect = useCallback(() => {
    if (clientRef.current) {
      clientRef.current.close();
      clientRef.current = null;
    }
    setClient(null);
    setRetryCount(0);
    setNextRetryAt(null);
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    connect();
  }, [connect]);

  return {
    connectionState,
    submitTask,
    getTask,
    listTasks,
    pauseTask,
    resumeTask,
    reconnect,
    client,
    lastError,
    retryCount,
    nextRetryAt,
    serverAddr: SERVER_ADDR,
  };
}

export default useGrpcClient;
