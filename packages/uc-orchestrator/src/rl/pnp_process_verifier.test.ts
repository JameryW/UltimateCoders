import { describe, expect, test } from "bun:test";

import {
  Applicability,
  NullModelGate,
  PnPProcessVerifier,
  ThreeValuedGate,
} from "./pnp_process_verifier";

const artifact = (id: string, kind: "plan" | "patch" | "test" | "trace" = "patch", verified = true) => ({
  id,
  kind,
  payload: "evidence",
  verified,
});

describe("PnP process reward", () => {
  test("aggregates only verified artifacts", () => {
    const reward = new PnPProcessVerifier().aggregate([artifact("a"), artifact("b", "test", false)]);
    expect(reward.verifiedCount).toBe(1);
    expect(reward.components.patch).toBe(0.4);
    expect(reward.total).toBe(0.4);
  });

  test("supports all four artifact kinds", () => {
    const verifier = new PnPProcessVerifier({ plan: 0.25, patch: 0.25, test: 0.25, trace: 0.25 });
    const reward = verifier.aggregate([
      artifact("p", "plan"),
      artifact("d", "patch"),
      artifact("t", "test"),
      artifact("r", "trace"),
    ]);
    expect(reward.total).toBe(1);
    expect(reward.verifiedCount).toBe(4);
  });

  test("rejects empty verified payloads", () => {
    expect(() =>
      new PnPProcessVerifier().aggregate([{ id: "a", kind: "patch", payload: "", verified: true }]),
    ).toThrow("payload");
  });

  test("rejects negative weights", () => {
    expect(() => new PnPProcessVerifier({ patch: -1 })).toThrow("weights");
  });

  test("returns zero for all-zero weights", () => {
    const verifier = new PnPProcessVerifier({ plan: 0, patch: 0, test: 0, trace: 0 });
    expect(verifier.aggregate([artifact("a")]).total).toBe(0);
  });
});

describe("three-valued preconditions", () => {
  test("is applicable when all facts match", () => {
    const result = new ThreeValuedGate().evaluate(
      { id: "p", preconditions: [{ key: "repo", expected: "core" }] },
      { repo: "core" },
    );
    expect(result.applicability).toBe(Applicability.APPLICABLE);
  });

  test("reports unknown when a fact is absent", () => {
    const result = new ThreeValuedGate().evaluate(
      { id: "p", preconditions: [{ key: "repo", expected: "core" }] },
      {},
    );
    expect(result.applicability).toBe(Applicability.UNKNOWN);
    expect(result.missing).toEqual(["repo"]);
  });

  test("is not applicable when a fact mismatches", () => {
    const result = new ThreeValuedGate().evaluate(
      { id: "p", preconditions: [{ key: "repo", expected: "core" }] },
      { repo: "other" },
    );
    expect(result.applicability).toBe(Applicability.NOT_APPLICABLE);
    expect(result.mismatched).toEqual(["repo"]);
  });

  test("rejects an empty policy id", () => {
    expect(() => new ThreeValuedGate().evaluate({ id: "", preconditions: [] }, {})).toThrow("id");
  });
});

describe("null-model release gate", () => {
  test("flags a pseudo repair below the delta", () => {
    const result = new NullModelGate(0.1).evaluate(0.8, 0.85);
    expect(result.pseudoRepair).toBe(true);
    expect(result.delta).toBe(0.05);
  });

  test("accepts a genuine improvement", () => {
    expect(new NullModelGate(0.1).evaluate(0.5, 0.7).pseudoRepair).toBe(false);
  });

  test("rejects invalid delta", () => {
    expect(() => new NullModelGate(-1)).toThrow("minDelta");
  });
});
