#!/usr/bin/env node
/**
 * Build script for UltimateCoders TUI.
 *
 * Uses esbuild with an alias to replace react-devtools-core
 * with an empty stub, avoiding the runtime import error.
 */
import esbuild from 'esbuild';
import {resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

esbuild.build({
  entryPoints: ['src/index.tsx'],
  bundle: true,
  outfile: 'dist/cli.js',
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  alias: {
    'react-devtools-core': resolve(__dirname, 'src/stubs/react-devtools-core.js'),
  },
  banner: {
    js: `#!/usr/bin/env node
// Built by esbuild for UltimateCoders TUI
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';
const require = createRequire(import.meta.url);
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
`,
  },
}).then(() => {
  console.log('Build complete: dist/cli.js');
}).catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});
