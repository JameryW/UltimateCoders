/**
 * InfraRSI and compiler-TCB guards (ledger item #114, UC_INFRA_RSI).
 *
 * Adds a censored peak-memory model for OOM trials, an infrastructure
 * recursive-optimisation report, and a compiler trusted-computing-base
 * fingerprint check. Pure in-memory calculations; no training or compilers
 * are invoked.
 */

export interface MemoryTrial {
  id: string;
  peakGb: number;
  oom: boolean;
}

export interface MemoryFit {
  estimatedPeakGb: number;
  observedCount: number;
  censoredCount: number;
  limitGb: number;
}

export class CensoredMemoryModel {
  constructor(private readonly oomLimitGb = 80) {
    if (!Number.isFinite(oomLimitGb) || oomLimitGb <= 0) {
      throw new Error("oomLimitGb must be positive");
    }
  }

  fit(trials: MemoryTrial[]): MemoryFit {
    if (trials.length === 0) {
      throw new Error("at least one memory trial is required");
    }
    let observed = 0;
    let observedMax = 0;
    let censored = 0;
    for (const trial of trials) {
      if (!trial.id.trim() || !Number.isFinite(trial.peakGb) || trial.peakGb < 0) {
        throw new Error("invalid memory trial");
      }
      if (trial.oom) {
        censored += 1;
      } else {
        observed += 1;
        observedMax = Math.max(observedMax, trial.peakGb);
      }
    }
    const censoredPeak = censored > 0 ? this.oomLimitGb * 1.1 : 0;
    return {
      estimatedPeakGb: round(Math.max(observedMax, censoredPeak)),
      observedCount: observed,
      censoredCount: censored,
      limitGb: this.oomLimitGb,
    };
  }
}

export interface CompilerFingerprint {
  version: string;
  knownMiscompiles: string[];
}

export interface TcbCheck {
  safe: boolean;
  compilerVersion: string;
  matches: string[];
}

export class CompilerTcb {
  private readonly fingerprints: CompilerFingerprint[];

  constructor(fingerprints: CompilerFingerprint[] = []) {
    for (const fingerprint of fingerprints) {
      if (!fingerprint.version.trim()) {
        throw new Error("compiler version must not be empty");
      }
    }
    this.fingerprints = fingerprints.map((fingerprint) => ({
      version: fingerprint.version,
      knownMiscompiles: [...fingerprint.knownMiscompiles],
    }));
  }

  check(compilerVersion: string, patch: string): TcbCheck {
    const fingerprint = this.fingerprints.find((item) => item.version === compilerVersion);
    if (!fingerprint) {
      return { safe: true, compilerVersion, matches: [] };
    }
    const matches = fingerprint.knownMiscompiles.filter((token) => patch.includes(token));
    return { safe: matches.length === 0, compilerVersion, matches };
  }
}

export interface InfraOptimization {
  baselineMfu: number;
  optimizedMfu: number;
  speedup: number;
  targetMfu: number;
}

export class InfraRsi {
  constructor(private readonly targetMfu = 30) {
    if (!Number.isFinite(targetMfu) || targetMfu <= 0) {
      throw new Error("targetMfu must be positive");
    }
  }

  optimize(baselineMfu: number, trialGains: number[]): InfraOptimization {
    if (!Number.isFinite(baselineMfu) || baselineMfu <= 0 || trialGains.length === 0) {
      throw new Error("baselineMfu must be positive and trials must not be empty");
    }
    let best = baselineMfu;
    for (const gain of trialGains) {
      if (!Number.isFinite(gain) || gain < 0) {
        throw new Error("trial gains must be finite and non-negative");
      }
      best = Math.max(best, baselineMfu * (1 + gain));
    }
    const optimized = Math.min(best, this.targetMfu);
    return {
      baselineMfu,
      optimizedMfu: round(optimized),
      speedup: round(optimized / baselineMfu),
      targetMfu: this.targetMfu,
    };
  }
}

function round(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

export const DEFAULT_TCB_FINGERPRINTS: CompilerFingerprint[] = [
  {
    version: "rustc-1.80.0",
    knownMiscompiles: ["miscompile-a", "known-ice-42"],
  },
];

export const INFRA_RSI_TARGET_MFU = 30;
