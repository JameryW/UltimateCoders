/**
 * TriGRPO hindsight credit over trees (ledger item #118).
 *
 * TriGRPO (arXiv:2611.02706) turns sparse terminal rewards into dense credit
 * by combining three complementary groups:
 *
 * - root group: relative return inside the same prompt;
 * - node group: parent/sibling outcome differences in a rolling tree;
 * - cross-task group: a historical baseline for the relevant task family.
 *
 * The module is a deterministic tree store and advantage calculator.  It does
 * not sample, roll out, or train a model.
 */

export interface TrajectoryNode {
  readonly id: string;
  readonly promptId: string;
  readonly taskFamily: string;
  readonly parentId: string | null;
  readonly reward: number;
}

export interface TriGrpoScore {
  readonly root: number;
  readonly node: number;
  readonly crossTask: number;
  readonly total: number;
}

export class TriGrpoError extends Error {}

export const ROOT_GROUP_WEIGHT = 0.4;
export const NODE_GROUP_WEIGHT = 0.4;
export const CROSS_TASK_WEIGHT = 0.2;

interface GroupStats {
  readonly mean: number;
  readonly std: number;
}

function stats(values: readonly number[]): GroupStats {
  if (values.length === 0) return { mean: 0, std: 0 };
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance =
    values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length;
  return { mean, std: Math.sqrt(variance) };
}

function standardised(value: number, group: GroupStats): number {
  if (group.std <= 1e-12) return 0;
  return (value - group.mean) / group.std;
}

export class TriGrpoTree {
  private readonly nodes = new Map<string, TrajectoryNode>();
  private readonly children = new Map<string, string[]>();

  add(node: TrajectoryNode): void {
    if (!node.id.trim()) throw new TriGrpoError("node id must not be empty");
    if (!node.promptId.trim()) throw new TriGrpoError("prompt id must not be empty");
    if (!node.taskFamily.trim()) throw new TriGrpoError("task family must not be empty");
    if (!Number.isFinite(node.reward)) throw new TriGrpoError("reward must be finite");
    if (this.nodes.has(node.id)) throw new TriGrpoError(`duplicate node: ${node.id}`);
    if (node.parentId !== null) {
      if (!node.parentId.trim()) throw new TriGrpoError("parent id must not be empty");
      if (!this.nodes.has(node.parentId)) throw new TriGrpoError(`unknown parent: ${node.parentId}`);
      if (this.wouldCycle(node.id, node.parentId)) throw new TriGrpoError("parent cycle");
    }
    this.nodes.set(node.id, node);
    if (node.parentId !== null) {
      const siblings = this.children.get(node.parentId) ?? [];
      siblings.push(node.id);
      this.children.set(node.parentId, siblings);
    }
  }

  get size(): number {
    return this.nodes.size;
  }

  node(id: string): TrajectoryNode | undefined {
    return this.nodes.get(id);
  }

  all(): readonly TrajectoryNode[] {
    return [...this.nodes.values()];
  }

  childrenOf(id: string): readonly string[] {
    return this.children.get(id) ?? [];
  }

  rootScores(): Map<string, GroupStats> {
    const groups = new Map<string, number[]>();
    for (const node of this.nodes.values()) {
      const values = groups.get(node.promptId) ?? [];
      values.push(node.reward);
      groups.set(node.promptId, values);
    }
    const result = new Map<string, GroupStats>();
    for (const [promptId, values] of groups) result.set(promptId, stats(values));
    return result;
  }

  nodeScores(): Map<string, GroupStats> {
    const groups = new Map<string, number[]>();
    for (const node of this.nodes.values()) {
      if (node.parentId === null) continue;
      const values = groups.get(node.parentId) ?? [];
      values.push(node.reward);
      groups.set(node.parentId, values);
    }
    const result = new Map<string, GroupStats>();
    for (const [parentId, values] of groups) result.set(parentId, stats(values));
    return result;
  }

  crossTaskScores(): Map<string, GroupStats> {
    const groups = new Map<string, number[]>();
    for (const node of this.nodes.values()) {
      const values = groups.get(node.taskFamily) ?? [];
      values.push(node.reward);
      groups.set(node.taskFamily, values);
    }
    const result = new Map<string, GroupStats>();
    for (const [family, values] of groups) result.set(family, stats(values));
    return result;
  }

  advantage(id: string): TriGrpoScore {
    const node = this.nodes.get(id);
    if (!node) throw new TriGrpoError(`unknown node: ${id}`);
    const root = standardised(node.reward, this.rootScores().get(node.promptId)!);
    const nodeGroup =
      node.parentId === null
        ? 0
        : standardised(node.reward, this.nodeScores().get(node.parentId)!);
    const cross = node.reward - this.crossTaskScores().get(node.taskFamily)!.mean;
    return {
      root,
      node: nodeGroup,
      crossTask: cross,
      total: ROOT_GROUP_WEIGHT * root + NODE_GROUP_WEIGHT * nodeGroup + CROSS_TASK_WEIGHT * cross,
    };
  }

  advantages(): Map<string, TriGrpoScore> {
    const root = this.rootScores();
    const nodeGroup = this.nodeScores();
    const cross = this.crossTaskScores();
    const result = new Map<string, TriGrpoScore>();
    for (const node of this.nodes.values()) {
      const r = standardised(node.reward, root.get(node.promptId)!);
      const n =
        node.parentId === null ? 0 : standardised(node.reward, nodeGroup.get(node.parentId)!);
      const c = node.reward - cross.get(node.taskFamily)!.mean;
      result.set(node.id, {
        root: r,
        node: n,
        crossTask: c,
        total: ROOT_GROUP_WEIGHT * r + NODE_GROUP_WEIGHT * n + CROSS_TASK_WEIGHT * c,
      });
    }
    return result;
  }

  best(): TrajectoryNode | null {
    let winner: TrajectoryNode | null = null;
    for (const node of this.nodes.values()) {
      if (!winner || node.reward > winner.reward) winner = node;
    }
    return winner;
  }

  private wouldCycle(candidateId: string, parentId: string): boolean {
    let current: string | null = parentId;
    while (current !== null) {
      if (current === candidateId) return true;
      current = this.nodes.get(current)?.parentId ?? null;
    }
    return false;
  }
}
