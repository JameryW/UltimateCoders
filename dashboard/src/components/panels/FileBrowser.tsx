import { useState, useEffect, useCallback, useRef } from "react";
import hljs from "highlight.js";
import * as api from "@/api/endpoints";
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
}

export function FileBrowser({ initialNav, onNavConsumed }: FileBrowserProps) {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string>("");
  const [currentPath, setCurrentPath] = useState("");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [fileContent, setFileContent] = useState<FileContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [highlightLine, setHighlightLine] = useState<number | null>(null);
  const codeRef = useRef<HTMLElement>(null);
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

  // Handle initial navigation from external components
  useEffect(() => {
    if (!initialNav) return;
    if (initialNav.repoId !== selectedRepo) {
      setSelectedRepo(initialNav.repoId);
    }
    // Navigate then open file
    const nav = initialNav;
    // Check if it's a file (has extension) or directory
    const pathParts = nav.path.split("/");
    const lastPart = pathParts[pathParts.length - 1]!;
    const hasExtension = lastPart.includes(".");
    if (hasExtension) {
      // It's a file — load its content
      loadFile(nav.repoId, nav.path, nav.line);
    } else {
      loadDirectory(nav.repoId, nav.path);
    }
    onNavConsumed?.();
  }, [initialNav]);

  // Load file content
  const loadFile = async (repoId: string, path: string, line?: number) => {
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
  };

  // Apply highlight.js after file content loads
  useEffect(() => {
    if (fileContent?.content && codeRef.current) {
      // Reset previous highlight
      codeRef.current.removeAttribute("data-highlighted");
      hljs.highlightElement(codeRef.current);
    }
    // Scroll to highlighted line
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

  // Navigate into directory
  const navigateTo = (path: string) => {
    loadDirectory(selectedRepo, path);
  };

  // Navigate breadcrumb
  const onBreadcrumb = (path: string) => {
    navigateTo(path);
  };

  // Click file entry
  const onFileClick = (entry: DirEntry) => {
    if (entry.type === "directory") {
      navigateTo(entry.path);
    } else {
      loadFile(selectedRepo, entry.path);
    }
  };

  // Back to directory from file view
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

  // Repo selector
  const repoSelector = (
    <div className="flex items-center gap-2 mb-2">
      <label className="text-xs text-[var(--text-muted)]">Repo:</label>
      <select
        value={selectedRepo}
        onChange={(e) => { setSelectedRepo(e.target.value); setFileContent(null); }}
        className="text-xs bg-[var(--bg-surface-alt)] text-[var(--text-primary)] border border-[var(--border-color)] rounded px-2 py-1"
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
      <div>
        {repoSelector}
        <div className="flex items-center gap-2 mb-2">
          <button
            onClick={backToDirectory}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            ← Back
          </button>
          <span className="text-xs font-mono text-[var(--text-secondary)] truncate" title={fileContent.path}>
            {fileContent.path}
          </span>
          <span className="text-xs text-[var(--text-muted)]">
            {fmtSize(fileContent.size)} · {fileContent.lines} lines
          </span>
          {fileContent.truncated && (
            <span className="text-xs text-yellow-400">⚠ truncated</span>
          )}
        </div>
        {fileContent.binary ? (
          <div className="text-xs text-[var(--text-muted)] py-4 text-center">
            Binary file ({fmtSize(fileContent.size)})
          </div>
        ) : (
          <div className="overflow-auto max-h-[500px] border border-[var(--border-color)] rounded text-xs font-mono">
            <table className="w-full">
              <tbody>
                {fileContent.content!.split("\n").map((line, i) => {
                  const lineNum = i + 1;
                  const isHighlighted = lineNum === highlightLine;
                  return (
                    <tr
                      key={i}
                      ref={isHighlighted ? lineRef : undefined}
                      className={isHighlighted ? "bg-yellow-500/20" : "hover:bg-[var(--bg-surface-alt)]"}
                    >
                      <td className="text-right text-[var(--text-muted)] pr-3 pl-2 select-none border-r border-[var(--border-color)] w-12 align-top">
                        {lineNum}
                      </td>
                      <td className="pl-3 pr-2 whitespace-pre">
                        <code
                          ref={i === 0 ? codeRef : undefined}
                          className={fileContent.language ? `language-${fileContent.language}` : ""}
                        >
                          {line}
                        </code>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  }

  // Directory listing view
  return (
    <div>
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
    </div>
  );
}
