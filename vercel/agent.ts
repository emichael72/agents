// An AI SDK (Vercel) terminal agent, the counterpart of the pydantic agent and MCPAgent's client.
//
// Same model profiles (agents/context/models.json), instructions (agents/context/instructions.json)
// and tools (agents/tools) as the other two agents. The agent loop
// (call the model, run requested tools, send results back, repeat until it answers) is done by
// the AI SDK's ToolLoopAgent; this file only builds the agent and renders its stream in the terminal.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { parseArgs, styleText } from 'node:util';
import { createMCPClient } from '@ai-sdk/mcp';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { isStepCount, ToolLoopAgent, type LanguageModel, type ModelMessage, type ToolSet } from 'ai';
import { localTools, oneAtATime } from './tools.ts';

const MCP_URL = 'http://127.0.0.1:6275/'; // MCPAgent's server (python -m mcpagent.server)

// Instructions (system prompt) and model profiles, shared by all three agents
const CONTEXT_DIR = path.join(import.meta.dirname, '..', 'context');
const INSTRUCTIONS_FILE = path.join(CONTEXT_DIR, 'instructions.json');
const MODELS_FILE = path.join(CONTEXT_DIR, 'models.json');

// Roughly MCPAgent's max_tool_calls=8: each step is one model call plus the tools it requested.
const MAX_STEPS = 9;

const dim = (text: string) => styleText('dim', text);

type Profile = {
  name?: string; base_url: string; base_url_env?: string; model: string; model_env?: string;
  api_key_env?: string; api_key?: string; timeout?: number;
};
type Models = { default?: string; profiles?: Record<string, Profile> };
export type ModelSettings = { profile: string; name: string; baseURL: string; model: string; apiKey: string; timeout: number };

/** Read the shared model profiles file: "default" (a profile name) and "profiles" (by name). */
export function loadModels(file = MODELS_FILE): Models {
  return JSON.parse(readFileSync(file, 'utf8')) as Models;
}

/**
 * Pick a model profile and apply overrides, the same way as the other two agents.
 * Precedence: explicit overrides (command line), then the environment variables the profile names
 * (model_env, base_url_env), then the profile's own values. The API key is read only from the
 * profile's api_key_env (or its api_key fallback), so a key is never sent to another server.
 * @param models The model profiles, as returned by `loadModels`.
 * @param profile Profile name; undefined uses the profile named by "default".
 * @param overrides Optional model and baseURL for this run.
 * @returns The resolved settings.
 * @throws If the profile does not exist, lacks base_url or model, or its API key is not set.
 */
export function resolveModel(models: Models, profile?: string,
                             overrides: { model?: string; baseURL?: string } = {}): ModelSettings {
  const profiles = models.profiles ?? {};
  const name = profile ?? models.default ?? '';
  const settings = profiles[name];
  if (!settings) {
    throw new Error(`Unknown model profile '${name}' (available: ${Object.keys(profiles).join(', ') || 'none'}). Check the models file.`);
  }
  const missing = (['base_url', 'model'] as const).filter((key) => !settings[key]);
  if (missing.length) throw new Error(`Model profile '${name}' is missing ${missing.join(', ')}.`);
  const env = (variable?: string) => (variable ? process.env[variable] : undefined) || undefined;
  const apiKey = (env(settings.api_key_env) ?? settings.api_key ?? '').trim();
  if (!apiKey) throw new Error(`Set ${settings.api_key_env ?? 'an API key'} in the environment for the '${name}' profile.`);
  return {
    profile: name,
    name: settings.name ?? name,
    baseURL: overrides.baseURL || env(settings.base_url_env) || settings.base_url,
    model: overrides.model || env(settings.model_env) || settings.model,
    apiKey,
    timeout: settings.timeout ?? 60,
  };
}

/** Create a model on an OpenAI-compatible server (LM Studio, OpenAI, ...) from resolved settings. */
export function buildModel(settings: ModelSettings): LanguageModel {
  return createOpenAICompatible({ name: settings.profile, baseURL: settings.baseURL, apiKey: settings.apiKey })(settings.model);
}

/** Read the shared instructions file and join its "instructions" lines with newlines. */
export function loadInstructions(file = INSTRUCTIONS_FILE): string {
  return (JSON.parse(readFileSync(file, 'utf8')) as { instructions: string[] }).instructions.join('\n');
}

export function buildAgent(model: LanguageModel, tools: ToolSet, parallel = false, timeoutSeconds = 60) {
  return new ToolLoopAgent({
    model,
    timeout: { stepMs: timeoutSeconds * 1000 }, // one model call plus the tools it requested
    instructions: loadInstructions(),
    tools: parallel ? tools : oneAtATime(tools),
    stopWhen: isStepCount(MAX_STEPS),
  });
}

type Agent = ReturnType<typeof buildAgent>;
type Write = (text: string) => void;

/**
 * The terminal layout shared by the three agents: tool calls and results as dimmed lines while
 * they happen, and the model's text with exactly one blank line before and after it.
 */
export class Output {
  private inText = false; // A text block is open
  private pending = ''; // Trailing newlines held back until more text follows

  private readonly write: Write;

  /** @param write Prints raw text (stdout by default). */
  constructor(write: Write) {
    this.write = write;
  }

  /** Print a chunk of streamed model text. */
  text(chunk: string): void {
    if (!this.inText) {
      chunk = chunk.replace(/^\n+/, '');
      if (!chunk) return;
      this.write('\n'); // Blank line before the text
      this.inText = true;
    }
    const body = chunk.replace(/\n+$/, '');
    if (body) {
      this.write(this.pending + body);
      this.pending = chunk.slice(body.length);
    } else {
      this.pending += chunk;
    }
  }

  /** Print a whole line (a tool call or result), closing any open text block first. */
  line(text: string): void {
    this.end();
    this.write(dim(text) + '\n');
  }

  /** Close the open text block, if any: end its line and add the blank line after it. */
  end(): void {
    if (this.inText) this.write('\n\n');
    this.inText = false;
    this.pending = '';
  }
}

/**
 * Run one user turn, printing streamed text and tool activity.
 * @param agent The agent to run.
 * @param prompt The user's message.
 * @param history The messages of earlier turns.
 * @param options trace: print tool calls and results; write: where to print (stdout by default).
 * @returns The updated message history, including this turn.
 */
export async function ask(agent: Agent, prompt: string, history: ModelMessage[],
                          { trace = true, write = (text: string) => void process.stdout.write(text) }:
                            { trace?: boolean; write?: Write } = {}): Promise<ModelMessage[]> {
  const messages: ModelMessage[] = [...history, { role: 'user', content: prompt }];
  const output = new Output(write);
  // The AI SDK reports every call of a model response before their results; hold each call line
  // until its result arrives, so the two print together (as in the other agents).
  const pendingCalls = new Map<string, string>();
  const showCall = (toolCallId: string) => {
    const line = pendingCalls.get(toolCallId);
    if (line) output.line(line);
    pendingCalls.delete(toolCallId);
  };
  try {
    const result = await agent.stream({ messages });
    for await (const part of result.fullStream) {
      switch (part.type) {
        case 'text-delta':
          output.text(part.text);
          break;
        case 'tool-call':
          if (trace) pendingCalls.set(part.toolCallId, `→ ${part.toolName}(${JSON.stringify(part.input)})`);
          break;
        case 'tool-result':
          if (trace) {
            showCall(part.toolCallId);
            output.line(`← ${part.toolName}: ${readable(part.output)}`);
          }
          break;
        case 'tool-error':
          if (trace) {
            showCall(part.toolCallId);
            output.line(`✗ ${part.toolName}: ${readable(errorMessage(part.error))}`);
          }
          break;
        case 'error':
          throw part.error;
      }
    }
    return [...messages, ...(await result.response).messages];
  } finally {
    for (const line of pendingCalls.values()) output.line(line); // Calls that never got a result
    output.end();
  }
}

/**
 * Make a tool's output readable for the terminal.
 * @param output Plain text, an MCP result ({ content: [{ type: 'text', text }] }), or JSON text such
 *   as MCPAgent's {"status", "logs", "summary"} result or an {"error": ...} failure.
 * @returns The "logs" lines or the error message when the output is such JSON, else the text.
 */
export function readable(output: unknown): string {
  const content = (output as { content?: { type: string; text?: string }[] })?.content;
  if (Array.isArray(content)) return readable(content.map((item) => item.text ?? '').join('\n'));
  if (typeof output !== 'string') return JSON.stringify(output);
  let data: unknown;
  try {
    data = JSON.parse(output);
  } catch {
    return output;
  }
  if (data && typeof data === 'object' && 'logs' in data && Array.isArray(data.logs)) return data.logs.join('\n');
  if (data && typeof data === 'object' && 'error' in data) return readable(String(data.error));
  return output;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function chat(agent: Agent, prompt: string | undefined, trace: boolean, showHistory: boolean) {
  let history: ModelMessage[] = [];
  if (prompt !== undefined) {
    history = await ask(agent, prompt, history, { trace });
    if (showHistory) console.dir(history, { depth: null });
    return;
  }
  console.log('Ask me to use a tool. /history shows messages, /reset clears them, exit quits.');
  const rl = createInterface({ input: process.stdin, output: process.stdout, prompt: 'You > ' });
  try {
    rl.prompt();
    // Iterating queues lines that arrive while the agent is busy (e.g. piped input).
    for await (const raw of rl) {
      const line = raw.trim();
      if (['exit', 'quit', 'q'].includes(line.toLowerCase())) return;
      if (line === '/reset') {
        history = [];
        console.log('History cleared.');
      } else if (line === '/history') {
        console.dir(history, { depth: null });
      } else if (line) {
        try {
          history = await ask(agent, line, history, { trace });
        } catch (error) {
          console.error(styleText('red', `\nError: ${errorMessage(error)}`));
        }
      }
      rl.prompt();
    }
  } finally {
    rl.close();
  }
}

async function main(): Promise<number> {
  const { values } = parseArgs({
    options: {
      profile: { type: 'string' },
      local: { type: 'boolean', default: false },
      openai: { type: 'boolean', default: false },
      model: { type: 'string' },
      'base-url': { type: 'string' },
      mcp: { type: 'string' }, // URL, or "" for the default MCPAgent server
      prompt: { type: 'string' },
      history: { type: 'boolean', default: false },
      quiet: { type: 'boolean', default: false },
      parallel: { type: 'boolean', default: false },
      help: { type: 'boolean', short: 'h', default: false },
    },
  });
  if (values.help) {
    console.log(`Usage: node agent.ts [options]
  --profile NAME     Model profile from context/models.json (default: its "default")
  --local, --openai  Shortcuts for --profile local and --profile openai
  --model ID         Override the profile's model for this run
  --base-url URL     Override the profile's OpenAI-compatible base URL for this run
  --mcp URL          Use tools from an MCP server instead of the local tools folder (--mcp "" = ${MCP_URL})
  --prompt TEXT      Run one prompt and exit
  --history          With --prompt, print the message history
  --quiet            Hide tool calls and results
  --parallel         Run the tool calls from one model response concurrently`);
    return 0;
  }

  if ([values.profile, values.local, values.openai].filter(Boolean).length > 1) {
    console.error('Use only one of --profile, --local and --openai.');
    return 2;
  }

  console.log(); // Blank line before anything the agent prints
  const mcpUrl = values.mcp === undefined ? undefined : values.mcp || MCP_URL;
  let mcpClient: Awaited<ReturnType<typeof createMCPClient>> | undefined;
  try {
    if (mcpUrl) {
      mcpClient = await createMCPClient({ transport: { type: 'http', url: mcpUrl } }).catch((error) => {
        throw new Error(`Cannot connect to MCP server ${mcpUrl}: ${errorMessage(error)}`);
      });
    }
    const profile = values.profile ?? (values.local ? 'local' : values.openai ? 'openai' : undefined);
    const settings = resolveModel(loadModels(), profile, { model: values.model, baseURL: values['base-url'] });
    const tools = mcpClient ? await mcpClient.tools() : localTools;
    const agent = buildAgent(buildModel(settings), tools, values.parallel, settings.timeout);
    console.log(`${settings.name} model: ${settings.model} @ ${settings.baseURL}`);
    console.log(`Tools: ${mcpUrl ? `MCP server ${mcpUrl}` : Object.keys(tools).join(', ')} (${values.parallel ? 'parallel' : 'sequential'})`);
    await chat(agent, values.prompt, !values.quiet, values.history);
    return 0;
  } catch (error) {
    console.error(styleText('red', `Error: ${errorMessage(error)}`));
    return 1;
  } finally {
    await mcpClient?.close();
  }
}

if (import.meta.main) process.exitCode = await main();
