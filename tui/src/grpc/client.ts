/**
 * gRPC client for UltimateCoders TaskService.
 *
 * Uses @grpc/grpc-js + @grpc/proto-loader for dynamic proto loading.
 * Provides typed client methods for all TaskService RPCs.
 *
 * The proto file is loaded from the uc-grpc crate's proto directory
 * at runtime, avoiding a code generation step.
 */

import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import path from 'node:path';
import fs from 'node:fs';

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
  WatchTaskRequest,
  TaskEventProto,
} from './types.js';

// ── Proto Loading ───────────────────────────────────────────

/** Default gRPC server address. */
const DEFAULT_SERVER_ADDR = 'localhost:50051';

/**
 * Resolve the proto file path.
 *
 * Search order:
 * 1. GRPC_PROTO_PATH env var (absolute path)
 * 2. Relative to this package: ../../crates/uc-grpc/proto/engine.proto
 */
function resolveProtoPath(): string {
  const envPath = process.env.GRPC_PROTO_PATH;
  if (envPath && fs.existsSync(envPath)) {
    return envPath;
  }

  // Relative from tui/ package root
  const relativePath = path.resolve(
    import.meta.dirname ?? __dirname,
    '../../../crates/uc-grpc/proto/engine.proto',
  );
  if (fs.existsSync(relativePath)) {
    return relativePath;
  }

  throw new Error(
    `Cannot find engine.proto. Set GRPC_PROTO_PATH env var or ensure proto file exists at ${relativePath}`,
  );
}

/**
 * Load and cache the proto package definition.
 * Called once; the result is reused across client instances.
 */
let cachedPackageDef: any = null;

function loadPackageDefinition(protoPath: string): any {
  if (cachedPackageDef) {
    return cachedPackageDef;
  }

  const packageDef = protoLoader.loadSync(protoPath, {
    keepCase: false, // Convert snake_case to camelCase
    longs: String,
    enums: String,
    defaults: true,
    oneofs: true,
  });

  cachedPackageDef = grpc.loadPackageDefinition(packageDef);
  return cachedPackageDef;
}

// ── TaskService Client ──────────────────────────────────────

/**
 * Typed client for the TaskService gRPC service.
 *
 * Uses grpc.loadPackageDefinition to create a properly typed gRPC client.
 * The dynamic methods are accessed via `as any` casts since TypeScript
 * cannot know about them at compile time.
 */
export class TaskServiceClient {
  private client: grpc.Client;

  constructor(serverAddr: string, protoPath: string) {
    const pkg = loadPackageDefinition(protoPath);
    const TaskService = pkg.ultimate_coders.TaskService;

    this.client = new TaskService(
      serverAddr,
      grpc.credentials.createInsecure(),
      {
        'grpc.max_receive_message_length': 1024 * 1024, // 1MB
        'grpc.max_send_message_length': 1024 * 1024,
      },
    );
  }

  /** Submit a new task. */
  submitTask(request: SubmitTaskRequest): Promise<SubmitTaskResponse> {
    return new Promise((resolve, reject) => {
      (this.client as any).SubmitTask(
        {
          description: request.description,
          projectId: request.projectId,
        },
        (err: grpc.ServiceError | null, response: SubmitTaskResponse) => {
          if (err) {
            reject(err);
          } else {
            resolve(response);
          }
        },
      );
    });
  }

  /** Get a task by ID. */
  getTask(request: GetTaskRequest): Promise<GetTaskResponse> {
    return new Promise((resolve, reject) => {
      (this.client as any).GetTask(
        {taskId: request.taskId},
        (err: grpc.ServiceError | null, response: GetTaskResponse) => {
          if (err) {
            reject(err);
          } else {
            resolve(response);
          }
        },
      );
    });
  }

  /** List all tasks. */
  listTasks(request: ListTasksRequest): Promise<ListTasksResponse> {
    return new Promise((resolve, reject) => {
      (this.client as any).ListTasks(
        {},
        (err: grpc.ServiceError | null, response: ListTasksResponse) => {
          if (err) {
            reject(err);
          } else {
            resolve(response);
          }
        },
      );
    });
  }

  /** Watch task events (server-streaming). Returns an event emitter. */
  watchTask(request: WatchTaskRequest): grpc.ClientReadableStream<any> {
    return (this.client as any).WatchTask({taskId: request.taskId});
  }

  /** Pause a task. */
  pauseTask(request: PauseTaskRequest): Promise<PauseTaskResponse> {
    return new Promise((resolve, reject) => {
      (this.client as any).PauseTask(
        {taskId: request.taskId},
        (err: grpc.ServiceError | null, response: PauseTaskResponse) => {
          if (err) {
            reject(err);
          } else {
            resolve(response);
          }
        },
      );
    });
  }

  /** Resume a task. */
  resumeTask(request: ResumeTaskRequest): Promise<ResumeTaskResponse> {
    return new Promise((resolve, reject) => {
      (this.client as any).ResumeTask(
        {taskId: request.taskId},
        (err: grpc.ServiceError | null, response: ResumeTaskResponse) => {
          if (err) {
            reject(err);
          } else {
            resolve(response);
          }
        },
      );
    });
  }

  /** Close the gRPC connection. */
  close(): void {
    this.client.close();
  }
}

// ── Client Factory ──────────────────────────────────────────

/**
 * Create a TaskServiceClient with auto-resolved proto path.
 *
 * @param serverAddr - gRPC server address (default: localhost:50051)
 * @returns TaskServiceClient instance
 */
export function createTaskServiceClient(
  serverAddr: string = process.env.GRPC_SERVER_ADDR ?? DEFAULT_SERVER_ADDR,
): TaskServiceClient {
  const protoPath = resolveProtoPath();
  return new TaskServiceClient(serverAddr, protoPath);
}
