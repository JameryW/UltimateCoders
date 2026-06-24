import { useState, useEffect, useCallback, useRef, memo } from "react";
// ponytail: common subset only (~37 langs) vs full 384-language bundle — saves ~350KB
import hljs from "highlight.js/lib/common";
import * as api from "@/api/endpoints";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import type { RepoInfo, DirEntry, FileContent } from "@/types/dashboard";

/** Format bytes to human-readable. */
function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export interface FileBrowserNavigateEvent {
  repoId: string;
  path: string;
  line?: number;
}

interface FileBrowserProps {
  /** Initial navigation (from SearchPanel/OutputFiles click). */
  initialNav?: FileBrowserNavigateEvent | null;
  onNavConsumed?: () => void;
  stale?: boolean;
}

export const FileBrowser = memo(function FileBrowser({ initialNav, onNavConsumed, stale = false }: FileBrowserProps) {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string>("");
  const [currentPath, setCurrentPath] = useState("");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [fileContent, setFileContent] = useState<FileContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [highlightLine, setHighlightLine] = useState<number | null>(null);
  const codeBlockRef = useRef<HTMLDivElement>(null);
  const lineRef = useRef<HTMLTableRowElement>(null);

  // Load repos on mount
  useEffect(() => {
    api.getRepos().then((data) => {
      setRepos(data.repos);
      if (data.repos.length > 0 && !selectedRepo) {
        setSelectedRepo(data.repos[0]!.repo_id);
      }
    }).catch(() => { /* ignore */ });
  }, []);

  // Load directory when repo or path changes
  const loadDirectory = useCallback(async (repoId: string, path: string) => {
    if (!repoId) return;
    setLoading(true);
    setError("");
    setFileContent(null);
    try {
      const data = await api.getRepoTree(repoId, path);
      setEntries(data.entries);
      setCurrentPath(path);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedRepo) loadDirectory(selectedRepo, "");
  }, [selectedRepo, loadDirectory]);

  // Load file content — useCallback for stable reference in initialNav effect
  const loadFile = useCallback(async (repoId: string, path: string, line?: number) => {
    setLoading(true);
    setError("");
    setHighlightLine(line ?? null);
    try {
      const data = await api.getRepoFile(repoId, path);
      setFileContent(data);
      setEntries([]);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Handle initial navigation from external components
  useEffect(() => {
    if (!initialNav) return;
    if (initialNav.repoId !== selectedRepo) {
      setSelectedRepo(initialNav.repoId);
    }
    const nav = initialNav;
    const pathParts = nav.path.split("/");
    const lastPart = pathParts[pathParts.length - 1]!;
    const hasExtension = lastPart.includes(".");
    if (hasExtension) {
      loadFile(nav.repoId, nav.path, nav.line);
    } else {
      loadDirectory(nav.repoId, nav.path);
    }
    onNavConsumed?.();
  }, [initialNav, selectedRepo, loadFile, loadDirectory, onNavConsumed]);

  // Apply highlight.js to the entire code block after file content loads
  // ponytail: highlight the parent <code> block wrapping all lines, not individual <code> per line
  useEffect(() => {
    if (fileContent?.content && codeBlockRef.current) {
      const codeEl = codeBlockRef.current.querySelector("code");
      if (codeEl) {
        codeEl.removeAttribute("data-highlighted");
        hljs.highlightElement(codeEl);
      }
    }
    if (highlightLine && lineRef.current) {
      lineRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [fileContent, highlightLine]);

  // Breadcrumb segments
  const pathParts = currentPath ? currentPath.split("/") : [];
  const breadcrumbs = [{ label: "root", path: "" }, ...pathParts.map((p, i) => ({
    label: p,
    path: pathParts.slice(0, i + 1).join("/"),
  }))];

  const navigateTo = (path: string) => {
    loadDirectory(selectedRepo, path);
  };

  const onBreadcrumb = (path: string) => {
    navigateTo(path);
  };

  const onFileClick = (entry: DirEntry) => {
    if (entry.type === "directory") {
      navigateTo(entry.path);
    } else {
      loadFile(selectedRepo, entry.path);
    }
  };

  const backToDirectory = () => {
    if (fileContent) {
      const dirPath = fileContent.path.includes("/")
        ? fileContent.path.substring(0, fileContent.path.lastIndexOf("/"))
        : "";
      setFileContent(null);
      setHighlightLine(null);
      loadDirectory(selectedRepo, dirPath || currentPath);
    }
  };

  // Split content into lines for line-numbered display
  const contentLines = fileContent?.content?.split("\n") ?? [];

  // Repo selector
  const repoSelector = (
    <div className="flex items-center gap-2 mb-2">
      <label className="text-xs text-[var(--text-muted)]">Repo:</label>
      <select
        value={selectedRepo}
        onChange={(e) => { setSelectedRepo(e.target.value); setFileContent(null); }}
        className="bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm text-[var(--text-primary)] focus:border-blue-500 focus:outline-none"
      >
        {repos.map((r) => (
          <option key={r.repo_id} value={r.repo_id}>{r.repo_id}</option>
        ))}
      </select>
    </div>
  );

  // Breadcrumb nav
  const breadcrumbNav = !fileContent && (
    <div className="flex items-center gap-1 flex-wrap text-xs mb-2">
      {breadcrumbs.map((bc, i) => (
        <span key={bc.path + i} className="flex items-center gap-1">
          {i > 0 && <span className="text-[var(--text-muted)]">/</span>}
          <button
            onClick={() => onBreadcrumb(bc.path)}
            className="text-blue-400 hover:text-blue-300 hover:underline"
          >
            {bc.label}
          </button>
        </span>
      ))}
    </div>
  );

  // File content view
  if (fileContent) {
    return (
      <Card stale={stale}>
        <CardHeader>
          <CardTitle>File Browser</CardTitle>
        </CardHeader>
        {repoSelector}
        <div className="flex items-center gap-2 mb-2">
          <button
            onClick={backToDirectory}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            &larr; Back
          </button>
          <span className="text-xs font-mono text-[var(--text-secondary)] truncate" title={fileContent.path}>
            {fileContent.path}
          </span>
          <span className="text-xs text-[var(--text-muted)]">
            {fmtSize(fileContent.size)} &middot; {fileContent.lines} lines
          </span>
          {fileContent.truncated && (
            <span className="text-xs text-yellow-400">&#9888; truncated</span>
          )}
        </div>
        {fileContent.binary ? (
          <div className="text-xs text-[var(--text-muted)] py-4 text-center">
            Binary file ({fmtSize(fileContent.size)})
          </div>
        ) : (
          <div ref={codeBlockRef} className="overflow-auto max-h-[500px] border border-[var(--border-color)] rounded text-xs font-mono">
            {/* ponytail: single <pre><code> block for highlight.js to process the whole file at once */}
            <pre className={`m-0 p-0 bg-transparent ${fileContent.language ? `language-${fileContent.language}` : ""}`}>
              <code className={fileContent.language ? `language-${fileContent.language}` : ""}>
                <table className="w-full">
                  <tbody>
                    {contentLines.map((line, i) => {
                      const lineNum = i + 1;
                      const isHighlighted = lineNum === highlightLine;
                      return (
                        <tr
                          key={lineNum}
                          ref={isHighlighted ? lineRef : undefined}
                          className={isHighlighted ? "bg-yellow-500/20" : "hover:bg-[var(--bg-surface-alt)]"}
                        >
                          <td className="text-right text-[var(--text-muted)] pr-3 pl-2 select-none border-r border-[var(--border-color)] w-12 align-top">
                            {lineNum}
                          </td>
                          <td className="pl-3 pr-2 whitespace-pre">
                            {line}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </code>
            </pre>
          </div>
        )}
      </Card>
    );
  }

  // Directory listing view
  return (
    <Card stale={stale}>
      <CardHeader>
        <CardTitle>File Browser</CardTitle>
      </CardHeader>
      {repoSelector}
      {breadcrumbNav}
      {error && <p className="text-xs text-red-400 mb-2">{error}</p>}
      {loading ? (
        <div className="text-xs text-[var(--text-muted)] py-4 text-center animate-pulse">Loading...</div>
      ) : entries.length === 0 && !fileContent ? (
        <div className="text-xs text-[var(--text-muted)] py-4 text-center">Empty directory</div>
      ) : (
        <div className="overflow-auto max-h-[400px] border border-[var(--border-color)] rounded">
          {entries.map((entry) => (
            <button
              key={entry.path}
              onClick={() => onFileClick(entry)}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-xs hover:bg-[var(--bg-surface-alt)] transition-colors text-left border-b border-[var(--border-color)] last:border-b-0"
            >
              <span className="w-4 text-center">
                {entry.type === "directory" ? "📁" : "📄"}
              </span>
              <span className="font-mono text-[var(--text-primary)] truncate flex-1">
                {entry.name}
              </span>
              {entry.type === "file" && (
                <span className="text-[var(--text-muted)] text-[10px]">
                  {fmtSize(entry.size)}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </Card>
  );
});
