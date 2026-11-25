/**
 * TRACE (#20) — turn-level credit assignment.
 *
 * Reference algorithm: TRACE (arXiv 2607.13988). For each turn k, the answer
 * likelihood is converted into a positive distance d_k = -log(p_k) + 0.1,
 * a value V(S_k) = log(d_0 / d_k), and a temporal difference
 * delta_k = log(d_k / d_{k+1}). A truncated K-step TD average produces the
 * dense turn credit c_k; a terminal reward bonus is added only when the
 * truncation window reaches the final turn. The final token advantage mixes
 * the sparse outcome advantage with the weighted turn reward.
 *
 * Indexing follows Algorithm 1: `turnLikelihoods[k]` is the frozen-model
 * answer likelihood at prefix state `S_k`, so an input of `T + 1` likelihoods
 * describes `T` tool transitions and yields `T` turn credits (`k = 0..T-1`).
 *
 * The module is pure: no I/O, no clock, no randomness, and no mutation.
 */

/** Dense-credit truncation horizon. */
export const K = 3;

/** TD discount factor. */
export const GAMMA = 0.8;

/** Terminal reward coefficient. */
export const LAMBDA_TERM = 2.0;

/** Outcome-advantage coefficient in the final token advantage. */
export const ALPHA_OUT = 1.0;

/** Turn-reward coefficient in the final token advantage. */
export const ALPHA_TURN = 0.2;

/** Reward baseline epsilon added to -log(likelihood). */
export const REWARD_BASELINE = 0.1;

/** Per-turn credit record. */
export interface TurnCredit {
	/** 0-based turn index. */
	turn: number;
	/** V(S_turn) = log(d_0 / d_turn). */
	logRatio: number;
	/** delta_turn = log(d_turn / d_(turn+1)). */
	delta: number;
	/** Truncated K-step TD average of deltas (dense turn credit). */
	credit: number;
}

/** Options for {@link computeTurnCredits} and {@link applyTraceCredit}. */
export interface TraceCreditOptions {
	/** Dense-credit truncation horizon; 0 disables dense TD. Defaults to {@link K}. */
	K?: number;
	/** TD discount factor in [0, 1]. Defaults to {@link GAMMA}. */
	gamma?: number;
	/** Positive reward baseline added to -log(likelihood). Defaults to {@link REWARD_BASELINE}. */
	baseline?: number;
	/** Terminal reward coefficient. Defaults to {@link LAMBDA_TERM}. */
	lambdaTerm?: number;
	/** Outcome-advantage coefficient. Defaults to {@link ALPHA_OUT}. */
	alphaOut?: number;
	/** Turn-reward coefficient. Defaults to {@link ALPHA_TURN}. */
	alphaTurn?: number;
}

interface ResolvedOptions {
	horizon: number;
	gamma: number;
	baseline: number;
	lambdaTerm: number;
	alphaOut: number;
	alphaTurn: number;
}

function requireFinite(name: string, value: number): number {
	if (!Number.isFinite(value)) throw new RangeError(`${name} must be a finite number, received ${String(value)}`);
	return value;
}

function requireNonNegative(name: string, value: number): number {
	requireFinite(name, value);
	if (value < 0) throw new RangeError(`${name} must be >= 0, received ${String(value)}`);
	return value;
}

function resolveOptions(options: TraceCreditOptions | undefined): ResolvedOptions {
	const resolved: ResolvedOptions = {
		horizon: options?.K ?? K,
		gamma: options?.gamma ?? GAMMA,
		baseline: options?.baseline ?? REWARD_BASELINE,
		lambdaTerm: options?.lambdaTerm ?? LAMBDA_TERM,
		alphaOut: options?.alphaOut ?? ALPHA_OUT,
		alphaTurn: options?.alphaTurn ?? ALPHA_TURN,
	};
	if (!Number.isInteger(resolved.horizon) || resolved.horizon < 0) {
		throw new RangeError(`K must be a non-negative integer, received ${String(resolved.horizon)}`);
	}
	requireFinite("gamma", resolved.gamma);
	if (resolved.gamma < 0 || resolved.gamma > 1) {
		throw new RangeError(`gamma must be in [0, 1], received ${String(resolved.gamma)}`);
	}
	if (!Number.isFinite(resolved.baseline) || resolved.baseline <= 0) {
		throw new RangeError(`baseline must be a positive finite number, received ${String(resolved.baseline)}`);
	}
	requireNonNegative("lambdaTerm", resolved.lambdaTerm);
	requireNonNegative("alphaOut", resolved.alphaOut);
	requireNonNegative("alphaTurn", resolved.alphaTurn);
	return resolved;
}

function validateLikelihood(likelihood: number, index: number): number {
	if (!Number.isFinite(likelihood) || likelihood <= 0 || likelihood > 1) {
		throw new RangeError(
			`turnLikelihoods[${index}] must be a finite number in (0, 1], received ${String(likelihood)}`,
		);
	}
	return likelihood;
}

/**
 * Truncated K-step TD average:
 * `c_k = sum_{u=k}^{min(k+K-1, T-1)} gamma^(u-k) * delta_u / Z_k`,
 * with `Z_k = sum gamma^(u-k)`. `K = 0` disables dense TD (`c_k = 0`).
 */
function truncatedTdCredit(deltas: readonly number[], turn: number, horizon: number, gamma: number): number {
	if (horizon === 0) return 0;
	const last = Math.min(turn + horizon - 1, deltas.length - 1);
	let weighted = 0;
	let normalizer = 0;
	for (let u = turn; u <= last; u++) {
		const weight = gamma ** (u - turn);
		weighted += weight * deltas[u];
		normalizer += weight;
	}
	return normalizer === 0 ? 0 : weighted / normalizer;
}

function safeLogRatio(numerator: number, denominator: number): number {
	const ratio = numerator / denominator;
	return Number.isFinite(ratio) && ratio > 0 ? Math.log(ratio) : Math.log(numerator) - Math.log(denominator);
}

/**
 * Compute turn-level credits from answer-ready prefix likelihoods.
 *
 * `turnLikelihoods[k]` is the frozen-model answer likelihood at state `S_k`
 * and must be a finite likelihood in (0, 1]. With `T` tool transitions the
 * array has `T + 1` entries and the result has `T` records (`turn = 0..T-1`),
 * each carrying `delta_k = log(d_k / d_{k+1})`. Inputs shorter than two
 * states (empty or a single prefix) return an empty array. Invalid
 * likelihoods and invalid options throw `RangeError`/`TypeError` rather than
 * producing NaN.
 */
export function computeTurnCredits(
	turnLikelihoods: readonly number[],
	options?: TraceCreditOptions,
): TurnCredit[] {
	if (!Array.isArray(turnLikelihoods)) throw new TypeError("turnLikelihoods must be an array");
	const resolved = resolveOptions(options);

	if (turnLikelihoods.length < 2) {
		for (let index = 0; index < turnLikelihoods.length; index++) validateLikelihood(turnLikelihoods[index], index);
		return [];
	}

	const distances = turnLikelihoods.map((likelihood, index) => {
		validateLikelihood(likelihood, index);
		return -Math.log(likelihood) + resolved.baseline;
	});

	const deltas: number[] = [];
	for (let turn = 0; turn < distances.length - 1; turn++) {
		deltas.push(safeLogRatio(distances[turn], distances[turn + 1]));
	}

	const credits: TurnCredit[] = [];
	for (let turn = 0; turn < deltas.length; turn++) {
		credits.push({
			turn,
			logRatio: safeLogRatio(distances[0], distances[turn]),
			delta: deltas[turn],
			credit: truncatedTdCredit(deltas, turn, resolved.horizon, resolved.gamma),
		});
	}
	return credits;
}

/**
 * Mix outcome and turn-level signals into one advantage per turn:
 *
 * `A_k = alphaOut * A_out + alphaTurn * (c_k + terminal_k)`
 *
 * where `terminal_k = lambdaTerm * gamma^(T-k) * A_out` when the truncation
 * window from turn k reaches the final turn, and 0 otherwise. The exponent
 * `T-k` treats final-answer generation as one additional transition after the
 * last tool observation (paper Eq. 10), so the final tool turn is discounted
 * by one step before receiving terminal-outcome fill.
 *
 * For convenience the first argument may be either a likelihood array (the
 * credits are computed internally with the same options) or a precomputed
 * `TurnCredit[]`.
 */
export function applyTraceCredit(
	turnLikelihoods: readonly number[],
	outcomeAdvantage: number,
	options?: TraceCreditOptions,
): number[];
export function applyTraceCredit(
	turnCredits: readonly TurnCredit[],
	outcomeAdvantage: number,
	options?: TraceCreditOptions,
): number[];
export function applyTraceCredit(
	input: readonly number[] | readonly TurnCredit[],
	outcomeAdvantage: number,
	options: TraceCreditOptions = {},
): number[] {
	if (!Array.isArray(input)) throw new TypeError("turnLikelihoods/turnCredits must be an array");
	requireFinite("outcomeAdvantage", outcomeAdvantage);

	const resolved = resolveOptions(options);
	const credits: readonly TurnCredit[] =
		input.length > 0 && typeof input[0] === "number"
			? computeTurnCredits(input as readonly number[], options)
			: (input as readonly TurnCredit[]);

	const totalTurns = credits.length;
	if (totalTurns === 0) return [];

	const advantages: number[] = [];
	for (let turn = 0; turn < totalTurns; turn++) {
		const credit = credits[turn];
		if (credit === null || typeof credit !== "object" || !Number.isFinite(credit.credit)) {
			throw new TypeError(`turnCredits[${turn}] must contain a finite credit`);
		}
		const windowEnd = resolved.horizon === 0 ? turn : Math.min(turn + resolved.horizon - 1, totalTurns - 1);
		const terminal =
			windowEnd === totalTurns - 1
				? resolved.lambdaTerm * resolved.gamma ** (totalTurns - turn) * outcomeAdvantage
				: 0;
		advantages.push(resolved.alphaOut * outcomeAdvantage + resolved.alphaTurn * (credit.credit + terminal));
	}
	return advantages;
}
