import { useState, useCallback } from "react";
import { createClient } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { EngineService } from "@/grpc/engine_pb";
import type { SearchResultItem as GrpcSearchResultItem } from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { SearchRequestSchema } from "@/grpc/engine_pb";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { GrpcConnectionState } from "@/hooks/useGrpcWeb";

/** gRPC-Web server address — empty = same-origin. */
const GRPC_WEB_ADDR = import.meta.env.VITE_GRPC_WEB_ADDR ?? "";

// ponytail: shared transport — avoids recreating per search
let _transport: ReturnType<typeof createGrpcWebTransport> | null = null;
function getTransport() {
  if (!_transport) _transport = createGrpcWebTransport({ baseUrl: GRPC_WEB_ADDR });
  return _transport;
}

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

export function SearchPanel({ grpcState }: { grpcState?: GrpcConnectionState }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setError(null);
    try {
      const transport = getTransport();
      const client = createClient(EngineService, transport);
      const req = create(SearchRequestSchema, { query: q, maxResults: 20 });
      const resp = await client.search(req);
      setResults(resp.items.map(mapResult));
      setSearched(true);
    } catch (err) {
      setError(String(err));
      setResults([]);
    } finally {
      setSearching(false);
    }
  }, [query]);

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>Code Search</CardTitle>
      </CardHeader>

      <div className="flex gap-2 mb-3">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleSearch(); }}
          aria-label="Search query"
          className="flex-1 bg-dark-900 border border-dark-700 rounded-md px-3 py-2 text-sm text-gray-200 focus:border-blue-500 focus:outline-none"
          placeholder="Search indexed repos… (e.g. 'fn search' or 'class EngineApi')"
        />
        <button
          onClick={handleSearch}
          disabled={searching || !query.trim()}
          aria-label="Run search"
          className="bg-blue-900/50 text-blue-300 border border-blue-500 rounded-md px-4 py-2 text-sm font-medium cursor-pointer hover:bg-blue-900/70 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {searching ? "Searching…" : "Search"}
        </button>
      </div>

      {error && (
        <p className="text-xs text-red-400 mb-2">Search failed: {error}</p>
      )}

      {searched && results.length === 0 && !error && (
        <p className="text-sm text-gray-500">No results found for "{query}"</p>
      )}

      {results.length > 0 && (
        <ul className="space-y-1.5 max-h-96 overflow-y-auto" aria-label="Search results">
          {results.map((r, i) => (
            <li key={`${r.filePath}-${r.startLine}-${i}`} className="border-l-2 border-l-blue-500 pl-2 py-1">
              <div className="flex items-center justify-between text-sm">
                <span className="text-gray-300 font-mono truncate">
                  {r.filePath}
                  {r.symbolName && <span className="text-blue-400 ml-1 font-sans not-italic">{r.symbolName}</span>}
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-xs text-gray-500">{r.repoId}</span>
                  <span className="text-xs text-gray-500">L{r.startLine}{r.endLine > r.startLine ? `-${r.endLine}` : ""}</span>
                  <span className={cn("text-xs px-1.5 py-0.5 rounded", matchTypeClass(r.matchType))}>
                    {r.matchType}
                  </span>
                </div>
              </div>
              {r.contentSnippet && (
                <pre className="text-xs text-gray-500 mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono bg-dark-900/50 rounded p-1.5 max-h-20 overflow-y-auto">
                  {r.contentSnippet}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}

      {!searched && (
        <p className="text-xs text-gray-500">
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
    case "text": return "bg-gray-800 text-gray-400";
    case "semantic": return "bg-purple-900/50 text-purple-300";
    case "ast": return "bg-cyan-900/50 text-cyan-300";
    default: return "bg-gray-800 text-gray-400";
  }
}
