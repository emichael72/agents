// Local tools, discovered from the shared agents/tools folder.
//
// Each <tool>/tool.json manifest (the same shape MCPAgent's server reads) describes a command, its
// fixed args and its parameters. This module turns every manifest into an AI SDK tool: the JSON
// schema is built from the params and converted to zod (which validates the model's arguments),
// and calling the tool runs the command as a child process.
import { execFile } from 'node:child_process';
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { tool, type ToolSet } from 'ai';
import { z } from 'zod';

/**
 * Find the repository root: the nearest folder above `start` that holds pyproject.toml.
 * @param start The folder to search from.
 * @returns The root folder.
 */
function findRepoRoot(start: string): string {
  for (let dir = start; ; dir = path.dirname(dir)) {
    if (existsSync(path.join(dir, 'pyproject.toml'))) return dir;
    if (path.dirname(dir) === dir) throw new Error(`No pyproject.toml above ${start}; is it inside the agents repository?`);
  }
}

/** The repository root, found from this file rather than by counting ../ levels. */
export const REPO_ROOT = findRepoRoot(import.meta.dirname);
export const TOOLS_DIR = path.join(REPO_ROOT, 'tools');
const SCRIPT_TIMEOUT_MS = 30_000;
const AGENT_NAME = 'Vercel Agent'; // Lets tools such as pr and pr_gate say which agent ran them

const execFileAsync = promisify(execFile);

type Param = { name: string; type?: string; description?: string; style?: 'flag' | 'positional'; required?: boolean };
type JSONSchemaInput = Parameters<typeof z.fromJSONSchema>[0];
type Manifest = { description?: string | string[]; command: string; args?: string[]; env?: Record<string, string>; params?: Param[]; timeout?: number };

/** Run a tool command from the tools folder and return its output. */
export async function runScript(command: string[], env: Record<string, string> = {},
                                timeoutMs = SCRIPT_TIMEOUT_MS): Promise<string> {
  const [file, ...args] = command;
  try {
    // Async: the event loop stays free while the child process runs.
    const { stdout } = await execFileAsync(file, args, {
      cwd: TOOLS_DIR,
      env: { ...process.env, AGENT_NAME, ...env },
      timeout: timeoutMs,
    });
    return stdout.trim();
  } catch (error) {
    const { stdout, stderr, killed } = error as { stdout?: string; stderr?: string; killed?: boolean };
    if (killed) throw new Error(`${args[0]} timed out after ${timeoutMs / 1000}s`);
    // A thrown error becomes a tool-error the model sees, like MCPAgent's isError results.
    throw new Error(stdout?.trim() || stderr?.trim() || String(error));
  }
}

/** JSON schema for a manifest's params, built the same way as MCPAgent's server. */
export function inputSchema(manifest: Manifest) {
  const params = manifest.params ?? [];
  return {
    type: 'object' as const,
    properties: Object.fromEntries(params.map((p) => [p.name, { type: p.type ?? 'string', description: p.description ?? '' }])),
    required: params.filter((p) => p.required !== false).map((p) => p.name),
    additionalProperties: false,
  };
}

/** Command line for one call: command, fixed args, then each given param as a flag or positional. */
export function buildArgv(manifest: Manifest, input: Record<string, unknown>): string[] {
  const argv = [manifest.command, ...(manifest.args ?? [])];
  for (const param of manifest.params ?? []) {
    const value = input[param.name];
    if (value === undefined || value === null) continue; // Optional and omitted: the script uses its own default
    argv.push(...(param.style === 'positional' ? [String(value)] : [`--${param.name}`, String(value)]));
  }
  return argv;
}

/** One tool per <tool>/tool.json under toolsDir; the folder name is the tool name. */
export function loadTools(toolsDir = TOOLS_DIR): ToolSet {
  const tools: ToolSet = {};
  for (const name of readdirSync(toolsDir).sort()) {
    const manifestPath = path.join(toolsDir, name, 'tool.json');
    if (!existsSync(manifestPath)) continue;
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as Manifest;
    tools[name] = tool({
      description: Array.isArray(manifest.description) ? manifest.description.join(' ') : manifest.description,
      inputSchema: z.fromJSONSchema(inputSchema(manifest) as JSONSchemaInput) as z.ZodType<Record<string, unknown>>,
      execute: (input) => runScript(buildArgv(manifest, input), manifest.env, (manifest.timeout ?? SCRIPT_TIMEOUT_MS / 1000) * 1000),
    });
  }
  return tools;
}

export const localTools = loadTools();

/**
 * Make tool calls run one at a time. The AI SDK starts each tool as soon as its call arrives,
 * with no sequential mode; MCPAgent's server rejects overlapping calls ("Busy").
 */
export function oneAtATime<TOOLS extends ToolSet>(tools: TOOLS): TOOLS {
  let queue: Promise<unknown> = Promise.resolve();
  const wrapped: ToolSet = {};
  for (const [name, original] of Object.entries(tools)) {
    const execute = original.execute;
    wrapped[name] = !execute ? original : {
      ...original,
      execute: (input: unknown, options: unknown) => {
        const run = queue.then(() => execute(input as never, options as never));
        queue = run.catch(() => undefined);
        return run;
      },
    };
  }
  return wrapped as TOOLS;
}
