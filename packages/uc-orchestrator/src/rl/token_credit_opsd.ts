// Token-level credit from multi-agent communication and dynamic rubrics.
//
// Ledger item #124 (OPSD4MAS, arXiv:2611.16206; ORSD, arXiv:2611.06543).
// The module turns communication facts into leave-one-out filtered token
// credits and turns process-rubric score differences into an additive GRPO
// advantage. It is pure: no model call, no sampler, no I/O.

export interface CommunicationFact {
	readonly id: string;
	readonly sender: string;
	readonly receiver: string;
	readonly tokenIds: readonly number[];
	readonly withFactLogprob: number;
	readonly withoutFactLogprob: number;
}

export interface FilteredFact extends CommunicationFact {
	readonly gain: number;
}

export interface RubricItem {
	readonly id: string;
	readonly weight: number;
	readonly withRubricLogprob: number;
	readonly withoutRubricLogprob: number;
}

export interface RubricAdvantage {
	readonly advantage: number;
	readonly weightedDelta: number;
	readonly activeItems: number;
}

export class TokenCreditError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "TokenCreditError";
	}
}

/** Leave-one-out gain of keeping a communication fact. */
export function communicationGain(fact: CommunicationFact): number {
	return fact.withFactLogprob - fact.withoutFactLogprob;
}

/** Coarse/fine leave-one-out filter; keeps only facts that improved the outcome. */
export function filterCommunicationFacts(
	facts: readonly CommunicationFact[],
	minGain = 0,
): readonly FilteredFact[] {
	const seen = new Set<string>();
	const kept: FilteredFact[] = [];
	for (const fact of facts) {
		validateFact(fact);
		if (seen.has(fact.id)) {
			throw new TokenCreditError(`duplicate fact ${fact.id}`);
		}
		seen.add(fact.id);
		const gain = communicationGain(fact);
		if (gain > minGain) {
			kept.push({ ...fact, gain });
		}
	}
	return kept;
}

/** Distribute each kept fact's gain over the tokens it produced. */
export function communicationTokenCredit(
	facts: readonly FilteredFact[],
	totalTokens: number,
): readonly number[] {
	if (!Number.isInteger(totalTokens) || totalTokens < 0) {
		throw new TokenCreditError("totalTokens must be a non-negative integer");
	}
	const credit = new Array<number>(totalTokens).fill(0);
	for (const fact of facts) {
		if (fact.tokenIds.length === 0) {
			continue;
		}
		const share = fact.gain / fact.tokenIds.length;
		for (const token of fact.tokenIds) {
			if (!Number.isInteger(token) || token < 0 || token >= totalTokens) {
				throw new TokenCreditError(`token id ${token} outside [0, ${totalTokens})`);
			}
			credit[token] += share;
		}
	}
	return credit;
}

/** Weighted difference between scoring with and without the process rubric. */
export function rubricAdvantage(items: readonly RubricItem[]): RubricAdvantage {
	const seen = new Set<string>();
	let weightedDelta = 0;
	let active = 0;
	for (const item of items) {
		if (!item.id.trim()) {
			throw new TokenCreditError("rubric item id must not be empty");
		}
		if (seen.has(item.id)) {
			throw new TokenCreditError(`duplicate rubric item ${item.id}`);
		}
		seen.add(item.id);
		if (!Number.isFinite(item.weight) || item.weight < 0) {
			throw new TokenCreditError("rubric weight must be finite and non-negative");
		}
		if (!Number.isFinite(item.withRubricLogprob) || !Number.isFinite(item.withoutRubricLogprob)) {
			throw new TokenCreditError("rubric logprobs must be finite");
		}
		if (item.weight === 0) {
			continue;
		}
		active += 1;
		weightedDelta += item.weight * (item.withRubricLogprob - item.withoutRubricLogprob);
	}
	return { advantage: weightedDelta, weightedDelta, activeItems: active };
}

/** Add a rubric advantage uniformly across the response tokens. */
export function addRubricAdvantage(
	tokenAdvantages: readonly number[],
	advantage: number,
): readonly number[] {
	if (!Number.isFinite(advantage)) {
		throw new TokenCreditError("advantage must be finite");
	}
	if (tokenAdvantages.length === 0) {
		return [];
	}
	const share = advantage / tokenAdvantages.length;
	return tokenAdvantages.map((value) => {
		if (!Number.isFinite(value)) {
			throw new TokenCreditError("token advantage must be finite");
		}
		return value + share;
	});
}

/** Combine outcome advantage, dense communication credit, and rubric shaping. */
export function combineTokenAdvantages(input: {
	readonly outcomeAdvantage: number;
	readonly tokenCredit: readonly number[];
	readonly rubricAdvantage: number;
	readonly denseWeight?: number;
	readonly rubricWeight?: number;
}): readonly number[] {
	if (!Number.isFinite(input.outcomeAdvantage)) {
		throw new TokenCreditError("outcomeAdvantage must be finite");
	}
	const denseWeight = input.denseWeight ?? 1;
	const rubricWeight = input.rubricWeight ?? 1;
	if (!Number.isFinite(denseWeight) || denseWeight < 0) {
		throw new TokenCreditError("denseWeight must be finite and non-negative");
	}
	if (!Number.isFinite(rubricWeight) || rubricWeight < 0) {
		throw new TokenCreditError("rubricWeight must be finite and non-negative");
	}
	const shaped = addRubricAdvantage(input.tokenCredit, input.rubricAdvantage * rubricWeight);
	return shaped.map(
		(value) => input.outcomeAdvantage + denseWeight * value,
	);
}

function validateFact(fact: CommunicationFact): void {
	if (!fact.id.trim()) {
		throw new TokenCreditError("communication fact id must not be empty");
	}
	if (!fact.sender.trim() || !fact.receiver.trim()) {
		throw new TokenCreditError("communication endpoints must not be empty");
	}
	if (!Number.isFinite(fact.withFactLogprob) || !Number.isFinite(fact.withoutFactLogprob)) {
		throw new TokenCreditError("communication logprobs must be finite");
	}
	if (fact.tokenIds.some((token) => !Number.isInteger(token) || token < 0)) {
		throw new TokenCreditError("token ids must be non-negative integers");
	}
}
