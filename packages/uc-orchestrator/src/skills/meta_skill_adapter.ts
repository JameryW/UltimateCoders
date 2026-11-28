/**
 * Meta-Skill adapter for executor-capability matching.
 *
 * Implements ledger item #58 (Skill Adaptation, arXiv:2610.33024). A skill that
 * helps a flagship model can hurt a smaller executor: the prose is correct but
 * the executor cannot turn it into tool calls, context decisions, or a final
 * verification step. The adapter keeps the skill's intent and adds an explicit
 * execution contract only where the target tier needs it.
 */

export type ExecutorTier = "flagship" | "mid" | "small";

export type SkillObstacle =
	| "tool_execution"
	| "context_management"
	| "instruction_following"
	| "verification";

export interface SkillDocument {
	readonly name: string;
	readonly body: string;
}

export interface AdaptedSkill {
	readonly name: string;
	readonly body: string;
	readonly tier: ExecutorTier;
	readonly obstacles: readonly SkillObstacle[];
}

const CONTRACT_MARKER = "## Executor-specific execution contract";

const OBSTACLE_ORDER: readonly SkillObstacle[] = [
	"tool_execution",
	"context_management",
	"instruction_following",
	"verification",
];

/**
 * Select the obstacles that are worth making explicit for a tier. Flagship
 * executors receive no contract; mid-tier executors receive the three
 * execution-facing sections; small executors receive all four.
 */
export function obstaclesForTier(tier: ExecutorTier): readonly SkillObstacle[] {
	if (tier === "flagship") {
		return [];
	}
	if (tier === "mid") {
		return [
			"tool_execution",
			"context_management",
			"instruction_following",
		];
	}
	return OBSTACLE_ORDER;
}

function toolExecutionSection(tier: ExecutorTier): string {
	const detail =
		tier === "small"
			? [
					"1. Call exactly one tool per step.",
					"2. Use the tool's declared argument names; do not invent fields.",
					"3. After every call, read the returned path, exit code, and error text before deciding the next call.",
					"4. If a tool call fails twice with the same error, stop retrying and inspect the input.",
				].join("\n")
			: [
					"1. Use one tool call per step with the declared argument names.",
					"2. Read the returned exit code and error text before the next call.",
					"3. Do not retry the same failing call without changing the input.",
				].join("\n");
	return `### Tool execution\n${detail}`;
}

function contextManagementSection(tier: ExecutorTier): string {
	const detail =
		tier === "small"
			? [
					"1. Keep the active goal, the files already read, and the next action in view.",
					"2. Do not reread a file whose content has not changed.",
					"3. If the context is above 70%, write a short working-state summary before continuing.",
					"4. Prefer the smallest relevant file range over a whole-repository read.",
				].join("\n")
			: [
					"1. Track the active goal and the files already read.",
					"2. Avoid rereading unchanged files.",
					"3. Summarize the working state before the context budget is exhausted.",
				].join("\n");
	return `### Context management\n${detail}`;
}

function instructionFollowingSection(tier: ExecutorTier): string {
	const detail =
		tier === "small"
			? [
					"1. Number the remaining steps before acting.",
					"2. Execute the steps in order; do not skip a step because it looks obvious.",
					"3. Mark a step complete only after its tool evidence exists.",
					"4. If the request changes, stop and restate the new acceptance condition.",
				].join("\n")
			: [
					"1. Execute the requested steps in order.",
					"2. Do not skip a step without recording why.",
					"3. Mark a step complete only with tool evidence.",
				].join("\n");
	return `### Instruction following\n${detail}`;
}

function verificationSection(tier: ExecutorTier): string {
	const detail =
		tier === "small"
			? [
					"1. Run the narrowest relevant test after the final edit.",
					"2. Run the regression suite before declaring completion.",
					"3. Report the exact command, exit code, and failing test name.",
					"4. Do not claim success from an unexecuted test or a test you wrote but never ran.",
				].join("\n")
			: [
					"1. Run the narrowest relevant test after the final edit.",
					"2. Run the regression suite before declaring completion.",
					"3. Report the command and exit code as evidence.",
				].join("\n");
	return `### Verification\n${detail}`;
}

function sectionFor(obstacle: SkillObstacle, tier: ExecutorTier): string {
	switch (obstacle) {
		case "tool_execution":
			return toolExecutionSection(tier);
		case "context_management":
			return contextManagementSection(tier);
		case "instruction_following":
			return instructionFollowingSection(tier);
		case "verification":
			return verificationSection(tier);
	}
}

export class MetaSkillAdapter {
	/**
	 * Rewrite a skill for the target tier. Re-adapting an already adapted skill
	 * is idempotent: the existing contract block is replaced, never duplicated.
	 */
	rewrite(skill: SkillDocument, tier: ExecutorTier): AdaptedSkill {
		if (!skill.name.trim()) {
			throw new Error("skill name must not be empty");
		}
		if (!skill.body.trim()) {
			throw new Error("skill body must not be empty");
		}
		const obstacles = obstaclesForTier(tier);
		if (obstacles.length === 0) {
			return {
				name: skill.name,
				body: skill.body,
				tier,
				obstacles: [],
			};
		}
		const base = stripExistingContract(skill.body);
		const sections = obstacles.map((obstacle) =>
			sectionFor(obstacle, tier),
		);
		return {
			name: skill.name,
			body: [
				base,
				CONTRACT_MARKER,
				`Target executor tier: ${tier}. These are executable requirements, not background advice.`,
				...sections,
			].join("\n\n"),
			tier,
			obstacles,
		};
	}

	adaptMany(
		skills: readonly SkillDocument[],
		tier: ExecutorTier,
	): readonly AdaptedSkill[] {
		return skills.map((skill) => this.rewrite(skill, tier));
	}
}

function stripExistingContract(body: string): string {
	const markerIndex = body.indexOf(CONTRACT_MARKER);
	if (markerIndex < 0) {
		return body.trimEnd();
	}
	return body.slice(0, markerIndex).trimEnd();
}
