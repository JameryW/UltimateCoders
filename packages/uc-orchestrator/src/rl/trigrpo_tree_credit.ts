/**
 * TriGRPO tree credit with hindsight sibling expansion (ledger #134).
 *
 * TriGRPO (arXiv:2611.02706) turns a sparse success signal into dense credit
 * by comparing three groups: the prompt (root), the parent/sibling set (node),
 * and the task-family history (cross-task).  This module adds the V29 piece
 * that is easy to miss in a flat trajectory store: when a successful action is
 * found late, it can be replayed as a *sibling* at the matching decision state,
 * giving failed branches a counterfactual comparator instead of only a terminal
 * zero.
 *
 * The module is deterministic and pure: no sampling, rollouts, or training.
 */

import {
	TriGrpoError,
	TriGrpoTree,
} from "./trigrpo_tree";
import type { TriGrpoScore } from "./trigrpo_tree";

/** A trajectory branch with enough metadata to derive token credit. */
export interface CreditBranch {
	readonly id: string;
	readonly promptId: string;
	readonly taskFamily: string;
	readonly parentId: string | null;
	/** Decision state shared by siblings at the same fork. */
	readonly decisionState: string;
	readonly reward: number;
	/** Number of response tokens that receive the advantage. */
	readonly tokenCount: number;
}

/** Error raised by the sibling-expansion credit store. */
export class TriGrpoTreeCreditError extends Error {}

/**
 * A thin credit layer over {@link TriGrpoTree}.
 *
 * It is deliberately a wrapper rather than a second implementation of the
 * three groups: the underlying tree already owns the group statistics and the
 * standardisation rules.
 */
export class TriGrpoTreeCredit {
	private readonly tree = new TriGrpoTree();
	private readonly branches = new Map<string, CreditBranch>();

	/** Add a branch. */
	add(branch: CreditBranch): CreditBranch {
		if (!branch.id.trim()) throw new TriGrpoTreeCreditError("branch id must not be empty");
		if (!branch.decisionState.trim()) {
			throw new TriGrpoTreeCreditError("decision state must not be empty");
		}
		if (!Number.isInteger(branch.tokenCount) || branch.tokenCount <= 0) {
			throw new TriGrpoTreeCreditError("tokenCount must be a positive integer");
		}
		if (this.branches.has(branch.id)) {
			throw new TriGrpoTreeCreditError(`duplicate branch: ${branch.id}`);
		}
		this.tree.add({
			id: branch.id,
			promptId: branch.promptId,
			taskFamily: branch.taskFamily,
			parentId: branch.parentId,
			reward: branch.reward,
		});
		this.branches.set(branch.id, branch);
		return branch;
	}

	/** Number of branches. */
	get size(): number {
		return this.branches.size;
	}

	/** Look up a branch. */
	branch(id: string): CreditBranch | undefined {
		return this.branches.get(id);
	}

	/**
	 * Replay a discovered success as a sibling of an existing branch.
	 *
	 * The new branch shares the source's prompt, family, parent, and decision
	 * state, so it becomes a direct counterfactual comparator at the same fork.
	 */
	addSuccessfulSibling(
		sourceId: string,
		siblingId: string,
		reward: number,
		tokenCount = 1,
	): CreditBranch {
		const source = this.branches.get(sourceId);
		if (!source) throw new TriGrpoTreeCreditError(`unknown source branch: ${sourceId}`);
		if (source.parentId === null) {
			throw new TriGrpoTreeCreditError("top-level branch has no sibling decision state");
		}
		return this.add({
			id: siblingId,
			promptId: source.promptId,
			taskFamily: source.taskFamily,
			parentId: source.parentId,
			decisionState: source.decisionState,
			reward,
			tokenCount,
		});
	}

	/** Three-group advantage for one branch. */
	advantage(id: string): TriGrpoScore {
		if (!this.branches.has(id)) {
			throw new TriGrpoTreeCreditError(`unknown branch: ${id}`);
		}
		return this.tree.advantage(id);
	}

	/** Three-group advantages for every branch. */
	advantages(): Map<string, TriGrpoScore> {
		return this.tree.advantages();
	}

	/**
	 * Distribute a branch's total advantage across response tokens.
	 *
	 * When a mask is supplied, only `true` positions receive credit; the total
	 * over active tokens remains the branch advantage.  This is the hook the
	 * exporter uses to loss-mask low-value segments without losing the
	 * counterfactual signal.
	 */
	tokenCredit(id: string, mask?: readonly boolean[]): number[] {
		const branch = this.branches.get(id);
		if (!branch) throw new TriGrpoTreeCreditError(`unknown branch: ${id}`);
		const total = this.tree.advantage(id).total;
		if (mask === undefined) {
			return Array.from({ length: branch.tokenCount }, () => total / branch.tokenCount);
		}
		if (mask.length === 0) throw new TriGrpoTreeCreditError("mask must not be empty");
		const active = mask.filter(Boolean).length;
		if (active === 0) return mask.map(() => 0);
		return mask.map((keep) => (keep ? total / active : 0));
	}

	/** Highest-reward branch, or null when empty. */
	best(): CreditBranch | null {
		const best = this.tree.best();
		return best ? this.branches.get(best.id) ?? null : null;
	}
}

export { TriGrpoError };
