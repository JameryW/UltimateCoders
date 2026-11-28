/**
 * DiDPO + A2Agent (#48): sub-diff differential credit and localization credit.
 *
 * DiDPO replaces a single trajectory-level reward with sub-diff anchors:
 * a cohesive sub-diff is compared against the mean outcome of the same sub-diff
 * across sampled trajectories, and the residual advantage is projected onto the
 * tokens that generated it. A2Agent separates localization credit into
 * *discovery* (finding the right region) and *commitment* (locking the final
 * edit there), so a trajectory that found the bug but never committed is not
 * scored the same as one that did neither.
 *
 * Pure deterministic bookkeeping. No model or I/O.
 */

export interface SubDiff {
	readonly id: string;
	readonly path: string;
	readonly patch: string;
	readonly groupability: number;
}

/** Split a multi-file patch into cohesive per-file sub-diffs. */
export function splitSubDiffs(diff: string, minGroupability = 0.5): SubDiff[] {
	if (typeof diff !== "string") {
		throw new TypeError("diff must be a string");
	}
	if (minGroupability < 0 || minGroupability > 1) {
		throw new RangeError("minGroupability must be in [0, 1]");
	}
	const chunks = diff
		.split(/^diff --git /m)
		.map((chunk) => chunk.trim())
		.filter(Boolean);
	const subDiffs: SubDiff[] = [];
	for (let index = 0; index < chunks.length; index++) {
		const chunk = chunks[index];
		const match = /^a\/(.+?) b\/(.+?)(?:\r?\n|$)/.exec(chunk);
		const path = match?.[2] ?? `unknown-${index}`;
		const hunks = (chunk.match(/^@@ /gm) ?? []).length;
		const groupability = 1 / Math.max(1, hunks);
		if (groupability < minGroupability) continue;
		subDiffs.push({
			id: `subdiff-${index}`,
			path,
			patch: `diff --git ${chunk}`,
			groupability,
		});
	}
	return subDiffs;
}

export interface TrajectorySample {
	readonly id: string;
	readonly repo: string;
	readonly subDiffs: readonly SubDiff[];
	readonly advantage: number;
}

export interface DiffAnchor {
	readonly signature: string;
	readonly subDiffIds: readonly string[];
	readonly meanAdvantage: number;
}

/** Build cross-trajectory anchors for each distinct sub-diff path. */
export function buildAnchors(samples: readonly TrajectorySample[]): DiffAnchor[] {
	const groups = new Map<string, { ids: Set<string>; advantages: number[] }>();
	for (const sample of samples) {
		if (!Number.isFinite(sample.advantage)) {
			throw new RangeError("sample advantage must be finite");
		}
		const seen = new Set<string>();
		for (const subDiff of sample.subDiffs) {
			if (seen.has(subDiff.path)) continue;
			seen.add(subDiff.path);
			const group = groups.get(subDiff.path) ?? { ids: new Set<string>(), advantages: [] };
			group.ids.add(subDiff.id);
			group.advantages.push(sample.advantage);
			groups.set(subDiff.path, group);
		}
	}
	return [...groups.entries()]
		.map(([signature, group]) => ({
			signature,
			subDiffIds: [...group.ids].sort(),
			meanAdvantage:
				group.advantages.reduce((sum, value) => sum + value, 0) /
				group.advantages.length,
		}))
		.sort((left, right) => left.signature.localeCompare(right.signature));
}

export interface SubDiffAdvantage {
	readonly subDiffId: string;
	readonly advantage: number;
}

export interface DiDPOCredit {
	readonly sampleId: string;
	readonly perSubDiff: readonly SubDiffAdvantage[];
	readonly discovery: number;
	readonly commitment: number;
	readonly total: number;
}

export const A2_DISCOVERY_WEIGHT = 0.4;
export const A2_COMMITMENT_WEIGHT = 0.6;

/** A2Agent's discovery + commitment credit for the localization phase. */
export function a2Credit(discovered: boolean, committed: boolean): number {
	return (
		(discovered ? A2_DISCOVERY_WEIGHT : 0) +
		(committed ? A2_COMMITMENT_WEIGHT : 0)
	);
}

export function a2Advantage(
	discovered: boolean,
	committed: boolean,
	groupBaseline = 0,
): number {
	if (!Number.isFinite(groupBaseline)) {
		throw new RangeError("groupBaseline must be finite");
	}
	return a2Credit(discovered, committed) - groupBaseline;
}

/** Project a trajectory's residual advantage onto its sub-diffs. */
export function applyDiffInDiff(
	sample: TrajectorySample,
	anchors: readonly DiffAnchor[],
): DiDPOCredit {
	const anchorByPath = new Map(anchors.map((anchor) => [anchor.signature, anchor]));
	const perSubDiff = sample.subDiffs.map((subDiff) => {
		const anchor = anchorByPath.get(subDiff.path);
		const baseline = anchor?.meanAdvantage ?? 0;
		const weighted = (sample.advantage - baseline) * (0.5 + 0.5 * subDiff.groupability);
		return { subDiffId: subDiff.id, advantage: weighted };
	});
	const discovery = a2Credit(true, false);
	const commitment = a2Credit(false, true);
	return {
		sampleId: sample.id,
		perSubDiff,
		discovery,
		commitment,
		total: a2Credit(true, true),
	};
}

/** Fit anchors once and encode each sampled trajectory into DiDPO credit. */
export class DiDPOTrainer {
	private anchors: DiffAnchor[] = [];

	fit(samples: readonly TrajectorySample[]): DiffAnchor[] {
		this.anchors = buildAnchors(samples);
		return this.anchors.map((anchor) => ({ ...anchor, subDiffIds: [...anchor.subDiffIds] }));
	}

	encode(
		sample: TrajectorySample,
		discovered = true,
		committed = true,
		groupBaseline = 0,
	): DiDPOCredit {
		const credit = applyDiffInDiff(sample, this.anchors);
		return {
			...credit,
			discovery: discovered ? A2_DISCOVERY_WEIGHT : 0,
			commitment: committed ? A2_COMMITMENT_WEIGHT : 0,
			total: a2Advantage(discovered, committed, groupBaseline),
		};
	}

	currentAnchors(): readonly DiffAnchor[] {
		return this.anchors;
	}
}
