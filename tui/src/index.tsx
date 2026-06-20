/**
 * UltimateCoders TUI - Ink/React terminal UI with CJK/IME support.
 *
 * Entry point: renders the App component using Ink.
 * exitOnCtrlC: false — App handles Ctrl+C with confirmation dialog.
 */
import React from 'react';
import {render} from 'ink';
import App from './components/App.js';

const {waitUntilExit} = render(<App />, {exitOnCtrlC: false});

waitUntilExit().then(() => {
  process.exit(0);
});
