/**
 * useGrpcClient hook - manages gRPC connection state and provides client methods.
 *
 * Features:
 * - Auto-connect on mount
 * - Expose connection state (disconnected/connecting/connected/error)
 * - Reconnect on disconnect
 * - Return client methods (submitTask, getTask, listTasks, pauseTask, resumeTask)
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

  /** Manually reconnect. */
  reconnect: () => void;

  /** The underlying client (null if not connected). */
  client: TaskServiceClient | null;
}

// ── Hook Implementation ─────────────────────────────────────

/**
 * Check if a gRPC error indicates the server is unavailable.
 */
function isUnavailableError(err: any): boolean {
  return typeof err?.code === 'number' && err.code === GRPC_UNAVAILABLE;
}

export function useGrpcClient(): UseGrpcClientReturn {
  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
  const [client, setClient] = useState<TaskServiceClient | null>(null);
  // Track the latest client in a ref so we can close it on unmount
  // even if `client` state has been updated since the effect ran.
  const clientRef = useRef<TaskServiceClient | null>(null);

  // Keep the ref in sync with the state
  useEffect(() => {
    clientRef.current = client;
  }, [client]);

  const connect = useCallback(() => {
    setConnectionState('connecting');

    try {
      const newClient = createTaskServiceClient();
      setClient(newClient);

      // The gRPC channel connects lazily. To verify the server is
      // actually reachable, we attempt a lightweight RPC (listTasks).
      // If this fails, we mark the connection as error immediately.
      newClient.listTasks({}).then(() => {
        setConnectionState('connected');
      }).catch((err: any) => {
        if (isUnavailableError(err)) {
          setConnectionState('error');
        } else {
          // Non-UNAVAILABLE errors (e.g. server has no tasks) still mean
          // the server is reachable — gRPC returned a valid response.
          setConnectionState('connected');
        }
      });
    } catch {
      setConnectionState('error');
      setClient(null);
    }
  }, []);

  // Connect on mount; close client on unmount
  useEffect(() => {
    connect();

    return () => {
      if (clientRef.current) {
        clientRef.current.close();
        clientRef.current = null;
      }
    };
  }, [connect]);

  const submitTask = useCallback(async (request: SubmitTaskRequest): Promise<SubmitTaskResponse | null> => {
    if (!client) return null;
    try {
      return await client.submitTask(request);
    } catch (err: any) {
      if (isUnavailableError(err)) {
        setConnectionState('error');
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
      }
      return null;
    }
  }, [client]);

  const reconnect = useCallback(() => {
    // Close existing client before reconnecting
    if (client) {
      client.close();
    }
    setClient(null);
    connect();
  }, [client, connect]);

  return {
    connectionState,
    submitTask,
    getTask,
    listTasks,
    pauseTask,
    resumeTask,
    reconnect,
    client,
  };
}

export default useGrpcClient;
