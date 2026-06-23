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
      exists: r.indexed,
    })),
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
