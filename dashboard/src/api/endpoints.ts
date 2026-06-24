/**
 * File Browser API — migrated from REST to gRPC-Web.
 *
 * All dashboard data now goes through gRPC-Web (EngineService).
 * This module provides thin wrappers for the File Browser panel.
 */

import { createClient } from "@connectrpc/connect";
import { EngineService } from "@/grpc/engine_pb";
import {
  ListReposRequestSchema,
  ListDirRequestSchema,
  GetFileRequestSchema,
  IndexRepoRequestSchema,
  RemoveIndexRequestSchema,
  GetIndexStateRequestSchema,
} from "@/grpc/engine_pb";
import { create } from "@bufbuild/protobuf";
import { getSharedTransport } from "@/hooks/useGrpcWeb";

import type { RepoInfo, DirEntry, FileContent } from "@/types/dashboard";

// ── File Browser API (gRPC-Web) ────────────────────────────

export async function getRepos(): Promise<{ repos: RepoInfo[] }> {
  const transport = getSharedTransport();
  const client = createClient(EngineService, transport);
  const resp = await client.listRepos(create(ListReposRequestSchema, {}));
  return {
    repos: resp.repos.map((r) => ({
      repo_id: r.repoId,
      local_path: r.localPath ?? "",
      remote_url: r.remoteUrl ?? undefined,
      default_branch: r.defaultBranch ?? undefined,
      exists: r.indexed,
      indexed: r.indexed,
      files_count: Number(r.filesCount ?? 0),
      symbols_count: Number(r.symbolsCount ?? 0),
      chunks_count: Number(r.chunksCount ?? 0),
      last_indexed_sha: r.lastIndexedSha ?? undefined,
    })),
  };
}

export async function indexRepo(
  repoId: string,
  localPath: string,
  remoteUrl?: string,
  defaultBranch = "main",
  forceFull = false,
): Promise<{ repo_id: string; files_indexed: number }> {
  const transport = getSharedTransport();
  const client = createClient(EngineService, transport);
  const resp = await client.indexRepo(
    create(IndexRepoRequestSchema, {
      repoId,
      localPath,
      remoteUrl: remoteUrl ?? "",
      defaultBranch,
      forceFull,
    }),
  );
  return {
    repo_id: resp.repoId,
    files_indexed: Number(resp.filesIndexed),
  };
}

export async function removeIndex(repoId: string): Promise<void> {
  const transport = getSharedTransport();
  const client = createClient(EngineService, transport);
  await client.removeIndex(
    create(RemoveIndexRequestSchema, { repoId }),
  );
}

export async function getIndexState(
  repoId: string,
): Promise<RepoInfo | null> {
  const transport = getSharedTransport();
  const client = createClient(EngineService, transport);
  const resp = await client.getIndexState(
    create(GetIndexStateRequestSchema, { repoId }),
  );
  if (!resp.indexed) return null;
  return {
    repo_id: resp.repoId,
    local_path: "",
    exists: true,
    indexed: resp.indexed,
    files_count: Number(resp.filesCount),
    symbols_count: Number(resp.symbolsCount),
    chunks_count: Number(resp.chunksCount),
    last_indexed_sha: resp.lastIndexedSha ?? undefined,
  };
}

export async function getRepoTree(
  repoId: string,
  path = "",
): Promise<{ entries: DirEntry[] }> {
  const transport = getSharedTransport();
  const client = createClient(EngineService, transport);
  const resp = await client.listDir(
    create(ListDirRequestSchema, { repoId, path }),
  );
  return {
    entries: resp.entries.map((e) => ({
      name: e.name,
      path: e.path,
      type: e.entryType === "directory" ? "directory" : "file",
      size: Number(e.size),
    })),
  };
}

export async function getRepoFile(
  repoId: string,
  path: string,
): Promise<FileContent> {
  const transport = getSharedTransport();
  const client = createClient(EngineService, transport);
  const resp = await client.getFile(
    create(GetFileRequestSchema, { repoId, path }),
  );
  return {
    repo_id: resp.repoId,
    path: resp.path,
    binary: resp.binary,
    size: Number(resp.size),
    content: resp.content ?? undefined,
    language: resp.language ?? undefined,
    truncated: resp.truncated,
    lines: resp.lines,
  };
}
