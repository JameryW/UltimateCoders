import { describe, expect, test } from "bun:test";

import {
  SwarmAction,
  S3Swarm,
  SwarmError,
  chooseAction,
  dedupeMetrics,
} from "./s3_swarm_rl";

describe("s3 swarm", () => {
  test("chooseAction prioritises inbox, then broadcast, then solo work", () => {
    expect(chooseAction({ agentId: "a", unread: 1, hasFinding: true, soloSteps: 0 })).toBe(SwarmAction.ReadInbox);
    expect(chooseAction({ agentId: "a", unread: 0, hasFinding: true, soloSteps: 0 })).toBe(SwarmAction.BroadcastFinding);
    expect(chooseAction({ agentId: "a", unread: 0, hasFinding: false, soloSteps: 0 })).toBe(SwarmAction.WorkSolo);
  });

  test("dedupeMetrics rewards unique coverage", () => {
    const metrics = dedupeMetrics(new Map([
      ["a", ["x", "y"]],
      ["b", ["x", "z"]],
    ]));
    expect(metrics.uniqueFindings).toBe(3);
    expect(metrics.totalFindings).toBe(4);
    expect(metrics.duplicateRate).toBe(0.25);
    expect(metrics.coverage).toBe(0.75);
    expect(metrics.reward).toBe(0.6 * 0.75 + 0.4 * 0.75);
  });

  test("empty findings produce zero coverage", () => {
    const metrics = dedupeMetrics(new Map([["a", []]]));
    expect(metrics.coverage).toBe(0);
    expect(metrics.reward).toBe(0.4);
  });

  test("constructor rejects empty and duplicate agents", () => {
    expect(() => new S3Swarm([])).toThrow(SwarmError);
    expect(() => new S3Swarm(["a", "a"])).toThrow(SwarmError);
  });

  test("broadcast deposits findings in every peer inbox", () => {
    const swarm = new S3Swarm(["a", "b"]);
    const event = swarm.step("a", "bug in cache");
    expect(event.action).toBe(SwarmAction.BroadcastFinding);
    expect(swarm.snapshot("b").inbox).toEqual(["bug in cache"]);
  });

  test("reading the inbox clears it and takes priority", () => {
    const swarm = new S3Swarm(["a", "b"]);
    swarm.step("a", "finding");
    const event = swarm.step("b", "ignored");
    expect(event.action).toBe(SwarmAction.ReadInbox);
    expect(swarm.snapshot("b").inbox).toEqual([]);
  });

  test("unknown agent is rejected", () => {
    expect(() => new S3Swarm(["a"]).step("b", "x")).toThrow(SwarmError);
  });

  test("runRound reports metrics and reset clears state", () => {
    const swarm = new S3Swarm(["a", "b"]);
    const metrics = swarm.runRound({ a: ["one", "two"], b: ["two"] });
    // b reads its inbox before it can broadcast its own discovery, so only a
    // contributes findings during this round.
    expect(metrics.totalFindings).toBe(2);
    expect(metrics.uniqueFindings).toBe(2);
    swarm.reset();
    expect(swarm.metrics().totalFindings).toBe(0);
  });

  test("snapshot findings are sorted", () => {
    const swarm = new S3Swarm(["a"]);
    swarm.step("a", "z");
    swarm.step("a", "a");
    expect(swarm.snapshot("a").findings).toEqual(["a", "z"]);
  });

  test("solo work increments soloSteps", () => {
    const swarm = new S3Swarm(["a"]);
    swarm.step("a");
    expect(swarm.snapshot("a").soloSteps).toBe(1);
  });
});
