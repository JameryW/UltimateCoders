import { describe, expect, test } from "bun:test";

import {
  CompilerTcb,
  CensoredMemoryModel,
  InfraRsi,
  type MemoryTrial,
} from "./infra_rsi";

const trial = (id: string, peakGb: number, oom = false): MemoryTrial => ({ id, peakGb, oom });

describe("censored peak-memory model", () => {
  test("uses the largest observed peak", () => {
    const fit = new CensoredMemoryModel(80).fit([trial("a", 20), trial("b", 40)]);
    expect(fit.estimatedPeakGb).toBe(40);
    expect(fit.censoredCount).toBe(0);
  });

  test("treats OOM as a censored lower bound", () => {
    const fit = new CensoredMemoryModel(80).fit([trial("a", 30), trial("b", 80, true)]);
    expect(fit.estimatedPeakGb).toBe(88);
    expect(fit.censoredCount).toBe(1);
  });

  test("rejects empty trials", () => {
    expect(() => new CensoredMemoryModel().fit([])).toThrow("at least one");
  });

  test("rejects malformed trials", () => {
    expect(() => new CensoredMemoryModel().fit([trial("", 10)])).toThrow("invalid");
  });

  test("rejects non-positive OOM limit", () => {
    expect(() => new CensoredMemoryModel(0)).toThrow("positive");
  });
});

describe("compiler TCB", () => {
  test("allows an unknown compiler version", () => {
    const result = new CompilerTcb().check("rustc-9.9", "patch");
    expect(result.safe).toBe(true);
  });

  test("blocks a known miscompile token", () => {
    const tcb = new CompilerTcb([{ version: "rustc-1.80.0", knownMiscompiles: ["miscompile-a"] }]);
    const result = tcb.check("rustc-1.80.0", "use miscompile-a here");
    expect(result.safe).toBe(false);
    expect(result.matches).toEqual(["miscompile-a"]);
  });

  test("allows a matching compiler without a trigger", () => {
    const tcb = new CompilerTcb([{ version: "rustc-1.80.0", knownMiscompiles: ["miscompile-a"] }]);
    expect(tcb.check("rustc-1.80.0", "clean patch").safe).toBe(true);
  });

  test("rejects an empty fingerprint version", () => {
    expect(() => new CompilerTcb([{ version: "", knownMiscompiles: [] }])).toThrow("version");
  });
});

describe("InfraRSI optimiser", () => {
  test("reports the best trial gain", () => {
    const result = new InfraRsi(40).optimize(17.4, [0.1, 0.5]);
    expect(result.optimizedMfu).toBe(26.1);
    expect(result.speedup).toBe(1.5);
  });

  test("caps at the target MFU", () => {
    const result = new InfraRsi(30).optimize(20, [2]);
    expect(result.optimizedMfu).toBe(30);
    expect(result.speedup).toBe(1.5);
  });

  test("rejects an empty trial list", () => {
    expect(() => new InfraRsi().optimize(20, [])).toThrow("trials");
  });

  test("rejects a negative gain", () => {
    expect(() => new InfraRsi().optimize(20, [-1])).toThrow("gains");
  });
});
