/**
 * SWE-Prime (#11) — two-stage semantic segment distillation.
 *
 * Stage 1 selects a high-value, representative subset of successful
 * trajectories (`selectHighValueTrajectories`). Stage 2 assigns a binary loss
 * mask to semantic segments: reverted and repeated-ineffective segments are
 * kept in the context window but excluded from the SFT loss, and adjacent
 * same-kind segments of the kept set are collapsed into one segment.
 *
 * This module is pure data planning: no I/O, no clock, no randomness, and no
 * mutation of the caller's objects.
 */

/** Behavioural category of a semantic trajectory segment. */
export type SegmentKind = "exploration" | "localization" | "editing" | "verification" | "repair";

/** All segment kinds, in the order used by the SWE-Prime segment scorer. */
export const SEGMENT_KINDS: readonly SegmentKind[] = [
	"exploration",
	"localization",
	"editing",
	"verification",
	"repair",
] as const;

/** One semantically coherent span of an agent trajectory. */
export interface TrajectorySegment {
	/** Stable segment identifier (unique within a trajectory). */
	id: string;
	/** Behavioural category of the segment. */
	kind: SegmentKind;
	/** First turn of the segment (inclusive, 0-based). */
	startTurn: number;
	/** Last turn of the segment (inclusive, 0-based). */
	endTurn: number;
	/** The work was rolled back; never train on this segment. */
	reverted: boolean;
	/** The segment repeats an earlier action without new information. */
	repeatedIneffective: boolean;
	/** The segment contains a positive, learnable core. */
	positiveCore: boolean;
	/** Optional caller-owned annotations; copied, never mutated. */
	metadata?: Record<string, unknown>;
}

/** Options for {@link distillSegments}. */
export interface DistillOptions {
	/**
	 * When true, additionally mask every segment that is not flagged
	 * `positiveCore`. Defaults to false (keep all non-reverted,
	 * non-repeated-ineffective segments).
	 */
	keepPositiveOnly?: boolean;
}

/** Result of distilling one trajectory's semantic segments. */
export interface DistilledTrajectory {
	/**
	 * Kept segments in trajectory order, with adjacent same-kind segments
	 * collapsed. Masked segments are absent here but still contribute their
	 * turns to `lossMask` so callers can keep them as context.
	 */
	segments: TrajectorySegment[];
	/** `keptTurns / totalTurns`; 0 for an empty trajectory. */
	keptRatio: number;
	/** Total number of turns covered by the input segments. */
	totalTurns: number;
	/** Number of turns selected for the SFT loss (sum of `lossMask`). */
	keptTurns: number;
	/**
	 * Per-turn loss mask over the input trajectory, in input segment order:
	 * 1 = train on the turn, 0 = keep as context only.
	 */
	lossMask: number[];
}

/** Minimal shape needed by {@link selectHighValueTrajectories}. */
export interface ScoredTrajectory {
	/** Optional stable id; used to break exact score ties deterministically. */
	id?: string;
	/** Higher is better. Must be finite. */
	score: number;
}

function segmentTurnCount(segment: TrajectorySegment): number {
	return segment.endTurn - segment.startTurn + 1;
}

function assertSegment(segment: TrajectorySegment, index: number): void {
	if (segment === null || typeof segment !== "object") {
		throw new TypeError(`segments[${index}] must be an object`);
	}
	if (typeof segment.id !== "string" || segment.id.length === 0) {
		throw new TypeError(`segments[${index}].id must be a non-empty string`);
	}
	if (!SEGMENT_KINDS.includes(segment.kind)) {
		throw new RangeError(`segments[${index}].kind is not a valid SegmentKind: ${String(segment.kind)}`);
	}
	if (
		!Number.isInteger(segment.startTurn) ||
		!Number.isInteger(segment.endTurn) ||
		segment.startTurn < 0 ||
		segment.endTurn < segment.startTurn
	) {
		throw new RangeError(
			`segments[${index}] has an invalid turn range: [${String(segment.startTurn)}, ${String(segment.endTurn)}]`,
		);
	}
}

function mergeMetadata(
	left: Record<string, unknown> | undefined,
	right: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
	if (left === undefined) return right === undefined ? undefined : { ...right };
	if (right === undefined) return { ...left };
	return { ...left, ...right };
}

/**
 * Collapse adjacent (list-adjacent and turn-contiguous) segments of the same
 * kind. The first segment's `id` is retained; turn ranges are joined and
 * metadata is shallow-merged (right wins on key conflicts).
 */
function collapseAdjacentSameKind(segments: readonly TrajectorySegment[]): TrajectorySegment[] {
	const collapsed: TrajectorySegment[] = [];
	for (const segment of segments) {
		const previous = collapsed[collapsed.length - 1];
		if (
			previous !== undefined &&
			previous.kind === segment.kind &&
			previous.endTurn + 1 === segment.startTurn &&
			String(previous.metadata?.subtaskId ?? "") === String(segment.metadata?.subtaskId ?? "")
		) {
			collapsed[collapsed.length - 1] = {
				...previous,
				endTurn: segment.endTurn,
				positiveCore: previous.positiveCore || segment.positiveCore,
				metadata: mergeMetadata(previous.metadata, segment.metadata),
			};
		} else {
			collapsed.push({ ...segment, metadata: segment.metadata === undefined ? undefined : { ...segment.metadata } });
		}
	}
	return collapsed;
}

/**
 * Distill one trajectory's semantic segments into a loss mask plus the kept
 * (and same-kind-collapsed) segment list.
 *
 * Masking rules, in order:
 * 1. `reverted` and `repeatedIneffective` segments are always masked.
 * 2. With `keepPositiveOnly`, non-`positiveCore` segments are masked too.
 *
 * Turns are counted as `endTurn - startTurn + 1`; `lossMask` has one entry per
 * turn of the input, in input order, so masked segments remain available as
 * context while contributing zero loss.
 */
export function distillSegments(
	segments: readonly TrajectorySegment[],
	options: DistillOptions = {},
): DistilledTrajectory {
	if (!Array.isArray(segments)) throw new TypeError("segments must be an array");
	const keepPositiveOnly = options.keepPositiveOnly ?? false;
	if (typeof keepPositiveOnly !== "boolean") throw new TypeError("options.keepPositiveOnly must be a boolean");

	const lossMask: number[] = [];
	const kept: TrajectorySegment[] = [];
	let totalTurns = 0;
	let keptTurns = 0;

	for (let index = 0; index < segments.length; index++) {
		const segment = segments[index];
		assertSegment(segment, index);
		const turns = segmentTurnCount(segment);
		totalTurns += turns;
		const masked = segment.reverted || segment.repeatedIneffective || (keepPositiveOnly && !segment.positiveCore);
		for (let turn = 0; turn < turns; turn++) {
			lossMask.push(masked ? 0 : 1);
		}
		if (!masked) {
			kept.push(segment);
			keptTurns += turns;
		}
	}

	return {
		segments: collapseAdjacentSameKind(kept),
		keptRatio: totalTurns === 0 ? 0 : keptTurns / totalTurns,
		totalTurns,
		keptTurns,
		lossMask,
	};
}

/**
 * Deterministically select the top `ratio` fraction of scored trajectories.
 *
 * - `ratio` is clamped to [0, 1]; at least one item is selected when the ratio
 *   is positive and the input is non-empty.
 * - Ranking is by descending `score`, then ascending `id` for exact ties, then
 *   original input order. The returned array is a new array in ranked order.
 * - The input array and its items are not mutated.
 */
export function selectHighValueTrajectories<T extends ScoredTrajectory>(
	items: readonly T[],
	ratio = 0.1,
): T[] {
	if (!Array.isArray(items)) throw new TypeError("items must be an array");
	if (!Number.isFinite(ratio)) throw new RangeError(`ratio must be a finite number, received ${String(ratio)}`);

	const ranked = items.map((item, index) => {
		if (item === null || typeof item !== "object") {
			throw new TypeError(`items[${index}] must be an object`);
		}
		if (!Number.isFinite(item.score)) {
			throw new RangeError(`items[${index}].score must be a finite number, received ${String(item.score)}`);
		}
		return { item, index };
	});

	if (ranked.length === 0) return [];
	const boundedRatio = Math.min(1, Math.max(0, ratio));
	const count = boundedRatio === 0 ? 0 : Math.max(1, Math.ceil(ranked.length * boundedRatio));

	ranked.sort((a, b) => {
		if (b.item.score !== a.item.score) return b.item.score - a.item.score;
		const aId = a.item.id;
		const bId = b.item.id;
		if (aId !== undefined && bId !== undefined) {
			if (aId !== bId) return aId < bId ? -1 : 1;
		} else if (aId !== undefined || bId !== undefined) {
			return aId !== undefined ? -1 : 1;
		}
		return a.index - b.index;
	});

	return ranked.slice(0, count).map((entry) => entry.item);
}
