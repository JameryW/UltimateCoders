/**
 * AgentStepper interactive-debugging state machine (#71).
 *
 * A deterministic event/breakpoint controller with a shadow micro-commit after
 * every tool call. The real repository is untouched: commits are logical
 * records that a debugger can replay or diff. Live hot edits are bounded
 * in-memory overrides; applying them to a running model/tool is the caller's
 * responsibility.
 */

export type AgentEventKind =
	| "before_llm_query"
	| "after_llm_query"
	| "before_tool_call"
	| "after_tool_call";

export interface AgentEvent {
	seq: number;
	kind: AgentEventKind;
	summary: string;
	diff?: string;
}

export type StepperState = "paused" | "stepping" | "running";

export interface MicroCommit {
	id: string;
	parent: string | null;
	eventSeq: number;
	summary: string;
	diff: string;
}

export interface IngestResult {
	state: StepperState;
	commit: MicroCommit | null;
	pausedByBreakpoint: boolean;
}

export interface HotEdit {
	key: string;
	value: string;
}

export class AgentStepperError extends Error {}

const DEFAULT_MAX_EVENTS = 1024;
const DEFAULT_MAX_COMMITS = 1024;
const DEFAULT_MAX_OVERRIDES = 64;

export class AgentStepper {
	private readonly maxEvents: number;
	private readonly maxCommits: number;
	private readonly maxOverrides: number;
	private stateValue: StepperState = "paused";
	private readonly eventList: AgentEvent[] = [];
	private readonly commitList: MicroCommit[] = [];
	private readonly breakpointSet = new Set<AgentEventKind>();
	private readonly overrideMap = new Map<string, string>();

	constructor(
		maxEvents = DEFAULT_MAX_EVENTS,
		maxCommits = DEFAULT_MAX_COMMITS,
		maxOverrides = DEFAULT_MAX_OVERRIDES,
	) {
		if (maxEvents <= 0 || maxCommits <= 0 || maxOverrides <= 0) {
			throw new AgentStepperError("capacities must be positive");
		}
		this.maxEvents = maxEvents;
		this.maxCommits = maxCommits;
		this.maxOverrides = maxOverrides;
	}

	get state(): StepperState {
		return this.stateValue;
	}

	get events(): readonly AgentEvent[] {
		return this.eventList;
	}

	get commits(): readonly MicroCommit[] {
		return this.commitList;
	}

	get breakpoints(): readonly AgentEventKind[] {
		return [...this.breakpointSet].sort();
	}

	get overrides(): readonly HotEdit[] {
		return [...this.overrideMap.entries()]
			.sort(([left], [right]) => left.localeCompare(right))
			.map(([key, value]) => ({ key, value }));
	}

	addBreakpoint(kind: AgentEventKind): boolean {
		if (this.breakpointSet.has(kind)) {
			return false;
		}
		this.breakpointSet.add(kind);
		return true;
	}

	removeBreakpoint(kind: AgentEventKind): boolean {
		return this.breakpointSet.delete(kind);
	}

	resume(): void {
		this.stateValue = "running";
	}

	pause(): void {
		this.stateValue = "paused";
	}

	step(): boolean {
		if (this.stateValue !== "paused") {
			return false;
		}
		this.stateValue = "stepping";
		return true;
	}

	hotEdit(key: string, value: string): void {
		if (!key.trim()) {
			throw new AgentStepperError("hot-edit key must not be empty");
		}
		if (!this.overrideMap.has(key) && this.overrideMap.size >= this.maxOverrides) {
			throw new AgentStepperError(
				`override capacity of ${this.maxOverrides} reached`,
			);
		}
		this.overrideMap.set(key, value);
	}

	ingest(event: AgentEvent): IngestResult {
		if (!event.summary.trim()) {
			throw new AgentStepperError("event summary must not be empty");
		}
		if (this.eventList.length >= this.maxEvents) {
			throw new AgentStepperError(`event capacity of ${this.maxEvents} reached`);
		}
		const pausedByBreakpoint = this.breakpointSet.has(event.kind);
		let commit: MicroCommit | null = null;
		if (event.kind === "after_tool_call") {
			if (this.commitList.length >= this.maxCommits) {
				throw new AgentStepperError(
					`commit capacity of ${this.maxCommits} reached`,
				);
			}
			commit = {
				id: `shadow-${this.commitList.length + 1}-${event.seq}`,
				parent: this.commitList.at(-1)?.id ?? null,
				eventSeq: event.seq,
				summary: event.summary,
				diff: event.diff ?? "",
			};
		}
		this.eventList.push(event);
		if (commit) {
			this.commitList.push(commit);
		}
		if (pausedByBreakpoint) {
			this.stateValue = "paused";
		} else if (this.stateValue === "stepping") {
			this.stateValue = "paused";
		}
		return { state: this.stateValue, commit, pausedByBreakpoint };
	}
}
