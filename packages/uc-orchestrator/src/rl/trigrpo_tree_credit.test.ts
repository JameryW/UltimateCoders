/**
 * Tests for TriGRPO tree credit with sibling expansion (#134).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { TriGrpoTreeCredit, TriGrpoTreeCreditError } from "./trigrpo_tree_credit";
import type { CreditBranch } from "./trigrpo_tree_credit";

function branch(
	overrides: Partial<CreditBranch> & Pick<CreditBranch, "id" | "parentId" | "reward">,
): CreditBranch {
	return {
		promptId: "prompt-1",
		taskFamily: "repair",
		decisionState: "locate",
		tokenCount: 2,
		...overrides,
	};
}

function fixture(): TriGrpoTreeCredit {
	const tree = new TriGrpoTreeCredit();
	tree.add(branch({ id: "root", parentId: null, reward: 0.0, decisionState: "issue" }));
	tree.add(branch({ id: "a", parentId: "root", reward: 0.2, tokenCount: 3 }));
	tree.add(branch({ id: "b", parentId: "root", reward: 0.8, tokenCount: 2 }));
	tree.add(
		branch({
			id: "feature",
			promptId: "prompt-2",
			taskFamily: "feature",
			parentId: null,
			reward: 1.0,
			decisionState: "issue",
		}),
	);
	return tree;
}

describe("TriGrpoTreeCredit", () => {
	it("adds branches and reports the count", () => {
		const tree = fixture();
		assert.equal(tree.size, 4);
		assert.equal(tree.branch("a")?.reward, 0.2);
	});

	it("replays a discovered success as a sibling at the same fork", () => {
		const tree = fixture();
		const sibling = tree.addSuccessfulSibling("a", "a-success", 0.8, 4);
		assert.equal(sibling.parentId, "root");
		assert.equal(sibling.promptId, "prompt-1");
		assert.equal(sibling.taskFamily, "repair");
		assert.equal(sibling.decisionState, "locate");
		assert.equal(tree.size, 5);
		assert.equal(tree.advantage("a-success").root, tree.advantage("b").root);
	});

	it("a successful sibling changes the node-group stats", () => {
		const tree = fixture();
		const before = tree.advantage("a").node;
		tree.addSuccessfulSibling("a", "a-success", 0.8, 2);
		const after = tree.advantage("a").node;
		assert.notEqual(before, after);
		assert.ok(after < before, "a low branch is further below the sibling mean");
	});

	it("token credit sums to the full branch advantage", () => {
		const tree = fixture();
		const total = tree.advantage("b").total;
		const credit = tree.tokenCredit("b");
		assert.equal(credit.length, 2);
		assert.ok(Math.abs(credit.reduce((sum, value) => sum + value, 0) - total) < 1e-12);
	});

	it("loss mask keeps the total across active tokens and zeroes the rest", () => {
		const tree = fixture();
		const total = tree.advantage("a").total;
		const credit = tree.tokenCredit("a", [true, false, true]);
		assert.equal(credit[1], 0);
		assert.ok(Math.abs(credit[0] + credit[2] - total) < 1e-12);
	});

	it("an all-false mask yields no credit", () => {
		const tree = fixture();
		assert.deepEqual(tree.tokenCredit("a", [false, false, false]), [0, 0, 0]);
	});

	it("rejects a top-level source because it has no sibling decision state", () => {
		const tree = fixture();
		assert.throws(
			() => tree.addSuccessfulSibling("root", "other", 0.5),
			TriGrpoTreeCreditError,
		);
	});

	it("rejects unknown sources and duplicate ids", () => {
		const tree = fixture();
		assert.throws(() => tree.addSuccessfulSibling("missing", "x", 0.5), /unknown source/);
		assert.throws(
			() => tree.add(branch({ id: "a", parentId: "root", reward: 0.1 })),
			/duplicate branch/,
		);
	});

	it("rejects invalid tokens and empty decision state", () => {
		const tree = new TriGrpoTreeCredit();
		assert.throws(
			() => tree.add(branch({ id: "x", parentId: null, reward: 0, tokenCount: 0 })),
			/tokenCount/,
		);
		assert.throws(
			() => tree.add(branch({ id: "y", parentId: null, reward: 0, decisionState: " " })),
			/decision state/,
		);
	});

	it("exposes best branch and advantages for every branch", () => {
		const tree = fixture();
		assert.equal(tree.best()?.id, "feature");
		assert.equal(tree.advantages().size, 4);
		assert.throws(() => tree.advantage("missing"), /unknown branch/);
	});
});
