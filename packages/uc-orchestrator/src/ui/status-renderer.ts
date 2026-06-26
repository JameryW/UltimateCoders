/**
 * StatusRenderer — abstract interface for rendering connection/task status.
 *
 * Current implementation uses ui.setStatus() (footer text).
 * When OMP implements setFooter(), swap the implementation without changing callers.
 *
 * ponytail: setStatus bridge, upgrade to setFooter when OMP implements it
 */

import type { ExtensionUIContext } from "@oh-my-pi/pi-coding-agent";

export interface StatusRenderer {
	setField(key: string, text: string | undefined): void;
}

/**
 * setStatus-based implementation — writes to OMP footer status bar.
 * Multiple keys coexist (each extension gets its own slot).
 */
export class FooterStatusRenderer implements StatusRenderer {
	private ui: ExtensionUIContext;

	constructor(ui: ExtensionUIContext) {
		this.ui = ui;
	}

	setField(key: string, text: string | undefined): void {
		this.ui.setStatus(`uc-${key}`, text);
	}
}
