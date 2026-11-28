/**
 * Plug-and-Play process verifier + TactiNavi three-valued release gate
 * (ledger item #113, UC_PNP_PROCESS_VERIFIER).
 *
 * Pure TypeScript helpers: aggregate independently verifiable intermediate
 * artifacts into a bounded dense process reward, and evaluate policy
 * preconditions with an explicit UNKNOWN state.
 */

export type ArtifactKind = "plan" | "patch" | "test" | "trace";

export interface ProcessArtifact {
  id: string;
  kind: ArtifactKind;
  payload: string;
  verified: boolean;
  weight?: number;
}

export interface ProcessReward {
  total: number;
  components: Record<ArtifactKind, number>;
  verifiedCount: number;
}

const DEFAULT_WEIGHTS: Record<ArtifactKind, number> = {
  plan: 0.2,
  patch: 0.4,
  test: 0.25,
  trace: 0.15,
};

export class PnPProcessVerifier {
  private readonly weights: Record<ArtifactKind, number>;

  constructor(weights: Partial<Record<ArtifactKind, number>> = {}) {
    this.weights = { ...DEFAULT_WEIGHTS, ...weights };
    for (const value of Object.values(this.weights)) {
      if (!Number.isFinite(value) || value < 0) {
        throw new Error("weights must be finite and non-negative");
      }
    }
  }

  aggregate(artifacts: ProcessArtifact[]): ProcessReward {
    const components = { plan: 0, patch: 0, test: 0, trace: 0 } as Record<ArtifactKind, number>;
    const totals = { plan: 0, patch: 0, test: 0, trace: 0 } as Record<ArtifactKind, number>;
    let verifiedCount = 0;
    for (const artifact of artifacts) {
      if (!artifact.id.trim()) {
        throw new Error("artifact id must not be empty");
      }
      if (!artifact.verified) {
        continue;
      }
      if (!artifact.payload.trim()) {
        throw new Error("verified artifact payload must not be empty");
      }
      const weight = artifact.weight ?? this.weights[artifact.kind];
      if (!Number.isFinite(weight) || weight < 0) {
        throw new Error("artifact weight must be finite and non-negative");
      }
      components[artifact.kind] += weight;
      totals[artifact.kind] += weight;
      verifiedCount += 1;
    }
    const denominator = Object.values(this.weights).reduce((sum, value) => sum + value, 0);
    const total = denominator === 0 ? 0 : Object.values(totals).reduce((sum, value) => sum + value, 0) / denominator;
    return { total: round(total), components, verifiedCount };
  }
}

export enum TriValue {
  TRUE = "true",
  FALSE = "false",
  UNKNOWN = "unknown",
}

export interface Precondition {
  key: string;
  expected: string;
}

export interface PolicyPackage {
  id: string;
  preconditions: Precondition[];
}

export enum Applicability {
  APPLICABLE = "applicable",
  NOT_APPLICABLE = "not_applicable",
  UNKNOWN = "unknown",
}

export interface GateResult {
  applicability: Applicability;
  missing: string[];
  mismatched: string[];
}

export class ThreeValuedGate {
  evaluate(pkg: PolicyPackage, context: Record<string, string | undefined>): GateResult {
    if (!pkg.id.trim()) {
      throw new Error("policy package id must not be empty");
    }
    const missing: string[] = [];
    const mismatched: string[] = [];
    for (const precondition of pkg.preconditions) {
      if (!precondition.key.trim()) {
        throw new Error("precondition key must not be empty");
      }
      const actual = context[precondition.key];
      if (actual === undefined) {
        missing.push(precondition.key);
      } else if (actual !== precondition.expected) {
        mismatched.push(precondition.key);
      }
    }
    if (missing.length > 0) {
      return { applicability: Applicability.UNKNOWN, missing, mismatched };
    }
    if (mismatched.length > 0) {
      return { applicability: Applicability.NOT_APPLICABLE, missing, mismatched };
    }
    return { applicability: Applicability.APPLICABLE, missing, mismatched };
  }
}

export interface NullModelResult {
  pseudoRepair: boolean;
  baseline: number;
  candidate: number;
  delta: number;
}

export class NullModelGate {
  constructor(private readonly minDelta = 0.1) {
    if (!Number.isFinite(minDelta) || minDelta < 0) {
      throw new Error("minDelta must be finite and non-negative");
    }
  }

  evaluate(baseline: number, candidate: number): NullModelResult {
    if (!Number.isFinite(baseline) || !Number.isFinite(candidate)) {
      throw new Error("scores must be finite");
    }
    const delta = round(candidate - baseline);
    return {
      pseudoRepair: delta < this.minDelta,
      baseline,
      candidate,
      delta,
    };
  }
}

function round(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

export const PNP_DEFAULT_WEIGHTS = DEFAULT_WEIGHTS;
