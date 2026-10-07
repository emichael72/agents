// Launches the Vercel Agent while keeping the repository's command: node vercel/agent.ts.
// The agent itself is the vercelagent package next to this file.

import { main } from './vercelagent/index.ts';

process.exitCode = await main();
