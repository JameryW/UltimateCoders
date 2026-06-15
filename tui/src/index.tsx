/**
 * UltimateCoders TUI - Ink/React terminal UI with CJK/IME support.
 *
 * Entry point: renders the App component using Ink.
 */
import React from 'react';
import {render} from 'ink';
import App from './components/App.js';

const {waitUntilExit} = render(<App />);

waitUntilExit().then(() => {
  process.exit(0);
});
