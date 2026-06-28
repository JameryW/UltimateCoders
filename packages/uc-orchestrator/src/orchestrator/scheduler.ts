/**
 * DAG Scheduler — Topological sort with wave-based parallel execution.
 *
 * Inspired by omp swarm-extension's DAG engine but with dynamic
 * scheduling support for UC's orchestrator pattern.
 *
 * ponytail: Kahn's algorithm for topological sort — O(V+E),
 * upgrade to incremental if tasks get huge.
 */

// ── Types ──────────────────────────────────────────────────────────

export interface SubtaskDef {
	id: string;
	description: string;
	dependsOn: string[];
	files: string[];
	/** How this subtask should be dispatched: "local" | "remote" | "prefer_remote" (default) */
	dispatchMode?: string;
	/** Capabilities required by this subtask (e.g. "rust", "python", "docker"). Worker must have ALL. */
	requiredCapabilities?: string[];
}

/** A wave is a group of subtasks that can execute in parallel. */
export type DAGWave = SubtaskDef[];

// ── DAG Construction ───────────────────────────────────────────────

/**
 * Build execution waves from subtask definitions.
 *
 * Uses Kahn's algorithm to topologically sort the dependency graph,
 * then groups subtasks into waves where all subtasks in a wave
 * have their dependencies satisfied by earlier waves.
 *
 * @throws Error if circular dependencies are detected
 */
export function buildDAG(subtasks: SubtaskDef[]): DAGWave[] {
	const ids = new Set(subtasks.map((s) => s.id));

	// Validate: all depends_on references must exist
	for (const st of subtasks) {
		for (const dep of st.dependsOn) {
			if (!ids.has(dep)) {
				throw new Error(
					`Subtask ${st.id} depends on ${dep}, which does not exist`,
				);
			}
		}
	}

	// Detect cycles via Kahn's algorithm
	const cycles = detectCycles(subtasks);
	if (cycles) {
		throw new Error(`Circular dependencies detected: [${cycles.join(", ")}]`);
	}

	// Build in-degree map
	const inDegree = new Map<string, number>();
	const dependents = new Map<string, Set<string>>(); // who depends on me

	for (const st of subtasks) {
		inDegree.set(st.id, st.dependsOn.length);
		for (const dep of st.dependsOn) {
			if (!dependents.has(dep)) dependents.set(dep, new Set());
			dependents.get(dep)!.add(st.id);
		}
	}

	// Kahn's algorithm — wave by wave
	const waves: DAGWave[] = [];
	const processed = new Set<string>();

	while (processed.size < subtasks.length) {
		// Find all subtasks with in-degree 0 (not yet processed)
		const wave: SubtaskDef[] = [];
		for (const st of subtasks) {
			if (processed.has(st.id)) continue;
			if (inDegree.get(st.id)! === 0) {
				wave.push(st);
			}
		}

		if (wave.length === 0) {
			// This shouldn't happen after cycle detection, but guard anyway
			throw new Error("Deadlock in dependency graph — no progress possible");
		}

		waves.push(wave);

		// Process this wave: decrease in-degree of dependents
		for (const st of wave) {
			processed.add(st.id);
			const deps = dependents.get(st.id) || new Set();
			for (const depId of deps) {
				inDegree.set(depId, inDegree.get(depId)! - 1);
			}
		}
	}

	return waves;
}

/**
 * Detect circular dependencies using Kahn's algorithm.
 *
 * Returns an array of IDs involved in a cycle, or null if acyclic.
 */
export function detectCycles(subtasks: SubtaskDef[]): string[] | null {
	const inDegree = new Map<string, number>();
	const dependents = new Map<string, Set<string>>();

	for (const st of subtasks) {
		inDegree.set(st.id, st.dependsOn.length);
		for (const dep of st.dependsOn) {
			if (!dependents.has(dep)) dependents.set(dep, new Set());
			dependents.get(dep)!.add(st.id);
		}
	}

	const queue: string[] = [];
	for (const [id, deg] of inDegree) {
		if (deg === 0) queue.push(id);
	}

	let processed = 0;
	while (queue.length > 0) {
		const id = queue.shift()!;
		processed++;
		for (const depId of dependents.get(id) || new Set()) {
			inDegree.set(depId, inDegree.get(depId)! - 1);
			if (inDegree.get(depId) === 0) {
				queue.push(depId);
			}
		}
	}

	if (processed < subtasks.length) {
		// Remaining subtasks are in cycles
		return subtasks
			.filter((st) => inDegree.get(st.id)! > 0)
			.map((st) => st.id);
	}

	return null;
}

// ── File-Aware Wave Splitting ───────────────────────────────────────

/**
 * Split waves so that no two subtasks in the same sub-wave share files.
 *
 * Within each wave from buildDAG(), further partition into sub-waves
 * where all subtasks have disjoint file sets. Subtasks with no files
 * (empty array) are always safe to parallelize.
 *
 * Uses greedy graph coloring: subtasks sharing files get an edge,
 * then each color class becomes a sub-wave.
 *
 * ponytail: O(V²) greedy — fine for ≤10 subtasks per wave.
 */
export function splitWavesByFileOverlap(waves: DAGWave[]): DAGWave[] {
	const result: DAGWave[] = [];
	for (const wave of waves) {
		if (wave.length <= 1 || wave.every((s) => s.files.length === 0)) {
			result.push(wave);
			continue;
		}
		// Build conflict graph: edge if two subtasks share any file
		const conflictPairs = new Set<string>();
		for (let i = 0; i < wave.length; i++) {
			for (let j = i + 1; j < wave.length; j++) {
				if (hasFileOverlap(wave[i].files, wave[j].files)) {
					conflictPairs.add(`${i}:${j}`);
				}
			}
		}
		if (conflictPairs.size === 0) {
			result.push(wave);
			continue;
		}
		// Greedy coloring
		const colorAssignment = new Map<number, number>(); // index → color
		for (let i = 0; i < wave.length; i++) {
			const neighborColors = new Set<number>();
			for (let j = 0; j < wave.length; j++) {
				if (i === j) continue;
				const key = i < j ? `${i}:${j}` : `${j}:${i}`;
				if (conflictPairs.has(key) && colorAssignment.has(j)) {
					neighborColors.add(colorAssignment.get(j)!);
				}
			}
			// Assign lowest available color
			let color = 0;
			while (neighborColors.has(color)) color++;
			colorAssignment.set(i, color);
		}
		// Group by color → sub-waves
		const colorGroups = new Map<number, SubtaskDef[]>();
		for (const [idx, color] of colorAssignment) {
			if (!colorGroups.has(color)) colorGroups.set(color, []);
			colorGroups.get(color)!.push(wave[idx]);
		}
		// Insert sub-waves in color order
		const maxColor = Math.max(...colorAssignment.values());
		for (let c = 0; c <= maxColor; c++) {
			const group = colorGroups.get(c);
			if (group) result.push(group);
		}
	}
	return result;
}

function hasFileOverlap(a: string[], b: string[]): boolean {
	if (a.length === 0 || b.length === 0) return false;
	const setB = new Set(b);
	return a.some((f) => setB.has(f));
}

// ── Runtime File Intent Tracking ────────────────────────────────────

/**
 * Track which files each running subtask intends to modify.
 *
 * Used at execution time to defer subtasks whose files conflict
 * with already-running subtasks. Complements the static
 * splitWavesByFileOverlap by handling cases where actual modified
 * files differ from declared files.
 */
export class FileIntentTracker {
	/** subtaskId → Set of file paths */
	private intents = new Map<string, Set<string>>();
	/** filePath → Set of subtask IDs owning it */
	private fileOwners = new Map<string, Set<string>>();

	/** Declare that a subtask intends to modify the given files.
	 *  If the subtask already has declared intents, releases old ones first. */
	declare(subtaskId: string, files: string[]): void {
		// Release any previous intents for this subtask to avoid stale entries
		if (this.intents.has(subtaskId)) {
			this.release(subtaskId);
		}
		const fileSet = new Set(files);
		this.intents.set(subtaskId, fileSet);
		for (const f of files) {
			if (!this.fileOwners.has(f)) this.fileOwners.set(f, new Set());
			this.fileOwners.get(f)!.add(subtaskId);
		}
	}

	/** Release all file intents for a completed/failed/cancelled subtask. */
	release(subtaskId: string): void {
		const files = this.intents.get(subtaskId);
		if (!files) return;
		for (const f of files) {
			const owners = this.fileOwners.get(f);
			if (owners) {
				owners.delete(subtaskId);
				if (owners.size === 0) this.fileOwners.delete(f);
			}
		}
		this.intents.delete(subtaskId);
	}

	/** Check if any of the given files conflict with running subtasks.
	 *  Returns the set of conflicting subtask IDs (empty if no conflict). */
	isConflicting(files: string[]): Set<string> {
		const conflicting = new Set<string>();
		for (const f of files) {
			const owners = this.fileOwners.get(f);
			if (owners) {
				for (const id of owners) conflicting.add(id);
			}
		}
		return conflicting;
	}

	/** Get all currently tracked file ownerships (for debugging/status). */
	getOwnedFiles(): Map<string, string[]> {
		const result = new Map<string, string[]>();
		for (const [file, owners] of this.fileOwners) {
			result.set(file, [...owners]);
		}
		return result;
	}

	/** Clear all intents. */
	clear(): void {
		this.intents.clear();
		this.fileOwners.clear();
	}
}

// ── Circuit Breaker ─────────────────────────────────────────────────

export type CircuitState = "closed" | "open" | "half_open";

/**
 * Simple circuit breaker for subtask execution.
 *
 * Prevents cascading failures: if too many subtasks fail consecutively,
 * the circuit opens and subsequent attempts fail fast (with a delay
 * before trying again in half-open state).
 *
 * ponytail: no token buckets, no RPM/TPM tracking — omp host process
 * handles API rate limiting. This only prevents wasting resources on
 * a degraded service.
 */
export class CircuitBreaker {
	private state: CircuitState = "closed";
	private failureCount = 0;
	private lastFailureTime = 0;

	constructor(
		private readonly threshold = 3,
		private readonly resetMs = 30_000,
	) {}

	/** Check if execution is allowed. Returns false if circuit is open. */
	canExecute(): boolean {
		if (this.state === "closed") return true;
		if (this.state === "half_open") return true;
		// Open: check if reset timeout has elapsed
		if (Date.now() - this.lastFailureTime >= this.resetMs) {
			this.state = "half_open";
			return true;
		}
		return false;
	}

	/** Record a successful execution. */
	recordSuccess(): void {
		this.failureCount = 0;
		this.state = "closed";
	}

	/** Record a failed execution. */
	recordFailure(): void {
		this.failureCount++;
		this.lastFailureTime = Date.now();
		if (this.failureCount >= this.threshold) {
			this.state = "open";
		}
	}

	/** Get current circuit state (for debugging/status). */
	getState(): CircuitState {
		return this.state;
	}

	/** Reset the circuit to closed state. */
	reset(): void {
		this.failureCount = 0;
		this.state = "closed";
	}
}
