import { memo, useCallback, useEffect, useState } from "react";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { Badge } from "@/components/ui/badge";
import { getRepos, indexRepo, removeIndex } from "@/api/endpoints";
import type { RepoInfo } from "@/types/dashboard";

interface RepoManagementPanelProps {
  className?: string;
}

export const RepoManagementPanel = memo(function RepoManagementPanel({
  className,
}: RepoManagementPanelProps) {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const fetchRepos = useCallback(async () => {
    try {
      const data = await getRepos();
      setRepos(data.repos);
    } catch {
      // gRPC unavailable — show empty
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRepos();
  }, [fetchRepos]);

  // Collect unique tags
  const allTags = Array.from(
    new Set(repos.flatMap((r) => r.tags ?? [])),
  ).sort();

  // Filter by tag
  const filtered = tagFilter
    ? repos.filter((r) => r.tags?.includes(tagFilter))
    : repos;

  const handleReindex = useCallback(
    async (repo: RepoInfo, forceFull = false) => {
      setActionLoading(repo.repo_id);
      try {
        await indexRepo(
          repo.repo_id,
          repo.local_path,
          repo.remote_url,
          repo.default_branch,
          forceFull,
        );
        await fetchRepos();
      } catch {
        // error handled silently
      } finally {
        setActionLoading(null);
      }
    },
    [fetchRepos],
  );

  const handleRemove = useCallback(
    async (repoId: string) => {
      if (!confirm(`Remove index for "${repoId}"?`)) return;
      setActionLoading(repoId);
      try {
        await removeIndex(repoId);
        await fetchRepos();
      } catch {
        // error handled silently
      } finally {
        setActionLoading(null);
      }
    },
    [fetchRepos],
  );

  return (
    <Card className={cn("col-span-12", className)}>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>Repositories</CardTitle>
        <button
          onClick={fetchRepos}
          className="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
        >
          ↻ Refresh
        </button>
      </CardHeader>

      {/* Tag filter */}
      {allTags.length > 0 && (
        <div className="flex gap-1.5 px-4 pb-2 flex-wrap">
          <button
            onClick={() => setTagFilter(null)}
            className={cn(
              "px-2 py-0.5 text-xs rounded-full transition-colors",
              tagFilter === null
                ? "bg-[var(--accent)] text-white"
                : "bg-[var(--surface-2)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]",
            )}
          >
            all
          </button>
          {allTags.map((tag) => (
            <button
              key={tag}
              onClick={() =>
                setTagFilter(tag === tagFilter ? null : tag)
              }
              className={cn(
                "px-2 py-0.5 text-xs rounded-full transition-colors",
                tag === tagFilter
                  ? "bg-[var(--accent)] text-white"
                  : "bg-[var(--surface-2)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]",
              )}
            >
              {tag}
            </button>
          ))}
        </div>
      )}

      <div className="px-4 pb-4">
        {loading ? (
          <div className="text-sm text-[var(--text-secondary)] py-4 text-center">
            Loading repositories…
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            title="No repositories"
            description="Configure repos in uc.repos.yaml or add via scan_dirs."
          />
        ) : (
          <div className="space-y-2">
            {filtered.map((repo) => (
              <RepoRow
                key={repo.repo_id}
                repo={repo}
                isLoading={actionLoading === repo.repo_id}
                onReindex={handleReindex}
                onRemove={handleRemove}
              />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
});

// ── Repo row ────────────────────────────────────────────────

interface RepoRowProps {
  repo: RepoInfo;
  isLoading: boolean;
  onReindex: (repo: RepoInfo, forceFull?: boolean) => void;
  onRemove: (repoId: string) => void;
}

const RepoRow = memo(function RepoRow({
  repo,
  isLoading,
  onReindex,
  onRemove,
}: RepoRowProps) {
  const statusColor = repo.indexed
    ? "text-green-500"
    : "text-[var(--text-secondary)]";

  return (
    <div className="flex items-center justify-between gap-3 p-2.5 rounded-lg bg-[var(--surface-1)] border border-[var(--border)]">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className={cn("text-xs", statusColor)}>●</span>
          <span className="font-mono text-sm font-medium truncate">
            {repo.repo_id}
          </span>
          {(repo.tags ?? []).map((tag) => (
            <Badge key={tag} variant="secondary">
              {tag}
            </Badge>
          ))}
        </div>
        <div className="flex items-center gap-3 text-xs text-[var(--text-secondary)] mt-0.5">
          <span className="truncate">{repo.local_path}</span>
          {repo.indexed && (
            <>
              <span>
                {repo.files_count ?? 0} files
              </span>
              <span>
                {repo.symbols_count ?? 0} symbols
              </span>
              <span>
                {repo.chunks_count ?? 0} chunks
              </span>
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        <button
          onClick={() => onReindex(repo, false)}
          disabled={isLoading}
          className="px-2 py-1 text-xs rounded bg-[var(--surface-2)] hover:bg-[var(--accent)] hover:text-white transition-colors disabled:opacity-50"
          title="Incremental index"
        >
          {isLoading ? "…" : "Sync"}
        </button>
        <button
          onClick={() => onReindex(repo, true)}
          disabled={isLoading}
          className="px-2 py-1 text-xs rounded bg-[var(--surface-2)] hover:bg-[var(--accent)] hover:text-white transition-colors disabled:opacity-50"
          title="Full reindex"
        >
          Full
        </button>
        <button
          onClick={() => onRemove(repo.repo_id)}
          disabled={isLoading}
          className="px-2 py-1 text-xs rounded bg-[var(--surface-2)] hover:bg-red-500 hover:text-white transition-colors disabled:opacity-50"
          title="Remove index"
        >
          ✕
        </button>
      </div>
    </div>
  );
});
