/**
 * Behavioral trajectory fingerprinting and orthogonal skill/memory evaluation
 * (ledger item #84). The fingerprint deliberately ignores outcomes, token
 * counts and tool names; only the search/edit/test phase transitions matter.
 */

export type BehavioralPhase = "search" | "edit" | "test";

export interface BehaviorEvent {
	phase: BehavioralPhase;
}

export interface BehaviorProfile {
	readonly searches: number;
	readonly edits: number;
	readonly tests: number;
	readonly transitions: string;
	readonly fingerprint: string;
}

export function behaviorProfile(events: readonly BehaviorEvent[]): BehaviorProfile {
	let searches = 0;
	let edits = 0;
	let tests = 0;
	let transitions = "";
	for (const event of events) {
		if (event.phase === "search") searches += 1;
		else if (event.phase === "edit") edits += 1;
		else tests += 1;
		transitions += event.phase[0]!.toUpperCase();
	}
	return {
		searches,
		edits,
		tests,
		transitions,
		fingerprint: fnv1a(`${searches}:${edits}:${tests}:${transitions}`),
	};
}

export function compareProfiles(a: BehaviorProfile, b: BehaviorProfile): number {
	const left = `${a.searches}:${a.edits}:${a.tests}:${a.transitions}`;
	const right = `${b.searches}:${b.edits}:${b.tests}:${b.transitions}`;
	if (left === right) return 1;
	const maxLen = Math.max(left.length, right.length);
	let same = 0;
	for (let index = 0; index < maxLen; index += 1) {
		if (left[index] === right[index]) same += 1;
	}
	return same / maxLen;
}

export function fnv1a(value: string): string {
	let hash = 0x811c9dc5;
	for (let index = 0; index < value.length; index += 1) {
		hash ^= value.charCodeAt(index);
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return hash.toString(16).padStart(8, "0");
}

export interface RepoView {
	readonly path: string;
	readonly symbols: readonly string[];
	readonly dependencies: readonly string[];
}

export class CodeNibContext {
	private readonly views = new Map<string, RepoView>();

	addView(view: RepoView): void {
		if (!view.path.trim()) throw new Error("view path must not be empty");
		this.views.set(view.path, view);
	}

	view(path: string): RepoView | undefined {
		return this.views.get(path);
	}

	render(paths: readonly string[]): string {
		const parts: string[] = [];
		for (const path of paths) {
			const view = this.views.get(path);
			if (!view) continue;
			parts.push(
				`${view.path}: symbols=${view.symbols.join(",")} deps=${view.dependencies.join(",")}`,
			);
		}
		return parts.join("\n");
	}

	get size(): number {
		return this.views.size;
	}
}

export type SkillLevel = "none" | "curated" | "auto";
export type MemoryMode = "off" | "on";

export interface BenchSample {
	readonly taskId: string;
	readonly skill: SkillLevel;
	readonly memory: MemoryMode;
	readonly score: number;
}

export interface PairedComparison {
	readonly pairs: number;
	readonly delta: number;
	readonly ciLow: number;
	readonly ciHigh: number;
}

function mulberry32(seed: number): () => number {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6d2b79f5) >>> 0;
		let value = state;
		value = Math.imul(value ^ (value >>> 15), value | 1);
		value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
		return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
	};
}

export function pairedBootstrap(
	samples: readonly BenchSample[],
	skillA: SkillLevel,
	skillB: SkillLevel,
	memory: MemoryMode,
	iterations = 2000,
	seed = 7,
): PairedComparison {
	const byTask = new Map<string, { a?: number; b?: number }>();
	for (const sample of samples) {
		if (sample.memory !== memory) continue;
		const entry = byTask.get(sample.taskId) ?? {};
		if (sample.skill === skillA) entry.a = sample.score;
		if (sample.skill === skillB) entry.b = sample.score;
		byTask.set(sample.taskId, entry);
	}
	const deltas: number[] = [];
	for (const entry of byTask.values()) {
		if (entry.a === undefined || entry.b === undefined) continue;
		deltas.push(entry.a - entry.b);
	}
	if (deltas.length === 0) {
		return { pairs: 0, delta: 0, ciLow: 0, ciHigh: 0 };
	}
	const mean = deltas.reduce((sum, value) => sum + value, 0) / deltas.length;
	const random = mulberry32(seed);
	const bootstrapped: number[] = [];
	for (let iteration = 0; iteration < iterations; iteration += 1) {
		let total = 0;
		for (let index = 0; index < deltas.length; index += 1) {
			total += deltas[Math.floor(random() * deltas.length)]!;
		}
		bootstrapped.push(total / deltas.length);
	}
	bootstrapped.sort((left, right) => left - right);
	const lowIndex = Math.floor(iterations * 0.025);
	const highIndex = Math.min(iterations - 1, Math.floor(iterations * 0.975));
	return {
		pairs: deltas.length,
		delta: mean,
		ciLow: bootstrapped[lowIndex]!,
		ciHigh: bootstrapped[highIndex]!,
	};
}

export class SkillBuffBench {
	constructor(private readonly samples: readonly BenchSample[] = []) {}

	compare(skillA: SkillLevel, skillB: SkillLevel, memory: MemoryMode): PairedComparison {
		return pairedBootstrap(this.samples, skillA, skillB, memory);
	}

	levels(): SkillLevel[] {
		return ["none", "curated", "auto"];
	}
}
