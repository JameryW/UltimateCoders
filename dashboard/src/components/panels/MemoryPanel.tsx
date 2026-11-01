import { useCallback, useState } from "react";
import { createClient } from "@connectrpc/connect";
import { create } from "@bufbuild/protobuf";
import {
  EngineService,
  ReadMemoryRequestSchema,
  SearchMemoryRequestSchema,
  WriteMemoryRequestSchema,
  type MemoryEntryProto,
  type MemorySearchResultProto,
} from "@/grpc/engine_pb";
import { getSharedTransport, unaryWithTimeout, type GrpcConnectionState } from "@/hooks/useGrpcWeb";

type MemoryScope = "project" | "task" | "global";

const SCOPES: Array<{ id: MemoryScope; label: string }> = [
  { id: "project", label: "项目" },
  { id: "task", label: "任务" },
  { id: "global", label: "全局" },
];

const CONTENT_TYPES = ["text", "structured", "code", "diff", "reference"];

function formatTime(value: bigint | undefined): string {
  if (!value) return "—";
  const ms = Number(value) > 1e12 ? Number(value) : Number(value) * 1000;
  return new Date(ms).toLocaleString();
}

function scopeIds(scope: MemoryScope, scopeId: string): { taskId: string; projectId: string } {
  return {
    taskId: scope === "task" ? scopeId.trim() : "",
    projectId: scope === "project" ? scopeId.trim() : "",
  };
}

/**
 * Memory surface backed by the gateway's EngineService ReadMemory / WriteMemory /
 * SearchMemory RPCs (the same contract the Python worker uses). Supports the
 * project / task / global key scopes and the five memory content types.
 */
export function MemoryPanel({
  grpcState,
  stale = false,
  variant = "terminal",
}: {
  grpcState: GrpcConnectionState;
  stale?: boolean;
  variant?: "default" | "terminal";
}) {
  const [scope, setScope] = useState<MemoryScope>("project");
  const [scopeId, setScopeId] = useState("");
  const [keyName, setKeyName] = useState("convention");
  const [contentType, setContentType] = useState("text");
  const [content, setContent] = useState("");
  const [importance, setImportance] = useState("0.8");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState<"read" | "write" | "search" | null>(null);
  const [entry, setEntry] = useState<MemoryEntryProto | null>(null);
  const [results, setResults] = useState<MemorySearchResultProto[]>([]);
  const [message, setMessage] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);

  const terminal = variant === "terminal";
  const disconnected = grpcState !== "connected" || stale;

  const run = useCallback(
    async (kind: "read" | "write" | "search") => {
      if (disconnected) {
        setMessage({ tone: "warn", text: "gRPC 未连接，无法访问记忆服务" });
        return;
      }
      if (kind !== "search" && !keyName.trim()) {
        setMessage({ tone: "warn", text: "请填写记忆 Key" });
        return;
      }
      if (scope !== "global" && !scopeId.trim()) {
        setMessage({ tone: "warn", text: scope === "task" ? "请填写 task id" : "请填写 project id" });
        return;
      }
      setBusy(kind);
      setMessage(null);
      try {
        const client = createClient(EngineService, getSharedTransport());
        const ids = scopeIds(scope, scopeId);
        if (kind === "write") {
          if (!content.trim()) {
            setMessage({ tone: "warn", text: "请填写记忆内容" });
            return;
          }
          const req = create(WriteMemoryRequestSchema, {
            keyScope: scope,
            ...ids,
            key: keyName.trim(),
            contentType,
            content,
            sourceAgent: "dashboard",
            importance: Number.isFinite(Number(importance)) ? Math.min(Math.max(Number(importance), 0), 1) : 0.5,
            tags: tags.split(/[,，\s]+/).filter(Boolean),
          });
          const resp = await unaryWithTimeout((signal) => client.writeMemory(req, { signal }), "writeMemory");
          setEntry(resp.entry ?? null);
          setResults([]);
          setMessage({ tone: "ok", text: `已写入 ${scope}/${keyName.trim()}` });
          return;
        }
        if (kind === "read") {
          const req = create(ReadMemoryRequestSchema, {
            keyScope: scope,
            ...ids,
            key: keyName.trim(),
            includeSemantic: false,
          });
          const resp = await unaryWithTimeout((signal) => client.readMemory(req, { signal }), "readMemory");
          setEntry(resp.entry ?? null);
          setResults([]);
          setMessage(
            resp.entry
              ? { tone: "ok", text: `读取 ${scope}/${keyName.trim()}` }
              : { tone: "warn", text: `未找到 ${scope}/${keyName.trim()}` },
          );
          return;
        }
        const query = keyName.trim() || content.trim();
        if (!query) {
          setMessage({ tone: "warn", text: "请填写搜索关键词" });
          return;
        }
        const req = create(SearchMemoryRequestSchema, {
          query,
          scopeType: scope === "task" ? "all" : scope,
          projectId: ids.projectId,
          maxResults: 8,
          minScore: 0,
        });
        const resp = await unaryWithTimeout((signal) => client.searchMemory(req, { signal }), "searchMemory");
        setResults(resp.results ?? []);
        setEntry(null);
        setMessage({ tone: "ok", text: `语义搜索「${query}」· ${resp.results?.length ?? 0} 条` });
      } catch (error) {
        setMessage({ tone: "warn", text: `请求失败：${String(error).slice(0, 140)}` });
      } finally {
        setBusy(null);
      }
    },
    [content, contentType, disconnected, importance, keyName, scope, scopeId, tags],
  );

  return (
    <div className={terminal ? "terminal-memory-panel" : "space-y-3"}>
      <div className="terminal-memory-head">
        <strong>分层记忆</strong>
        <small>
          {disconnected ? "gRPC 未连接" : `engine · ${grpcState}`} · Read / Write / SearchMemory
        </small>
      </div>

      <div className="terminal-memory-chips" role="group" aria-label="记忆作用域">
        {SCOPES.map((item) => (
          <button
            key={item.id}
            type="button"
            className={scope === item.id ? "is-active" : undefined}
            aria-pressed={scope === item.id}
            onClick={() => setScope(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="terminal-memory-grid">
        <label>
          <span>{scope === "task" ? "task id" : scope === "project" ? "project id" : "全局作用域"}</span>
          <input
            value={scopeId}
            onChange={(event) => setScopeId(event.target.value)}
            placeholder={scope === "task" ? "任务 UUID" : scope === "project" ? "Fixture" : "无需填写"}
            disabled={scope === "global"}
            aria-label="记忆作用域 ID"
          />
        </label>
        <label>
          <span>Key</span>
          <input
            value={keyName}
            onChange={(event) => setKeyName(event.target.value)}
            placeholder="convention"
            aria-label="记忆 Key"
          />
        </label>
        <label>
          <span>类型</span>
          <select value={contentType} onChange={(event) => setContentType(event.target.value)} aria-label="记忆类型">
            {CONTENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>重要度</span>
          <input
            type="number"
            min={0}
            max={1}
            step={0.1}
            value={importance}
            onChange={(event) => setImportance(event.target.value)}
            aria-label="记忆重要度"
          />
        </label>
      </div>

      <label className="terminal-memory-content">
        <span>内容</span>
        <textarea
          value={content}
          onChange={(event) => setContent(event.target.value)}
          rows={4}
          placeholder="写入的记忆正文；读取/搜索时留空即可"
          aria-label="记忆内容"
        />
      </label>

      <div className="terminal-memory-actions">
        <label>
          <span>标签</span>
          <input
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="verify, decision"
            aria-label="记忆标签"
          />
        </label>
        <button type="button" onClick={() => void run("write")} disabled={busy !== null}>
          {busy === "write" ? "写入中…" : "写入"}
        </button>
        <button type="button" onClick={() => void run("read")} disabled={busy !== null}>
          {busy === "read" ? "读取中…" : "读取"}
        </button>
        <button type="button" onClick={() => void run("search")} disabled={busy !== null}>
          {busy === "search" ? "搜索中…" : "语义搜索"}
        </button>
      </div>

      {message && <div className={`terminal-memory-notice ${message.tone}`}>{message.text}</div>}

      {entry && (
        <article className="terminal-memory-entry" aria-label="记忆条目">
          <header>
            <b>{entry.contentType || "text"}</b>
            <span>重要度 {entry.importance.toFixed(2)}</span>
            <span>{formatTime(entry.updatedAt || entry.createdAt)}</span>
            {entry.tags.length > 0 && <em>{entry.tags.join(" · ")}</em>}
          </header>
          <pre>{entry.content}</pre>
          {entry.sourceAgent && <small>source: {entry.sourceAgent}</small>}
        </article>
      )}

      {results.length > 0 && (
        <ul className="terminal-memory-results" aria-label="记忆搜索结果">
          {results.map((result, index) => (
            <li key={`${result.entry?.id ?? index}-${index}`}>
              <header>
                <b>{result.entry?.contentType || "text"}</b>
                <span>score {result.score.toFixed(3)}</span>
                {result.entry?.tags.length ? <em>{result.entry.tags.join(" · ")}</em> : null}
              </header>
              <p>{result.entry?.content ?? "（空）"}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
