/**
 * SINKFLEX-RL + MAGPIE + PTA-IRT (#54): long-horizon RL accounting.
 *
 * SINKFLEX-RL keeps the attention sink as a differentiable scalar
 * (`alpha_sink = sigmoid(ell - s_eta)`, `O_sink = alpha_sink * O_std`) so a
 * FlexAttention kernel can train long tool-call trajectories without
 * materialising an O(n^2) matrix. MAGPIE aggregates role-specific preference
 * rewards monotonically, and PTA-IRT folds privileged trace evidence into a
 * cheap one-parameter ability estimate.
 *
 * Pure deterministic bookkeeping. No model or I/O.
 */

/** Stable sigmoid that does not overflow for large negative inputs. */
export function sigmoid(value: number): number {
	if (!Number.isFinite(value)) throw new RangeError("sigmoid input must be finite");
	if (value >= 0) return 1 / (1 + Math.exp(-value));
	const exp = Math.exp(value);
	return exp / (1 + exp);
}

export interface SinkFlexConfig {
	/** Sink logit offset (`s_eta`). Defaults to 0. */
	readonly sinkEta?: number;
}

export interface SinkAttentionOutput {
	readonly alphaSink: number;
	readonly output: number;
	readonly sinkLogit: number;
}

/** Evaluate the zero-value sink identity for one output vector summary. */
export function sinkFlexAttention(
	ell: number,
	stdOutput: number,
	config: SinkFlexConfig = {},
): SinkAttentionOutput {
	if (!Number.isFinite(ell)) throw new RangeError("ell must be finite");
	if (!Number.isFinite(stdOutput)) throw new RangeError("stdOutput must be finite");
	const sinkEta = config.sinkEta ?? 0;
	if (!Number.isFinite(sinkEta)) throw new RangeError("sinkEta must be finite");
	const sinkLogit = ell - sinkEta;
	const alphaSink = sigmoid(sinkLogit);
	return {
		alphaSink,
		output: alphaSink * stdOutput,
		sinkLogit,
	};
}

/** Published peak-memory reduction from the SINKFLEX-RL evaluation. */
export const SINKFLEX_PUBLISHED_PEAK_REDUCTION = 0.197;

export interface MemoryEstimate {
	readonly baselineGb: number;
	readonly optimizedGb: number;
	readonly savedGb: number;
	readonly reduction: number;
}

/** Compare two measured peak-memory values. */
export function memoryEstimate(baselineGb: number, optimizedGb: number): MemoryEstimate {
	if (!Number.isFinite(baselineGb) || baselineGb <= 0) {
		throw new RangeError("baselineGb must be positive and finite");
	}
	if (!Number.isFinite(optimizedGb) || optimizedGb < 0) {
		throw new RangeError("optimizedGb must be non-negative and finite");
	}
	const savedGb = baselineGb - optimizedGb;
	return {
		baselineGb,
		optimizedGb,
		savedGb,
		reduction: savedGb / baselineGb,
	};
}

export interface RolePreference {
	readonly role: string;
	readonly reward: number;
	/** Relative bargaining weight; defaults to 1. */
	readonly weight?: number;
}

export interface NashAggregate {
	readonly value: number;
	readonly weights: Readonly<Record<string, number>>;
	readonly roles: readonly string[];
}

/**
 * MAGPIE's monotone aggregator. A role with a larger reward can never lower the
 * aggregate, and the normalised weights sum to one.
 */
export function magpieAggregate(preferences: readonly RolePreference[]): NashAggregate {
	if (preferences.length === 0) throw new Error("at least one role preference is required");
	const seen = new Set<string>();
	const weights: Record<string, number> = {};
	let totalWeight = 0;
	for (const preference of preferences) {
		if (!preference.role.trim()) throw new Error("role must not be empty");
		if (seen.has(preference.role)) {
			throw new Error(`duplicate role ${preference.role}`);
		}
		seen.add(preference.role);
		if (!Number.isFinite(preference.reward)) {
			throw new RangeError("role reward must be finite");
		}
		const weight = preference.weight ?? 1;
		if (!Number.isFinite(weight) || weight <= 0) {
			throw new RangeError("role weight must be positive and finite");
		}
		weights[preference.role] = weight;
		totalWeight += weight;
	}
	let value = 0;
	for (const preference of preferences) {
		const weight = weights[preference.role] / totalWeight;
		weights[preference.role] = weight;
		value += weight * preference.reward;
	}
	return {
		value,
		weights,
		roles: [...seen].sort(),
	};
}

/** One privileged trace observation for PTA-IRT. */
export interface PtaResponse {
	readonly difficulty: number;
	readonly passed: boolean;
	/** Privileged trace confidence; defaults to 1. */
	readonly traceWeight?: number;
}

export interface PtaEstimate {
	readonly ability: number;
	readonly evidence: number;
	readonly standardError: number;
}

/**
 * One-parameter logistic update with trace-weighted evidence. The estimate is
 * intentionally cheap: it is a running MAP point, not a full IRT fit.
 */
export function ptaIrtEstimate(
	responses: readonly PtaResponse[],
	priorAbility = 0,
	priorWeight = 1,
	learningRate = 0.5,
): PtaEstimate {
	if (!Number.isFinite(priorAbility)) throw new RangeError("priorAbility must be finite");
	if (!Number.isFinite(priorWeight) || priorWeight < 0) {
		throw new RangeError("priorWeight must be non-negative and finite");
	}
	if (!Number.isFinite(learningRate) || learningRate <= 0) {
		throw new RangeError("learningRate must be positive and finite");
	}
	let ability = priorAbility;
	let evidence = priorWeight;
	for (const response of responses) {
		if (!Number.isFinite(response.difficulty)) {
			throw new RangeError("response difficulty must be finite");
		}
		const traceWeight = response.traceWeight ?? 1;
		if (!Number.isFinite(traceWeight) || traceWeight < 0) {
			throw new RangeError("traceWeight must be non-negative and finite");
		}
		const probability = sigmoid(ability - response.difficulty);
		const observed = response.passed ? 1 : 0;
		ability += learningRate * traceWeight * (observed - probability);
		evidence += traceWeight;
	}
	return {
		ability,
		evidence,
		standardError: 1 / Math.sqrt(Math.max(evidence, 1e-12)),
	};
}

/** Convert the latent ability into a bounded benchmark score. */
export function ptaBenchmarkScore(estimate: PtaEstimate): number {
	return sigmoid(estimate.ability);
}

/** Convenience facade used by the training module. */
export class SinkFlexMagpieTrainer {
	attention(ell: number, stdOutput: number, config: SinkFlexConfig = {}): SinkAttentionOutput {
		return sinkFlexAttention(ell, stdOutput, config);
	}

	aggregate(preferences: readonly RolePreference[]): NashAggregate {
		return magpieAggregate(preferences);
	}

	estimate(responses: readonly PtaResponse[], priorAbility = 0): PtaEstimate {
		return ptaIrtEstimate(responses, priorAbility);
	}
}
