/**
 * SessionFarmer + CropChat (#49): harvest work episodes from wild coding
 * sessions and refine their tests with requirement-breaking mutants and a
 * blind solver.
 *
 * The module never starts a container or calls a model. It clusters recorded
 * turns into a Work Episode, seals the episode's mechanical evidence, and
 * drives a caller-supplied blind solver through the 2-round x 3-version
 * closure loop. A trial resolves only when the mechanical test and the
 * independent quality judgement both pass.
 *
 * Pure deterministic bookkeeping.
 */

export interface WorkTurn {
	readonly id: string;
	readonly sessionId: string;
	readonly turn: number;
	readonly request: string;
	readonly files: readonly string[];
	readonly commit?: string;
	readonly tests?: readonly string[];
}

export interface WorkEpisode {
	readonly id: string;
	readonly turns: readonly WorkTurn[];
	readonly sessions: readonly string[];
	readonly files: readonly string[];
	readonly commits: readonly string[];
	readonly hasTests: boolean;
}

function normalizeFiles(files: readonly string[]): string[] {
	return [...new Set(files.map((file) => file.trim()).filter(Boolean))].sort();
}

/** Cluster turns that share at least `minSharedFiles` edited files. */
export function clusterTurns(
	turns: readonly WorkTurn[],
	minSharedFiles = 1,
): WorkEpisode[] {
	if (minSharedFiles < 1) {
		throw new RangeError("minSharedFiles must be positive");
	}
	const seen = new Set<string>();
	for (const turn of turns) {
		if (!turn.id.trim()) throw new Error("turn id must not be empty");
		if (!turn.sessionId.trim()) throw new Error("sessionId must not be empty");
		if (!Number.isInteger(turn.turn) || turn.turn < 0) {
			throw new RangeError("turn must be a non-negative integer");
		}
		if (seen.has(turn.id)) throw new Error(`duplicate turn id ${turn.id}`);
		seen.add(turn.id);
	}

	const parent = turns.map((_, index) => index);
	const find = (index: number): number => {
		let current = index;
		while (parent[current] !== current) {
			parent[current] = parent[parent[current]];
			current = parent[current];
		}
		return current;
	};
	const union = (left: number, right: number): void => {
		const a = find(left);
		const b = find(right);
		if (a !== b) parent[b] = a;
	};

	const filesByIndex = turns.map((turn) => new Set(normalizeFiles(turn.files)));
	for (let left = 0; left < turns.length; left++) {
		for (let right = left + 1; right < turns.length; right++) {
			let shared = 0;
			for (const file of filesByIndex[left]) {
				if (filesByIndex[right].has(file)) shared++;
			}
			if (shared >= minSharedFiles) union(left, right);
		}
	}

	const groups = new Map<number, WorkTurn[]>();
	for (let index = 0; index < turns.length; index++) {
		const root = find(index);
		const group = groups.get(root) ?? [];
		group.push(turns[index]);
		groups.set(root, group);
	}

	return [...groups.values()]
		.map((group, index) => {
			const ordered = [...group].sort(
				(left, right) =>
					left.sessionId.localeCompare(right.sessionId) || left.turn - right.turn,
			);
			const sessions = [...new Set(ordered.map((turn) => turn.sessionId))].sort();
			const files = normalizeFiles(ordered.flatMap((turn) => turn.files));
			const commits = ordered
				.map((turn) => turn.commit)
				.filter((commit): commit is string => Boolean(commit));
			return {
				id: `episode-${index}`,
				turns: ordered,
				sessions,
				files,
				commits,
				hasTests: ordered.some((turn) => (turn.tests?.length ?? 0) > 0),
			};
		})
		.sort((left, right) => left.id.localeCompare(right.id));
}

export interface RequirementMutant {
	readonly id: string;
	readonly description: string;
	readonly assertion: string;
	readonly expectedResolved: boolean;
}

export interface SolverOutcome {
	readonly testsPassed: boolean;
	readonly qualityNotes?: string;
}

export type BlindSolver = (
	episode: WorkEpisode,
	mutant: RequirementMutant,
) => SolverOutcome;

export type QualityJudge = (
	episode: WorkEpisode,
	mutant: RequirementMutant,
	outcome: SolverOutcome,
) => boolean;

export interface ClosureTrial {
	readonly round: number;
	readonly version: number;
	readonly mutantId: string;
	readonly mechanicalResolved: boolean;
	readonly qualityResolved: boolean;
	readonly resolved: boolean;
	readonly expected: boolean;
	readonly correct: boolean;
}

export interface RefinementReport {
	readonly trials: readonly ClosureTrial[];
	readonly balancedAccuracy: number;
	readonly falsePositives: readonly string[];
	readonly falseNegatives: readonly string[];
	readonly rounds: number;
	readonly versions: number;
}

/** Balanced accuracy over "tests should resolve" labels. */
export function balancedAccuracy(trials: readonly ClosureTrial[]): number {
	const expectedPositive = trials.filter((trial) => trial.expected);
	const expectedNegative = trials.filter((trial) => !trial.expected);
	if (expectedPositive.length === 0 || expectedNegative.length === 0) {
		return trials.length > 0 && trials.every((trial) => trial.correct) ? 1 : 0;
	}
	const sensitivity =
		expectedPositive.filter((trial) => trial.correct).length / expectedPositive.length;
	const specificity =
		expectedNegative.filter((trial) => trial.correct).length / expectedNegative.length;
	return (sensitivity + specificity) / 2;
}

/** Run the CropChat closure loop: mutants x blind solver x quality judge. */
export class CropRefiner {
	refine(
		episode: WorkEpisode,
		mutants: readonly RequirementMutant[],
		solver: BlindSolver,
		judge: QualityJudge,
		rounds = 2,
		versions = 3,
	): RefinementReport {
		if (!Number.isInteger(rounds) || rounds < 1) {
			throw new RangeError("rounds must be a positive integer");
		}
		if (!Number.isInteger(versions) || versions < 1) {
			throw new RangeError("versions must be a positive integer");
		}
		if (mutants.length === 0) throw new Error("at least one mutant is required");
		const ids = new Set<string>();
		for (const mutant of mutants) {
			if (!mutant.id.trim()) throw new Error("mutant id must not be empty");
			if (ids.has(mutant.id)) throw new Error(`duplicate mutant id ${mutant.id}`);
			ids.add(mutant.id);
		}

		const trials: ClosureTrial[] = [];
		const falsePositives: string[] = [];
		const falseNegatives: string[] = [];
		for (let round = 1; round <= rounds; round++) {
			for (let version = 1; version <= versions; version++) {
				for (const mutant of mutants) {
					const outcome = solver(episode, mutant);
					const mechanicalResolved = outcome.testsPassed;
					const qualityResolved = judge(episode, mutant, outcome);
					const resolved = mechanicalResolved && qualityResolved;
					const correct = resolved === mutant.expectedResolved;
					if (!correct) {
						if (resolved) falsePositives.push(mutant.id);
						else falseNegatives.push(mutant.id);
					}
					trials.push({
						round,
						version,
						mutantId: mutant.id,
						mechanicalResolved,
						qualityResolved,
						resolved,
						expected: mutant.expectedResolved,
						correct,
					});
				}
			}
		}
		return {
			trials,
			balancedAccuracy: balancedAccuracy(trials),
			falsePositives,
			falseNegatives,
			rounds,
			versions,
		};
	}
}

export interface HarvestReport {
	readonly episodes: readonly WorkEpisode[];
	readonly untestedCommits: number;
	readonly testedCommits: number;
	readonly sessions: number;
}

/** SessionFarmer's deterministic harvester. */
export class SessionFarmer {
	private readonly turns: WorkTurn[] = [];

	record(turn: WorkTurn): void {
		if (this.turns.some((existing) => existing.id === turn.id)) {
			throw new Error(`duplicate turn id ${turn.id}`);
		}
		this.turns.push(turn);
	}

	episodes(minSharedFiles = 1): WorkEpisode[] {
		return clusterTurns(this.turns, minSharedFiles);
	}

	harvest(minSharedFiles = 1): HarvestReport {
		const episodes = this.episodes(minSharedFiles);
		const commits = this.turns
			.map((turn) => turn.commit)
			.filter((commit): commit is string => Boolean(commit));
		const testedCommits = this.turns
			.filter((turn) => (turn.tests?.length ?? 0) > 0)
			.filter((turn) => Boolean(turn.commit)).length;
		return {
			episodes,
			untestedCommits: commits.length - testedCommits,
			testedCommits,
			sessions: new Set(this.turns.map((turn) => turn.sessionId)).size,
		};
	}

	clear(): void {
		this.turns.length = 0;
	}
}

/** A sealed episode is the minimum input a blind solver may see. */
export interface SealedEpisode {
	readonly episodeId: string;
	readonly baseCommit: string;
	readonly files: readonly string[];
	readonly request: string;
}

export function sealEpisode(episode: WorkEpisode, baseCommit: string): SealedEpisode {
	if (!baseCommit.trim()) throw new Error("baseCommit must not be empty");
	if (episode.turns.length === 0) throw new Error("episode has no turns");
	if (episode.commits.length === 0) throw new Error("episode has no commit");
	return {
		episodeId: episode.id,
		baseCommit,
		files: episode.files,
		request: episode.turns.map((turn) => turn.request).join("\n"),
	};
}
