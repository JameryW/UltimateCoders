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

/** A single step in a subtask's multi-agent workflow chain.
 *  Field names mirror Rust `WorkflowStep` (snake_case) — proto-generated
 *  TS types use camelCase, but the decomposer JSON + persistence use snake_case.
 *  grpc-bridge.ts handles the camelCase mapping at proto serialization time. */
export interface WorkflowStepDef {
	agent: string;
	prompt: string;
	agent_config_json?: string;
	abort_on_failure?: boolean;
}

export interface SubtaskDef {
	id: string;
	description: string;
	dependsOn: string[];
	files: string[];
	/** How this subtask should be dispatched: "local" | "remote" | "prefer_remote" (default) */
	dispatchMode?: string;
	/** Capabilities required by this subtask (e.g. "rust", "python", "docker"). Worker must have ALL. */
	requiredCapabilities?: string[];
	/** Parent subtask ID — set when this subtask was decomposed from a larger one. */
	parentSubtaskId?: string;
	/** Depth of recursive decomposition (0 = original, 1 = decomposed from a subtask, etc.). */
	decompositionDepth?: number;
	/** Maximum allowed decomposition depth. Default: 2. */
	maxDecompositionDepth?: number;
	/** Estimated complexity: "simple" (single file, <50 lines) | "moderate" (1-3 files) | "complex" (3+ files or cross-cutting). */
	complexity?: string;
	/** Per-subtask agent configuration overrides. Keys: tools, allowed_tools,
	 *  disallowed_tools, mcp_configs, append_system_prompt, agent_name, agents_json. */
	agentConfig?: Record<string, unknown>;
	/** Ordered multi-agent workflow steps. Empty/undefined = single-agent
	 *  execution (backward compatible). When non-empty, worker runs steps
	 *  in order, threading each step's output into the next via template vars. */
	steps?: WorkflowStepDef[];
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

// ── Recursive Decomposition ──────────────────────────────────────

/** Check if a subtask should be further decomposed based on its characteristics. */
export function shouldDecompose(subtask: SubtaskDef): boolean {
	const depth = subtask.decompositionDepth ?? 0;
	const maxDepth = subtask.maxDecompositionDepth ?? 2;
	if (depth >= maxDepth) return false;
	// Complex subtasks with multiple files benefit from decomposition
	if (subtask.files.length >= 3) return true;
	// Long descriptions suggest complex tasks
	if (subtask.description.length > 200) return true;
	// Explicitly marked as complex
	if (subtask.complexity === "complex") return true;
	return false;
}

/**
 * Decompose a single subtask into multiple finer-grained subtasks.
 *
 * Splits a complex subtask by file boundaries when possible:
 * - Each file becomes its own subtask
 * - Shared setup/context becomes a dependency subtask
 * - Preserves the original subtask's dependsOn as dependencies for the first decomposed subtask
 *
 * ponytail: simple file-based split — upgrade to LLM-driven decomposition
 * when file-based split produces subtasks that are too fine-grained.
 */
export function decomposeSubtask(parent: SubtaskDef): SubtaskDef[] {
	const depth = (parent.decompositionDepth ?? 0) + 1;

	// If subtask declares specific files, split by file
	if (parent.files.length > 1) {
		const results: SubtaskDef[] = [];
		// Create one subtask per file
		for (let i = 0; i < parent.files.length; i++) {
			const depIds = i === 0 ? parent.dependsOn : [results[0].id];
			results.push({
				id: `${parent.id}-f${i}`,
				description: `${parent.description} (file: ${parent.files[i]})`,
				dependsOn: depIds,
				files: [parent.files[i]],
				dispatchMode: parent.dispatchMode,
				requiredCapabilities: parent.requiredCapabilities,
				parentSubtaskId: parent.id,
				decompositionDepth: depth,
				maxDecompositionDepth: parent.maxDecompositionDepth,
				complexity: "simple",
			});
		}
		return results;
	}

	// Single file or no files — split description by logical boundaries
	// ponytail: newline split as heuristic — LLM decomposition handles the real cases
	const parts = parent.description
		.split(/\n+/)
		.map((s) => s.trim())
		.filter((s) => s.length > 10);

	if (parts.length <= 1) {
		// Can't decompose further — return original
		return [{ ...parent, decompositionDepth: depth }];
	}

	const results: SubtaskDef[] = [];
	for (let i = 0; i < parts.length; i++) {
		const depIds = i === 0 ? parent.dependsOn : [results[i - 1].id];
		results.push({
			id: `${parent.id}-p${i}`,
			description: parts[i],
			dependsOn: depIds,
			files: i === 0 ? parent.files : [],
			dispatchMode: parent.dispatchMode,
			requiredCapabilities: parent.requiredCapabilities,
			parentSubtaskId: parent.id,
			decompositionDepth: depth,
			maxDecompositionDepth: parent.maxDecompositionDepth,
			complexity: "simple",
			agentConfig: parent.agentConfig,
		});
	}
	return results;
}

/**
 * Recursively decompose complex subtasks in a wave plan.
 *
 * For each subtask that shouldDecompose() returns true, replaces it
 * with the decomposed children. Preserves wave ordering by inserting
 * children where the parent was.
 *
 * ponytail: single-pass — doesn't re-check if decomposed children
 * should also be decomposed. Call again if deeper decomposition needed.
 */
export function recursiveDecompose(waves: DAGWave[]): DAGWave[] {
	const result: DAGWave[] = [];
	for (const wave of waves) {
		const newWave: SubtaskDef[] = [];
		for (const subtask of wave) {
			if (shouldDecompose(subtask)) {
				const children = decomposeSubtask(subtask);
				// First child takes parent's position in wave
				// Remaining children form sequential waves (depends on previous)
				newWave.push(children[0]);
				for (let i = 1; i < children.length; i++) {
					result.push([children[i]]);
				}
			} else {
				newWave.push(subtask);
			}
		}
		if (newWave.length > 0) {
			result.push(newWave);
		}
	}
	return result;
}

// ── Dynamic Dispatch ──────────────────────────────────────────────

/** Worker info for dispatch decisions. */
export interface WorkerAvailability {
	id: string;
	capabilities: string[];
	currentLoad: number;
	maxCapacity: number;
	isRemote: boolean;
}

/**
 * Resolve dynamic dispatch mode for a subtask based on available workers.
 *
 * Rules:
 * - "local" → always local
 * - "remote" → must be remote; if no capable remote worker, mark as blocked
 * - "prefer_remote" → use remote if capable worker available, else local
 * - "auto" → decide based on subtask characteristics and worker state
 *
 * Returns the resolved dispatch mode and optionally the target worker ID.
 */
export function resolveDispatchMode(
	subtask: SubtaskDef,
	workers: WorkerAvailability[],
): { mode: "local" | "remote" | "prefer_remote"; workerId?: string; blocked: boolean } {
	const mode = subtask.dispatchMode ?? "prefer_remote";

	if (mode === "local") {
		return { mode: "local", blocked: false };
	}

	const requiredCaps = new Set(subtask.requiredCapabilities ?? []);

	// Find capable workers (ALL required capabilities match)
	const capableRemote = workers
		.filter((w) => w.isRemote && (requiredCaps.size === 0 || [...requiredCaps].every((c) => w.capabilities.includes(c))))
		.filter((w) => w.isRemote && w.currentLoad < w.maxCapacity);

	const capableLocal = workers
		.filter((w) => !w.isRemote && (requiredCaps.size === 0 || [...requiredCaps].every((c) => w.capabilities.includes(c))))
		.filter((w) => !w.isRemote && w.currentLoad < w.maxCapacity);

	if (mode === "remote") {
		if (capableRemote.length === 0) {
			return { mode: "remote", blocked: true };
		}
		const best = pickLeastLoaded(capableRemote);
		return { mode: "remote", workerId: best.id, blocked: false };
	}

	// "prefer_remote" or "auto"
	if (capableRemote.length > 0) {
		const best = pickLeastLoaded(capableRemote);
		return { mode: "prefer_remote", workerId: best.id, blocked: false };
	}

	if (capableLocal.length > 0) {
		return { mode: "local", blocked: false };
	}

	// No capable worker at all
	return { mode, blocked: true };
}

function pickLeastLoaded(workers: WorkerAvailability[]): WorkerAvailability {
	return workers.reduce((best, w) =>
		w.currentLoad < best.currentLoad ? w : best,
	);
}
