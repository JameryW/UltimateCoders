/**
 * Clipboard write for UC overlays (yank keys).
 *
 * The vendor has copyToClipboard (oh-my-pi coding-agent utils/clipboard.ts) but
 * doesn't export it from the package index, and it's a submodule — a submodule
 * commit + pointer bump for one export isn't worth it, so this self-contained
 * version mirrors its approach (OSC52 escape to stdout, proven safe mid-TUI —
 * the vendor's own controllers write escapes to stdout the same way).
 */

import { execFileSync } from "node:child_process";

/** Write text to the system clipboard. Returns true on success. */
export function copyText(text: string): boolean {
	// darwin: pbcopy has a verifiable exit code; Terminal.app lacks OSC52, so
	// pbcopy is the only reliable path there.
	if (process.platform === "darwin") {
		try {
			execFileSync("pbcopy", [], { input: text, stdio: ["pipe", "ignore", "ignore"] });
			return true;
		} catch {
			return false;
		}
	}
	// ponytail ceiling: elsewhere OSC52 (iTerm2/WezTerm/xterm/mlterm/…).
	// Fire-and-forget (no ACK). No native fallback (xclip/wl-copy) — add if
	// users on non-OSC52 Linux terminals need it.
	if (process.stdout.isTTY) {
		process.stdout.write(`\x1b]52;c;${Buffer.from(text).toString("base64")}\x07`);
		return true;
	}
	return false;
}
