import { useState, useCallback } from "react";
import { createClient } from "@connectrpc/connect";
import { EngineService } from "@/grpc/engine_pb";
import type { SearchResultItem as GrpcSearchResultItem } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { SearchRequestSchema } from "@/grpc/engine_pb";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { getSharedTransport, type GrpcConnectionState } from "@/hooks/useGrpcWeb";
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
}

export function SearchPanel({ grpcState, onNavigateFile, stale }: { grpcState?: GrpcConnectionState; onNavigateFile?: (nav: FileBrowserNavigateEvent) => void; stale?: boolean }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const [maxResults, setMaxResults] = useState(20);
  const [showFilters, setShowFilters] = useState(false);
  const [modes, setModes] = useState<SearchMode[]>([]);
  const [language, setLanguage] = useState("");

  const toggleMode = (m: SearchMode) => {
    setModes((prev) => prev.includes(m) ? prev.filter((x) => x !== m) : [...prev, m]);
  };

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
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
      const resp = await client.search(req);
      setResults(resp.items.map(mapResult));
      setSearched(true);
    } catch (err) {
      setError(String(err));
      setResults([]);
    } finally {
      setSearching(false);
    }
  }, [query, maxResults, modes, language]);

  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>Code Search</CardTitle>
      </CardHeader>

      <div className="flex gap-2 mb-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleSearch(); }}
          aria-label="Search query"
          className="flex-1 bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
          placeholder="Search indexed repos… (e.g. 'fn search' or 'class EngineApi')"
        />
        <button
          onClick={handleSearch}
          disabled={searching || !query.trim()}
          aria-label="Run search"
          className="btn-action-info border border-blue-500 rounded-md px-4 py-2 text-sm font-medium cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {searching ? "Searching…" : "Search"}
        </button>
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
        {/* ponytail: toggle filter panel */}
        <button
          onClick={() => setShowFilters(!showFilters)}
          className={cn(
            "px-2 py-2 rounded-md border text-sm cursor-pointer transition-colors",
            showFilters ? "border-blue-500 text-blue-400" : "border-[var(--border-color)] text-[var(--text-muted)] hover:text-[var(--text-primary)]"
          )}
          title="Search filters"
        >
          ⚙
        </button>
      </div>

      {/* ponytail: collapsible filter controls */}
      {showFilters && (
        <div className="flex flex-wrap items-center gap-2 mb-2 p-2 bg-[var(--bg-primary)] rounded-md border border-[var(--border-color)]">
          <span className="text-xs text-[var(--text-muted)]">Mode:</span>
          {(["text", "semantic", "ast"] as SearchMode[]).map((m) => (
            <button
              key={m}
              onClick={() => toggleMode(m)}
              className={cn(
                "text-xs px-1.5 py-0.5 rounded cursor-pointer",
                modes.includes(m) ? matchTypeClass(m) : "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)]"
              )}
            >
              {m}
            </button>
          ))}
          <span className="text-xs text-[var(--text-muted)] ml-2">Lang:</span>
          <input
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            placeholder="e.g. rust, python"
            className="w-28 bg-[var(--bg-surface-alt)] border border-[var(--border-color)] rounded px-2 py-0.5 text-xs text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
          />
        </div>
      )}

      {error && (
        <p className="text-xs text-red-400 mb-2">Search failed: {error}</p>
      )}

      {searched && results.length === 0 && !error && (
        <p className="text-sm text-[var(--text-muted)]">No results found for "{query}"</p>
      )}

      {results.length > 0 && (
        <ul className="space-y-1.5 max-h-96 overflow-y-auto" aria-label="Search results">
          {results.map((r, i) => (
            <li key={`${r.filePath}-${r.startLine}-${i}`} className="border-l-2 border-l-blue-500 pl-2 py-1">
              <div className="flex items-center justify-between text-sm">
                <button
                  onClick={() => onNavigateFile?.({ repoId: r.repoId, path: r.filePath, line: r.startLine })}
                  className="text-[var(--text-primary)] font-mono truncate hover:text-blue-400 hover:underline text-left"
                >
                  {r.filePath}
                  {r.symbolName && <span className="text-blue-400 ml-1 font-sans not-italic">{r.symbolName}</span>}
                </button>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-xs text-[var(--text-muted)]">{r.repoId}</span>
                  <span className="text-xs text-[var(--text-muted)]">L{r.startLine}{r.endLine > r.startLine ? `-${r.endLine}` : ""}</span>
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
          ))}
        </ul>
      )}

      {!searched && (
        <p className="text-xs text-[var(--text-muted)]">
          Search across indexed repositories via gRPC-Web.
          {grpcState !== "connected" && " gRPC server is currently disconnected — search requires an active connection."}
        </p>
      )}
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
