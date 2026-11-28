/**
 * HarnessFlow MCTS (ledger item #115).
 *
 * HarnessFlow (arXiv:2611.06254) treats a harness as a complete executable
 * artifact and searches a refinement tree with UCT under a small evaluation
 * budget.  The coding agent rewrites the harness using archived code,
 * execution feedback and a reusable mechanism library; MCTS balances trying
 * promising refinements against exploring alternatives.
 *
 * This module is a pure, deterministic search skeleton.  The caller supplies
 * the branch generator and the evaluator; nothing here invokes a model, a
 * shell, or the filesystem.
 */

export interface FlowNode {
  readonly id: string;
  readonly harness: string;
  readonly parentId: string | null;
  visits: number;
  value: number;
  score: number | null;
  readonly untried: string[];
}

export interface HarnessFlowReport {
  readonly best: string;
  readonly bestScore: number;
  readonly iterations: number;
  readonly evaluations: number;
  readonly nodes: readonly FlowNode[];
}

export type HarnessBranch = (harness: string) => readonly string[];
export type HarnessEvaluator = (harness: string) => number;

export class HarnessFlowError extends Error {}

const DEFAULT_EXPLORATION = 1.414;
const DEFAULT_MAX_NODES = 64;

export class HarnessFlowMcts {
  private readonly branch: HarnessBranch;
  private readonly evaluate: HarnessEvaluator;
  private readonly exploration: number;
  private readonly maxNodes: number;

  constructor(
    branch: HarnessBranch,
    evaluate: HarnessEvaluator,
    exploration = DEFAULT_EXPLORATION,
    maxNodes = DEFAULT_MAX_NODES,
  ) {
    if (!Number.isFinite(exploration) || exploration < 0) {
      throw new HarnessFlowError("exploration must be finite and non-negative");
    }
    if (!Number.isInteger(maxNodes) || maxNodes < 2) {
      throw new HarnessFlowError("maxNodes must be an integer >= 2");
    }
    this.branch = branch;
    this.evaluate = evaluate;
    this.exploration = exploration;
    this.maxNodes = maxNodes;
  }

  search(rootHarness: string, iterations = 15): HarnessFlowReport {
    if (!rootHarness.trim()) {
      throw new HarnessFlowError("root harness must not be empty");
    }
    if (!Number.isInteger(iterations) || iterations < 1) {
      throw new HarnessFlowError("iterations must be a positive integer");
    }
    const nodes = new Map<string, FlowNode>();
    const root = this.makeNode("node-0", rootHarness, null);
    root.score = this.scoreOf(rootHarness);
    nodes.set(root.id, root);
    let sequence = 1;
    let evaluations = 1;

    while (evaluations < iterations) {
      const parent = this.selectExpandable(nodes, evaluations);
      if (!parent || nodes.size >= this.maxNodes) break;
      const harness = this.nextCandidate(parent, nodes);
      if (harness === null) continue;
      const child = this.makeNode(`node-${sequence}`, harness, parent.id);
      sequence += 1;
      child.score = this.scoreOf(harness);
      evaluations += 1;
      nodes.set(child.id, child);
      this.backpropagate(nodes, child.id, child.score);
    }

    let best = root;
    for (const node of nodes.values()) {
      if (node.score === null) continue;
      if (
        node.score > (best.score ?? Number.NEGATIVE_INFINITY) ||
        (node.score === best.score && node.harness < best.harness)
      ) {
        best = node;
      }
    }
    return {
      best: best.harness,
      bestScore: best.score ?? 0,
      iterations: evaluations,
      evaluations,
      nodes: [...nodes.values()],
    };
  }

  private makeNode(id: string, harness: string, parentId: string | null): FlowNode {
    return {
      id,
      harness,
      parentId,
      visits: 0,
      value: 0,
      score: null,
      untried: [...this.branch(harness)],
    };
  }

  private selectExpandable(nodes: Map<string, FlowNode>, evaluations: number): FlowNode | null {
    let best: FlowNode | null = null;
    let bestUcb = Number.NEGATIVE_INFINITY;
    for (const node of nodes.values()) {
      if (node.untried.length === 0) continue;
      const ucb =
        node.value +
        this.exploration * Math.sqrt(Math.log(Math.max(1, evaluations)) / Math.max(1, node.visits));
      if (ucb > bestUcb || (ucb === bestUcb && (best === null || node.id < best.id))) {
        best = node;
        bestUcb = ucb;
      }
    }
    return best;
  }

  private nextCandidate(parent: FlowNode, nodes: Map<string, FlowNode>): string | null {
    const seen = new Set<string>();
    for (const node of nodes.values()) seen.add(node.harness);
    while (parent.untried.length > 0) {
      const candidate = parent.untried.shift()!;
      if (candidate.trim() && !seen.has(candidate)) return candidate;
    }
    return null;
  }

  private scoreOf(harness: string): number {
    const score = this.evaluate(harness);
    if (!Number.isFinite(score) || score < 0 || score > 1) {
      throw new HarnessFlowError("evaluator must return a finite score in [0, 1]");
    }
    return score;
  }

  private backpropagate(nodes: Map<string, FlowNode>, startId: string, score: number): void {
    let current: string | null = startId;
    while (current !== null) {
      const node = nodes.get(current);
      if (!node) return;
      node.visits += 1;
      node.value = (node.value * (node.visits - 1) + score) / node.visits;
      current = node.parentId;
    }
  }
}
