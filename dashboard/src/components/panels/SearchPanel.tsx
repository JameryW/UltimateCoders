import { useState, useCallback, useRef } from "react";
import { createClient } from "@connectrpc/connect";
import { EngineService } from "@/grpc/engine_pb";
import type { SearchResultItem as GrpcSearchResultItem } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { SearchRequestSchema } from "@/grpc/engine_pb";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { getSharedTransport, unaryWithTimeout, type GrpcConnectionState } from "@/hooks/useGrpcWeb";
import type { FileBrowserNavigateEvent } from "@/components/panels/FileBrowser";

// ponytail: uses shared transport from useGrpcWeb — single HTTP/2 connection

type SearchMode = "text" | "semantic" | "ast";

interface SearchResult {
  repoId: string;
  filePath: string;
  startLine: number;
  endLine: number;
  contentSnippet: string;
  matchType: string;
  score: number;
  symbolName?: string;
  symbolKind?: string;
  parentSymbol?: string;
}

/** Neon tone for match-type chips / row rails under command-center. */
// eslint-disable-next-line react-refresh/only-export-components -- unit-tested helper
export function matchTypeTone(type: string): "text" | "semantic" | "ast" | "default" {
  switch (type) {
    case "text":
      return "text";
    case "semantic":
      return "semantic";
    case "ast":
      return "ast";
    default:
      return "default";
  }
}

function formatScore(score: number): string {
  if (!Number.isFinite(score)) return "";
  // Keep short: rank scores are often 0–1 floats.
  if (score > 0 && score < 1) return score.toFixed(3);
  return String(Math.round(score * 1000) / 1000);
}

/** Presentational result list — exported so SSR tests can lock CC row chrome without gRPC. */
export function SearchResultList({
  results,
  terminal,
  onNavigateFile,
}: {
  results: SearchResult[];
  terminal: boolean;
  onNavigateFile?: (nav: FileBrowserNavigateEvent) => void;
}) {
  if (results.length === 0) return null;
  return (
    <ul
      className={cn(terminal ? "terminal-search-results" : "space-y-1.5 max-h-96 overflow-y-auto")}
      aria-label="Search results"
      data-testid={terminal ? "terminal-search-results" : undefined}
    >
      {results.map((r, i) => {
        const tone = matchTypeTone(r.matchType);
        const scoreLabel = formatScore(r.score);
        if (terminal) {
          return (
            <li
              key={`${r.filePath}-${r.startLine}-${i}`}
              className={cn("terminal-search-row", `match-${tone}`)}
              data-testid="terminal-search-row"
              data-match-type={r.matchType || "unknown"}
            >
              <div className="terminal-search-row-topline">
                <button
                  type="button"
                  onClick={() => onNavigateFile?.({ repoId: r.repoId, path: r.filePath, line: r.startLine })}
                  className="terminal-search-jump"
                  data-testid="terminal-search-jump"
                  title={`${r.repoId}:${r.filePath}`}
                >
                  {r.filePath}
                  {r.symbolName && <span className="terminal-search-symbol">{r.symbolName}</span>}
                </button>
                <div className="terminal-search-row-meta">
                  <span className="terminal-search-repo">{r.repoId}</span>
                  <span className="terminal-search-lines">
                    L{r.startLine}{r.endLine > r.startLine ? `-${r.endLine}` : ""}
                  </span>
                  {scoreLabel !== "" && (
                    <span className="terminal-search-score" data-testid="terminal-search-score">
                      {scoreLabel}
                    </span>
                  )}
                  {r.symbolKind && (
                    <span className="terminal-search-kind" data-testid="terminal-search-kind">
                      {r.symbolKind}
                    </span>
                  )}
                  {r.parentSymbol && (
                    <span className="terminal-search-parent" data-testid="terminal-search-parent">
                      ∈ {r.parentSymbol}
                    </span>
                  )}
                  <span className={cn("terminal-search-chip", "match-tag", `match-${tone}`)}>
                    {r.matchType || "hit"}
                  </span>
                </div>
              </div>
              {r.contentSnippet && (
                <pre className="terminal-search-snippet">{r.contentSnippet}</pre>
              )}
            </li>
          );
        }
        return (
          <li key={`${r.filePath}-${r.startLine}-${i}`} className="border-l-2 border-l-blue-500 pl-2 py-1">
            <div className="flex items-center justify-between text-sm">
              <button
                type="button"
                onClick={() => onNavigateFile?.({ repoId: r.repoId, path: r.filePath, line: r.startLine })}
                className="text-[var(--text-primary)] font-mono truncate hover:text-blue-400 hover:underline text-left"
              >
                {r.filePath}
                {r.symbolName && <span className="text-blue-400 ml-1 font-sans not-italic">{r.symbolName}</span>}
              </button>
              <div className="flex items-center gap-2 shrink-0">
                <span className="text-xs text-[var(--text-muted)]">{r.repoId}</span>
                <span className="text-xs text-[var(--text-muted)]">L{r.startLine}{r.endLine > r.startLine ? `-${r.endLine}` : ""}</span>
                {scoreLabel !== "" && (
                  <span className="text-xs text-[var(--text-muted)]">{scoreLabel}</span>
                )}
                {r.symbolKind && (
                  <span className="text-xs text-[var(--text-muted)]">{r.symbolKind}</span>
                )}
                <span className={cn("text-xs px-1.5 py-0.5 rounded", matchTypeClass(r.matchType))}>
                  {r.matchType}
                </span>
              </div>
            </div>
            {r.contentSnippet && (
              <pre className="text-xs text-[var(--text-muted)] mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono bg-[var(--bg-primary)]/50 rounded p-1.5 max-h-20 overflow-y-auto">
                {r.contentSnippet}
              </pre>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function SearchPanel({
  grpcState,
  onNavigateFile,
  stale,
  variant = "default",
}: {
  grpcState?: GrpcConnectionState;
  onNavigateFile?: (nav: FileBrowserNavigateEvent) => void;
  stale?: boolean;
  /** Command-center monospaced search shell (no Card; neon match-type rails). */
  variant?: "default" | "terminal";
}) {
  const terminal = variant === "terminal";
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const [maxResults, setMaxResults] = useState(20);
  const [showFilters, setShowFilters] = useState(false);
  const [modes, setModes] = useState<SearchMode[]>([]);
  const [language, setLanguage] = useState("");
  // ponytail: request-id guard — gRPC-Web unary has no AbortController wired
  // through the Connect client here, so track the latest search. If a slow
  // search resolves after a newer one, its results are stale and must NOT
  // overwrite the fresh setResults (else the UI shows query A's results under
  // query B's input after a fast-then-slow double-Enter).
  const latestSearchId = useRef(0);

  const toggleMode = (m: SearchMode) => {
    setModes((prev) => prev.includes(m) ? prev.filter((x) => x !== m) : [...prev, m]);
  };

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    // ponytail: stamp this search; only the latest resolves may setResults.
    const reqId = ++latestSearchId.current;
    setSearching(true);
    setError(null);
    try {
      const transport = getSharedTransport();
      const client = createClient(EngineService, transport);
      const req = create(SearchRequestSchema, {
        query: q,
        maxResults,
        modes: modes.length > 0 ? modes : undefined,
        languages: language.trim() ? [language.trim()] : undefined,
      });
      // ponytail: F74 — 30s timeout (a stalling server left "Searching…" up
      // forever before).
      const resp = await unaryWithTimeout((signal) => client.search(req, { signal }), "search");
      if (reqId !== latestSearchId.current) return; // stale — a newer search won
      setResults(resp.items.map(mapResult));
      setSearched(true);
    } catch (err) {
      if (reqId !== latestSearchId.current) return; // stale error from an abandoned search
      setError(String(err));
      setResults([]);
    } finally {
      if (reqId === latestSearchId.current) setSearching(false);
    }
  }, [query, maxResults, modes, language]);

  const filtersOpen = terminal ? true : showFilters;

  const controls = (
    <>
      <div className={cn(terminal ? "terminal-search-controls" : "flex gap-2 mb-2")}>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleSearch(); }}
          aria-label="Search query"
          className={cn(
            terminal
              ? "terminal-search-query"
              : "flex-1 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none",
          )}
          placeholder="Search indexed repos… (e.g. 'fn search' or 'class EngineApi')"
          data-testid={terminal ? "terminal-search-query" : undefined}
        />
        <button
          type="button"
          onClick={handleSearch}
          disabled={searching || !query.trim()}
          aria-label="Run search"
          className={cn(
            terminal
              ? cn("terminal-search-chip", "is-active", "swarm")
              : "btn-action-info border border-blue-500 rounded-md px-4 py-2 text-sm font-medium cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap",
          )}
          data-testid={terminal ? "terminal-search-chip-run" : undefined}
        >
          {searching ? "Searching…" : "Search"}
        </button>
        {terminal ? (
          <div className="terminal-search-limit" role="group" aria-label="Max results">
            {[10, 20, 50, 100].map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => setMaxResults(n)}
                className={cn("terminal-search-chip", maxResults === n && "is-active dispatch")}
                data-testid="terminal-search-chip-limit"
              >
                {n}
              </button>
            ))}
          </div>
        ) : (
          <select
            value={maxResults}
            onChange={(e) => setMaxResults(Number(e.target.value))}
            aria-label="Max results"
            className="bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-2 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
          >
            {[10, 20, 50, 100].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        )}
        {!terminal && (
          <button
            type="button"
            onClick={() => setShowFilters(!showFilters)}
            className={cn(
              "px-2 py-2 rounded-md border text-sm cursor-pointer transition-colors",
              showFilters ? "border-blue-500 text-blue-400" : "border-[var(--border-color)] text-[var(--text-muted)] hover:text-[var(--text-primary)]",
            )}
            title="Search filters"
          >
            ⚙
          </button>
        )}
      </div>

      {filtersOpen && (
        <div
          className={cn(
            terminal
              ? "terminal-search-filters"
              : "flex flex-wrap items-center gap-2 mb-2 p-2 bg-[var(--bg-primary)] rounded-md border border-[var(--border-color)]",
          )}
        >
          <span className={cn(terminal ? "terminal-search-filter-label" : "text-xs text-[var(--text-muted)]")}>Mode:</span>
          {(["text", "semantic", "ast"] as SearchMode[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => toggleMode(m)}
              className={cn(
                terminal
                  ? cn(
                    "terminal-search-chip",
                    `match-${m}`,
                    modes.includes(m) && "is-active",
                  )
                  : cn(
                    "text-xs px-1.5 py-0.5 rounded cursor-pointer",
                    modes.includes(m) ? matchTypeClass(m) : "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)]",
                  ),
              )}
              data-testid={terminal ? "terminal-search-chip-mode" : undefined}
              data-mode={m}
            >
              {m}
            </button>
          ))}
          <span className={cn(terminal ? "terminal-search-filter-label" : "text-xs text-[var(--text-muted)] ml-2")}>Lang:</span>
          <input
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            placeholder="e.g. rust, python"
            aria-label="Language filter"
            className={cn(
              terminal
                ? "terminal-search-lang"
                : "w-28 bg-[var(--bg-surface-alt)] border border-[var(--border-color)] rounded px-2 py-0.5 text-xs text-[var(--text-primary)] focus:border-blue-500 focus:outline-none",
            )}
            data-testid={terminal ? "terminal-search-lang" : undefined}
          />
        </div>
      )}
    </>
  );

  const body = (
    <>
      {controls}

      {error && (
        <p className={cn(terminal ? "terminal-search-error" : "text-xs text-red-400 mb-2")}>
          Search failed: {error}
        </p>
      )}

      {searched && results.length === 0 && !error && (
        <p className={cn(terminal ? "terminal-empty-line" : "text-sm text-[var(--text-muted)]")} data-testid={terminal ? "terminal-search-empty" : undefined}>
          No results found for &quot;{query}&quot;
        </p>
      )}

      <SearchResultList results={results} terminal={terminal} onNavigateFile={onNavigateFile} />

      {!searched && (
        <p className={cn(terminal ? "terminal-empty-line" : "text-xs text-[var(--text-muted)]")} data-testid={terminal ? "terminal-search-hint" : undefined}>
          Search across indexed repositories via gRPC-Web.
          {grpcState !== "connected" && " gRPC server is currently disconnected — search requires an active connection."}
        </p>
      )}
    </>
  );

  if (terminal) {
    return (
      <section
        className={cn("terminal-search-panel", stale && "is-stale")}
        data-testid="terminal-search-panel"
        data-stale={stale ? "true" : "false"}
        aria-label="Code search"
      >
        <div className="terminal-search-head cc-session-log-head">
          <span>code search</span>
          <b data-testid="terminal-search-count">
            {searched ? `${results.length} hit${results.length === 1 ? "" : "s"}` : "ready"}
          </b>
        </div>
        {stale && (
          <div className="terminal-search-stale" data-testid="terminal-search-stale" role="status">
            snapshot stale · reconnect gateway for live index
          </div>
        )}
        {body}
      </section>
    );
  }

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Code Search</CardTitle>
      </CardHeader>
      {body}
    </Card>
  );
}

function mapResult(item: GrpcSearchResultItem): SearchResult {
  return {
    repoId: item.repoId,
    filePath: item.filePath,
    startLine: item.startLine,
    endLine: item.endLine,
    contentSnippet: item.contentSnippet,
    matchType: item.matchType,
    score: item.score,
    symbolName: item.symbolName ?? undefined,
    symbolKind: item.symbolKind ?? undefined,
    parentSymbol: item.parentSymbol ?? undefined,
  };
}

function matchTypeClass(type: string): string {
  switch (type) {
    case "text": return "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)]";
    case "semantic": return "bg-purple-500/20 text-purple-400";
    case "ast": return "bg-cyan-500/20 text-cyan-400";
    default: return "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)]";
  }
}
