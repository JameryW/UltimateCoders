/**
 * Cooperative swarm scheduling for s^3-style agent RL (ledger item #92).
 *
 * The swarm has no preset topology: each agent chooses its own work/communication
 * rhythm from three actions. This module models the local policy and the
 * deduplicated-coverage reward; it deliberately keeps the transport in-memory
 * and deterministic so a trainer can replay a round without a live model.
 */

export enum SwarmAction {
	WorkSolo = "work_solo",
	BroadcastFinding = "broadcast_finding",
	ReadInbox = "read_inbox",
}

export interface SwarmObservation {
	readonly agentId: string;
	readonly unread: number;
	readonly hasFinding: boolean;
	readonly soloSteps: number;
}

export interface SwarmEvent {
	readonly agentId: string;
	readonly step: number;
	readonly action: SwarmAction;
	readonly finding?: string;
}

export interface SwarmMetrics {
	readonly uniqueFindings: number;
	readonly totalFindings: number;
	readonly duplicateRate: number;
	readonly coverage: number;
	readonly reward: number;
}

export interface SwarmAgentSnapshot {
	readonly id: string;
	readonly inbox: readonly string[];
	readonly findings: readonly string[];
	readonly soloSteps: number;
}

interface AgentState {
	readonly id: string;
	inbox: string[];
	findings: Set<string>;
	soloSteps: number;
}

export class SwarmError extends Error {}

export function chooseAction(observation: SwarmObservation): SwarmAction {
	if (observation.unread > 0) return SwarmAction.ReadInbox;
	if (observation.hasFinding) return SwarmAction.BroadcastFinding;
	return SwarmAction.WorkSolo;
}

export function dedupeMetrics(findingsByAgent: ReadonlyMap<string, Iterable<string>>): SwarmMetrics {
	const all: string[] = [];
	const unique = new Set<string>();
	for (const findings of findingsByAgent.values()) {
		for (const finding of findings) {
			const normalised = finding.trim();
			if (!normalised) continue;
			all.push(normalised);
			unique.add(normalised);
		}
	}
	const total = all.length;
	const duplicateRate = total === 0 ? 0 : (total - unique.size) / total;
	const coverage = total === 0 ? 0 : unique.size / total;
	const reward = 0.6 * coverage + 0.4 * (1 - duplicateRate);
	return {
		uniqueFindings: unique.size,
		totalFindings: total,
		duplicateRate,
		coverage,
		reward,
	};
}

export class S3Swarm {
	private readonly agents = new Map<string, AgentState>();
	private stepIndex = 0;

	constructor(agentIds: readonly string[]) {
		if (agentIds.length === 0) throw new SwarmError("at least one agent is required");
		for (const agentId of agentIds) {
			const id = agentId.trim();
			if (!id) throw new SwarmError("agent id must not be empty");
			if (this.agents.has(id)) throw new SwarmError(`duplicate agent: ${id}`);
			this.agents.set(id, { id, inbox: [], findings: new Set(), soloSteps: 0 });
		}
	}

	snapshot(agentId: string): SwarmAgentSnapshot {
		const agent = this.require(agentId);
		return {
			id: agent.id,
			inbox: [...agent.inbox],
			findings: [...agent.findings].sort(),
			soloSteps: agent.soloSteps,
		};
	}

	step(agentId: string, discovered?: string): SwarmEvent {
		const agent = this.require(agentId);
		this.stepIndex += 1;
		const finding = discovered?.trim();
		if (agent.inbox.length > 0) {
			agent.inbox = [];
			return { agentId, step: this.stepIndex, action: SwarmAction.ReadInbox };
		}
		if (finding && !agent.findings.has(finding)) {
			agent.findings.add(finding);
			for (const peer of this.agents.values()) {
				if (peer.id !== agentId) peer.inbox.push(finding);
			}
			return {
				agentId,
				step: this.stepIndex,
				action: SwarmAction.BroadcastFinding,
				finding,
			};
		}
		agent.soloSteps += 1;
		return { agentId, step: this.stepIndex, action: SwarmAction.WorkSolo };
	}

	runRound(discoveries: Readonly<Record<string, readonly string[]>>): SwarmMetrics {
		const agentIds = [...this.agents.keys()].sort();
		for (const agentId of agentIds) {
			for (const finding of discoveries[agentId] ?? []) {
				this.step(agentId, finding);
			}
		}
		const findings = new Map<string, string[]>();
		for (const agent of this.agents.values()) findings.set(agent.id, [...agent.findings]);
		return dedupeMetrics(findings);
	}

	metrics(): SwarmMetrics {
		const findings = new Map<string, string[]>();
		for (const agent of this.agents.values()) findings.set(agent.id, [...agent.findings]);
		return dedupeMetrics(findings);
	}

	reset(): void {
		this.stepIndex = 0;
		for (const agent of this.agents.values()) {
			agent.inbox = [];
			agent.findings.clear();
			agent.soloSteps = 0;
		}
	}

	private require(agentId: string): AgentState {
		const agent = this.agents.get(agentId.trim());
		if (!agent) throw new SwarmError(`unknown agent: ${agentId}`);
		return agent;
	}
}
