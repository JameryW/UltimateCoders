/**
 * RST solution-first quadruple synthesis (#68).
 *
 * Recursive Synthesis for Long-Horizon Terminal Tasks turns a seed task into a
 * tree of increasingly long terminal tasks. The invariant is deliberately
 * simple and deterministic: the verifier must fail on the bare workspace
 * (`V(W) == 0`) and pass after executing the reference solution
 * (`V(Exec(W, S)) == 1`). Every recursive round extends the reference solution
 * first and only then emits the public instruction.
 *
 * The module is pure bookkeeping; executing the solution or running the
 * verifier is the caller's job. Mirroring the ledger, it is a test-time task
 * generator, not a live scheduler.
 */

export interface Quadruple {
	workspace: string;
	solution: string;
	verifier: string;
	instruction: string;
	/** Simulated `V(W) == 1` result. */
	basePass: boolean;
	/** Simulated `V(Exec(W, S)) == 1` result. */
	solutionPass: boolean;
	commandCount: number;
	stepCount: number;
}

export interface Proof {
	solvable: boolean;
	nontrivial: boolean;
	accepted: boolean;
}

export interface SynthesisStep {
	round: number;
	task: Quadruple;
	proof: Proof;
}

export interface SynthesisReport {
	steps: readonly SynthesisStep[];
	accepted: number;
	total: number;
	solutionGrowth: number;
	commandGrowth: number;
	estimatedCostUsd: number;
}

export interface SynthesizerOptions {
	maxRounds?: number;
	costPerTaskUsd?: number;
}

export class RstSynthesisError extends Error {}

export function proveQuadruple(task: Quadruple): Proof {
	const solvable = task.solutionPass;
	const nontrivial = !task.basePass;
	return { solvable, nontrivial, accepted: solvable && nontrivial };
}

export function validateQuadruple(task: Quadruple): void {
	if (!task.workspace.trim() || !task.solution.trim() || !task.verifier.trim()) {
		throw new RstSynthesisError("workspace, solution and verifier must be non-empty");
	}
	if (!task.instruction.trim()) {
		throw new RstSynthesisError("instruction must be non-empty");
	}
	if (task.commandCount < 0 || task.stepCount < 0) {
		throw new RstSynthesisError("command and step counts must be non-negative");
	}
}

export function advanceQuadruple(task: Quadruple, round: number): Quadruple {
	if (round <= 0) {
		throw new RstSynthesisError("round must be positive");
	}
	return {
		workspace: `${task.workspace}\n# workspace evolution ${round}`,
		solution: `${task.solution}\n# solution extension ${round}`,
		verifier: task.verifier,
		instruction: `${task.instruction}\n- requirement ${round}`,
		basePass: false,
		solutionPass: true,
		commandCount: task.commandCount + 1,
		stepCount: task.stepCount + 1,
	};
}

export class RSTQuadrupleSynthesizer {
	private readonly maxRounds: number;
	private readonly costPerTaskUsd: number;
	private readonly steps: SynthesisStep[] = [];

	constructor(seed: Quadruple, options: SynthesizerOptions = {}) {
		validateQuadruple(seed);
		if (!proveQuadruple(seed).accepted) {
			throw new RstSynthesisError("seed must satisfy V(W)=0 and V(Exec(W,S))=1");
		}
		this.maxRounds = options.maxRounds ?? 15;
		this.costPerTaskUsd = options.costPerTaskUsd ?? 0.05;
		if (this.maxRounds <= 0 || this.costPerTaskUsd < 0) {
			throw new RstSynthesisError("invalid synthesizer options");
		}
		this.seed = seed;
	}

	private readonly seed: Quadruple;

	step(): SynthesisStep {
		if (this.steps.length >= this.maxRounds) {
			throw new RstSynthesisError("maximum recursion depth reached");
		}
		const previous = this.steps.at(-1)?.task ?? this.seed;
		const round = this.steps.length + 1;
		const task = advanceQuadruple(previous, round);
		const proof = proveQuadruple(task);
		if (!proof.accepted) {
			throw new RstSynthesisError("recursive task lost its solvability proof");
		}
		const step = { round, task, proof };
		this.steps.push(step);
		return step;
	}

	generate(rounds = this.maxRounds): SynthesisReport {
		if (rounds <= 0 || rounds > this.maxRounds) {
			throw new RstSynthesisError("rounds must be within 1..maxRounds");
		}
		while (this.steps.length < rounds) {
			this.step();
		}
		const steps = this.steps.slice(0, rounds);
		const last = steps.at(-1)?.task ?? this.seed;
		return {
			steps,
			accepted: steps.filter((step) => step.proof.accepted).length,
			total: steps.length,
			solutionGrowth: last.solution.length / Math.max(1, this.seed.solution.length),
			commandGrowth: last.commandCount / Math.max(1, this.seed.commandCount),
			estimatedCostUsd: steps.length * this.costPerTaskUsd,
		};
	}

	reset(): void {
		this.steps.length = 0;
	}
}
