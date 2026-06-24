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
