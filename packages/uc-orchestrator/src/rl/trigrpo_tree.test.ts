/**
 * Tests for TriGRPO tree credit (#118).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
	CROSS_TASK_WEIGHT,
	NODE_GROUP_WEIGHT,
	ROOT_GROUP_WEIGHT,
	TriGrpoError,
	TriGrpoTree,
} from "./trigrpo_tree";
import type { TrajectoryNode } from "./trigrpo_tree";

function node(overrides: Partial<TrajectoryNode> & Pick<TrajectoryNode, "id">): TrajectoryNode {
	return {
		promptId: "prompt-1",
		taskFamily: "repair",
		parentId: null,
		reward: 0,
		...overrides,
	};
}

describe("TriGRPO tree credit", () => {
	it("weights the three groups as published", () => {
		assert.equal(ROOT_GROUP_WEIGHT, 0.4);
		assert.equal(NODE_GROUP_WEIGHT, 0.4);
		assert.equal(CROSS_TASK_WEIGHT, 0.2);
	});

	it("standardises a root group relative to its prompt", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "a", reward: 1 }));
		tree.add(node({ id: "b", reward: 0 }));
		assert.equal(tree.advantage("a").root, 1);
		assert.equal(tree.advantage("b").root, -1);
	});

	it("uses parent/sibling differences for the node group", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "root", reward: 0.5 }));
		tree.add(node({ id: "good", parentId: "root", reward: 1 }));
		tree.add(node({ id: "bad", parentId: "root", reward: 0 }));
		assert.equal(tree.advantage("good").node, 1);
		assert.equal(tree.advantage("bad").node, -1);
		assert.equal(tree.advantage("root").node, 0);
	});

	it("uses a cross-task family baseline", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "a", taskFamily: "feature", reward: 1 }));
		tree.add(node({ id: "b", taskFamily: "feature", reward: 0 }));
		assert.equal(tree.advantage("a").crossTask, 0.5);
		assert.equal(tree.advantage("b").crossTask, -0.5);
	});

	it("sums the weighted advantages", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "root", reward: 0.5 }));
		tree.add(node({ id: "good", parentId: "root", reward: 1 }));
		tree.add(node({ id: "bad", parentId: "root", reward: 0 }));
		const advantage = tree.advantage("good");
		assert.equal(
			advantage.total,
			ROOT_GROUP_WEIGHT * advantage.root +
				NODE_GROUP_WEIGHT * advantage.node +
				CROSS_TASK_WEIGHT * advantage.crossTask,
		);
	});

	it("returns every node from advantages()", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "a", reward: 1 }));
		tree.add(node({ id: "b", reward: 0 }));
		assert.deepEqual([...tree.advantages().keys()], ["a", "b"]);
	});

	it("tracks children and the best node", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "root", reward: 0.2 }));
		tree.add(node({ id: "child", parentId: "root", reward: 0.9 }));
		assert.deepEqual(tree.childrenOf("root"), ["child"]);
		assert.equal(tree.best()?.id, "child");
		assert.equal(tree.size, 2);
	});

	it("rejects duplicate ids and unknown parents", () => {
		const tree = new TriGrpoTree();
		tree.add(node({ id: "a" }));
		assert.throws(() => tree.add(node({ id: "a" })), TriGrpoError);
		assert.throws(() => tree.add(node({ id: "b", parentId: "missing" })), TriGrpoError);
	});

	it("rejects invalid fields", () => {
		const tree = new TriGrpoTree();
		assert.throws(() => tree.add(node({ id: " ", reward: 1 })), TriGrpoError);
		assert.throws(() => tree.add(node({ id: "x", promptId: " " })), TriGrpoError);
		assert.throws(() => tree.add(node({ id: "x", taskFamily: "" })), TriGrpoError);
		assert.throws(() => tree.add(node({ id: "x", reward: Number.NaN })), TriGrpoError);
	});
});
