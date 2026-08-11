export type UcAction =
  | "status"
  | "tasks"
  | "workers"
  | "search"
  | "logs"
  | "pause"
  | "resume"
  | "cancel"
  | "submit"
  | "help"
  | "unknown";

export type UcCommandView = "overview" | "tasks" | "workers" | "search" | "logs";
export type UcNoticeTone = "success" | "info" | "warn" | "error";

export interface UcSubmitResult {
  success: boolean;
  taskId: string;
  status: string;
  subtaskCount: number;
  subtasks: Array<{
    id: string;
    description: string;
    status: string;
    dependsOn: string[];
    assignedWorker?: string;
  }>;
}

export interface UcTaskActionResult {
  success: boolean;
  taskId: string;
  status: string;
  error?: string;
}

export interface ParsedUcCommand {
  raw: string;
  name: string;
  action: UcAction;
  args: string[];
  target?: string;
  description?: string;
}

export interface UcCommandHandlers {
  refresh?: () => Promise<void>;
  submit?: (description: string) => Promise<UcSubmitResult>;
  pause?: (taskId: string) => Promise<UcTaskActionResult>;
  resume?: (taskId: string) => Promise<UcTaskActionResult>;
  cancel?: (taskId: string) => Promise<UcTaskActionResult>;
}

export interface UcCommandResult {
  action: UcAction;
  success: boolean;
  message: string;
  tone: UcNoticeTone;
  view?: UcCommandView;
  submitResult?: UcSubmitResult;
  taskActionResult?: UcTaskActionResult;
}

const ACTIONS = new Set<UcAction>([
  "status",
  "tasks",
  "workers",
  "search",
  "logs",
  "pause",
  "resume",
  "cancel",
  "submit",
  "help",
]);

export const UC_COMMAND_HELP =
  "commands: status/refresh · tasks · workers · search · logs · submit/run · pause · resume · cancel · help · (不执行宿主机 Shell)";

function tokenize(value: string): string[] {
  const tokens: string[] = [];
  const pattern = /"((?:\\.|[^"])*)"|'((?:\\.|[^'])*)'|(\S+)/g;
  for (const match of value.matchAll(pattern)) {
    const token = match[1] ?? match[2] ?? match[3] ?? "";
    tokens.push(token.replace(/\\(["'])/g, "$1"));
  }
  return tokens;
}

function canonicalAction(name: string): UcAction {
  if (name === "refresh") return "status";
  if (name === "events") return "logs";
  if (name === "run" || name === "execute") return "submit";
  return ACTIONS.has(name as UcAction) ? (name as UcAction) : "unknown";
}

export function parseUcCommand(rawValue: string, selectedTaskId?: string | null): ParsedUcCommand | null {
  const raw = rawValue.trim();
  if (!raw) return null;

  const tokens = tokenize(raw);
  const inputName = (tokens.shift() ?? "").replace(/^\//, "").toLowerCase();
  const action = canonicalAction(inputName);
  const args = tokens;
  const target = args[0] ?? selectedTaskId ?? undefined;
  const description = args.join(" ").trim();

  return {
    raw,
    name: inputName,
    action,
    args,
    target,
    description: description || undefined,
  };
}

function shortId(value: string, length = 10): string {
  return value.length > length ? value.slice(0, length) + "…" : value;
}

function failedResult(action: UcAction, message: string, view?: UcCommandView): UcCommandResult {
  return { action, success: false, message, tone: "error", view };
}

export async function executeUcCommand(
  rawValue: string,
  selectedTaskId: string | null | undefined,
  handlers: UcCommandHandlers,
): Promise<UcCommandResult> {
  const parsed = parseUcCommand(rawValue, selectedTaskId);
  if (!parsed) {
    return { action: "unknown", success: false, message: "", tone: "info" };
  }

  const { action, name, target } = parsed;

  try {
    if (action === "status") {
      if (!handlers.refresh) return failedResult(action, "status failed · Gateway refresh is unavailable", "overview");
      await handlers.refresh();
      return {
        action,
        success: true,
        message: name + " · refreshed from Gateway",
        tone: "success",
        view: "overview",
      };
    }

    if (action === "tasks") {
      return { action, success: true, message: "tasks · real TaskService data", tone: "info", view: "tasks" };
    }
    if (action === "workers") {
      return { action, success: true, message: "workers · real WorkerRegistry data", tone: "info", view: "workers" };
    }
    if (action === "search") {
      return { action, success: true, message: "search · EngineService retrieval surface", tone: "info", view: "search" };
    }
    if (action === "logs") {
      return { action, success: true, message: "logs · real DashboardService events", tone: "info", view: "logs" };
    }
    if (action === "help") {
      return { action, success: true, message: UC_COMMAND_HELP, tone: "info" };
    }
    if (action === "unknown") {
      return {
        action,
        success: false,
        message: "不执行宿主机 Shell “" + (name || parsed.raw) + "” · 输入 help 查看支持的 UC 命令",
        tone: "warn",
      };
    }

    if (action === "submit") {
      if (!parsed.description) {
        return {
          action,
          success: false,
          message: name + " 需要描述；例如 " + name + " \"fix flaky heartbeat test\"",
          tone: "warn",
          view: "tasks",
        };
      }
      if (!handlers.submit) return failedResult(action, name + " failed · TaskService is unavailable", "tasks");
      const result = await handlers.submit(parsed.description);
      if (!result.success) {
        return {
          action,
          success: false,
          message: name + " failed · " + (result.status || "server rejected"),
          tone: "error",
          submitResult: result,
        };
      }
      return {
        action,
        success: true,
        message: name + " · " + shortId(result.taskId) + " 已进入真实 DAG",
        tone: "success",
        view: "tasks",
        submitResult: result,
      };
    }

    if (!target) {
      return { action, success: false, message: name + " 需要先选择一个任务", tone: "warn" };
    }
    const handler = handlers[action];
    if (!handler) return failedResult(action, name + " failed · TaskService is unavailable");
    const result = await handler(target);
    if (!result.success) {
      return {
        action,
        success: false,
        message: name + " failed · " + (result.error ?? result.status ?? "server rejected"),
        tone: "error",
        taskActionResult: result,
      };
    }
    return {
      action,
      success: true,
      message: name + " · " + shortId(target) + " · " + (result.status || "已完成"),
      tone: "success",
      taskActionResult: result,
    };
  } catch (error) {
    return failedResult(action, (name || action) + " failed · " + String(error));
  }
}
