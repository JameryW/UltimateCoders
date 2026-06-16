import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest';
import {resolveProtoPath} from './client.js';

// Mock fs and path for resolveProtoPath testing
vi.mock('node:fs', () => ({
  default: {
    existsSync: vi.fn(),
  },
  existsSync: vi.fn(),
}));

vi.mock('node:path', () => ({
  default: {
    resolve: vi.fn((...args: string[]) => args.join('/')),
  },
  resolve: vi.fn((...args: string[]) => args.join('/')),
}));

import fs from 'node:fs';
import path from 'node:path';

describe('resolveProtoPath', () => {
  const mockedExistsSync = vi.mocked(fs.existsSync);
  const mockedResolve = vi.mocked(path.resolve);

  beforeEach(() => {
    vi.clearAllMocks();
    delete process.env.GRPC_PROTO_PATH;
  });

  afterEach(() => {
    delete process.env.GRPC_PROTO_PATH;
  });

  it('returns env var path when GRPC_PROTO_PATH is set and path exists', () => {
    process.env.GRPC_PROTO_PATH = '/custom/path/engine.proto';
    mockedExistsSync.mockReturnValue(true);

    const result = resolveProtoPath();
    expect(result).toBe('/custom/path/engine.proto');
    expect(mockedExistsSync).toHaveBeenCalledWith('/custom/path/engine.proto');
  });

  it('falls back to relative path when env var path does not exist', () => {
    process.env.GRPC_PROTO_PATH = '/nonexistent/engine.proto';
    // First call (env path) returns false, second (relative) returns true
    mockedExistsSync.mockReturnValueOnce(false).mockReturnValueOnce(true);
    mockedResolve.mockReturnValue('/relative/crates/uc-grpc/proto/engine.proto');

    const result = resolveProtoPath();
    expect(result).toBe('/relative/crates/uc-grpc/proto/engine.proto');
  });

  it('throws Error when neither path exists', () => {
    process.env.GRPC_PROTO_PATH = '/nonexistent/engine.proto';
    mockedExistsSync.mockReturnValue(false);
    mockedResolve.mockReturnValue('/relative/crates/uc-grpc/proto/engine.proto');

    expect(() => resolveProtoPath()).toThrow('Cannot find engine.proto');
  });

  it('uses relative path when GRPC_PROTO_PATH is not set', () => {
    mockedExistsSync.mockReturnValue(true);
    mockedResolve.mockReturnValue('/relative/crates/uc-grpc/proto/engine.proto');

    const result = resolveProtoPath();
    expect(result).toBe('/relative/crates/uc-grpc/proto/engine.proto');
  });

  it('throws Error when relative path does not exist and no env var', () => {
    mockedExistsSync.mockReturnValue(false);
    mockedResolve.mockReturnValue('/relative/crates/uc-grpc/proto/engine.proto');

    expect(() => resolveProtoPath()).toThrow('Cannot find engine.proto');
  });
});
