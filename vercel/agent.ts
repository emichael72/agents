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
import ora, { type Ora } from 'ora';
import { localTools, oneAtATime } from './tools.ts';

const MCP_URL = 'http://127.0.0.1:6275/'; // MCPAgent's server (python -m mcpagent.server)

// Instructions (system prompt) and model profiles, shared by all three agents
const CONTEXT_DIR = path.join(import.meta.dirname, '..', 'context');
const INSTRUCTIONS_FILE = path.join(CONTEXT_DIR, 'instructions.json');
const MODELS_FILE = path.join(CONTEXT_DIR, 'models.json');
const OUTPUT_FILE = path.join(CONTEXT_DIR, 'output.json'); // Terminal layout
const AGENT_FILE = path.join(CONTEXT_DIR, 'agent.json'); // Agent loop settings

// Tool calls per prompt, shared with the other agents (context/agent.json). Each step is one model
// call plus the tools it requested, so this allows at least max_tool_calls calls and a final answer.
// 0 means no limit: the loop then ends only when the model answers without calling a tool.
const AGENT_SETTINGS = JSON.parse(readFileSync(AGENT_FILE, 'utf8')) as
  { max_tool_calls?: number; memory_index?: string; save_on_exit?: boolean; names?: Record<string, string> };
const MAX_TOOL_CALLS = AGENT_SETTINGS.max_tool_calls ?? 8;
// The memory index (relative to the repository), loaded into the instructions
const MEMORY_INDEX = AGENT_SETTINGS.memory_index && path.join(CONTEXT_DIR, '..', AGENT_SETTINGS.memory_index);
const MAX_STEPS = MAX_TOOL_CALLS ? MAX_TOOL_CALLS + 1 : 0;

const gray = (text: string) => styleText('gray', text); // Everything except the model's answer

type Profile = {
  name?: string; base_url: string; base_url_env?: string; model: string; model_env?: string; model_auto?: boolean;
  api_key_env?: string; api_key?: string; timeout?: number;
};
type Models = { default?: string; profiles?: Record<string, Profile> };
export type ModelSettings = {
  profile: string; name: string; baseURL: string; model: string; apiKey: string; timeout: number;
  auto: boolean; // Ask the server which model is loaded (see loadedModel)
};

/** Read the shared model profiles file: "default" (a profile name) and "profiles" (by name). */
export function loadModels(file = MODELS_FILE): Models {
  return JSON.parse(readFileSync(file, 'utf8')) as Models;
}

/**
 * Pick a model profile and apply overrides, the same way as the other two agents.
 * Precedence: explicit overrides (command line), then the environment variables the profile names
 * (model_env, base_url_env), then the profile's own values; with model_auto, main() then asks the
 * server for its loaded model (loadedModel). The API key is read only from the
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
    auto: Boolean(settings.model_auto) && !overrides.model && !env(settings.model_env),
  };
}

/**
 * Ask an LM Studio server which model is loaded (its /api/v0/models lists each model's state).
 * @param baseURL The server's OpenAI-compatible base URL, e.g. http://boba:1234/v1.
 * @param apiKey Sent as a bearer token, for servers that check it.
 * @returns The first loaded language model's id, or undefined when none is loaded or the server
 *   cannot tell (not LM Studio, unreachable).
 */
export async function loadedModel(baseURL: string, apiKey = ''): Promise<string | undefined> {
  const root = baseURL.replace(/\/+$/, '').replace(/\/v1$/, '');
  try {
    const response = await fetch(`${root}/api/v0/models`, {
      headers: { Authorization: `Bearer ${apiKey}` }, signal: AbortSignal.timeout(3000),
    });
    const { data = [] } = await response.json() as { data?: { id: string; state?: string; type?: string }[] };
    return data.find((m) => m.state === 'loaded' && (m.type === 'llm' || m.type === 'vlm'))?.id;
  } catch {
    return undefined;
  }
}

/** Create a model on an OpenAI-compatible server (LM Studio, OpenAI, ...) from resolved settings. */
export function buildModel(settings: ModelSettings): LanguageModel {
  // includeUsage: streamed replies report their token counts (shown after each response)
  return createOpenAICompatible({ name: settings.profile, baseURL: settings.baseURL, apiKey: settings.apiKey,
                                  includeUsage: true })(settings.model);
}

/**
 * Read the shared instructions file.
 * @param file The JSON file (default: agents/context/instructions.json).
 * @param key Which lines to read: 'instructions', or 'on_exit' (the prompt sent before exit).
 * @returns Those lines, joined with newlines ('' if the file has none).
 */
export function loadInstructions(file = INSTRUCTIONS_FILE, key = 'instructions'): string {
  return ((JSON.parse(readFileSync(file, 'utf8')) as Record<string, string[] | undefined>)[key] ?? []).join('\n');
}

/**
 * The identity lines that open the instructions, naming the agent.
 * @param name The agent's name (context/agent.json's names); undefined when not configured.
 * @param file The instructions file.
 * @returns The lines with {name} filled in, and a blank line after them; '' without a name.
 */
export function identityText(name?: string, file = INSTRUCTIONS_FILE): string {
  const text = loadInstructions(file, 'identity');
  return name && text ? text.replaceAll('{name}', name) + '\n\n' : '';
}

/**
 * Whether a session may hold something to remember: a tool call, or more than one exchange.
 * @param history The session's messages.
 * @returns True when the model should be asked to save before exit.
 */
export function worthSaving(history: ModelMessage[]): boolean {
  const prompts = history.filter((message) => message.role === 'user').length;
  return history.some((message) => message.role === 'tool') || prompts > 1;
}

/**
 * The agents' memory, to append to their instructions: the topics in the memory index (kept by the
 * memory tool), so the model knows what it remembers without having to look.
 * @param index The index file (context/agent.json's memory_index); undefined when not configured.
 * @returns A paragraph listing the topics, or saying the memory is empty; '' without an index.
 */
export function memoryText(index?: string): string {
  if (!index) return '';
  let text = '';
  try {
    text = readFileSync(index, 'utf8');
  } catch {
    // No index yet: the memory is empty
  }
  const lines = text.split('\n').filter((line) => line.startsWith('- ')).map((line) => line.replaceAll('**', ''));
  if (!lines.length) return '\n\nYour memory is empty: save lasting facts and the user\'s preferences with the memory tool.';
  return '\n\nYour memory (topics saved in earlier runs; read one with the memory tool before relying on it, ' +
    'and save new facts with it):\n' + lines.join('\n');
}

export function buildAgent(model: LanguageModel, tools: ToolSet, parallel = false, timeoutSeconds = 60) {
  return new ToolLoopAgent({
    model,
    timeout: { stepMs: timeoutSeconds * 1000 }, // one model call plus the tools it requested
    instructions: identityText(AGENT_SETTINGS.names?.vercel) + loadInstructions() + memoryText(MEMORY_INDEX),
    tools: parallel ? tools : oneAtATime(tools),
    stopWhen: MAX_STEPS ? isStepCount(MAX_STEPS) : () => false,
  });
}

type Agent = ReturnType<typeof buildAgent>;
type Write = (text: string) => void;

export type OutputSettings = { width?: number; show_time?: boolean; show_tokens?: boolean; links?: boolean };

// A Markdown link, [text](url), or a bare web address: shown as a clickable OSC 8 link
const LINK = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()[\]"'`]+)/g;
const OPEN_LINK = /\[[^\]\n]*$|\]\([^)\s]*$/; // A Markdown link that is not finished yet

/**
 * Split text into plain parts and links, the same way in all three agents.
 * @param text The text.
 * @returns [shown text, URL or undefined] pairs. A Markdown link shows only its text; a bare address
 *   shows itself, without trailing punctuation.
 */
export function linkSegments(text: string): [string, string | undefined][] {
  const segments: [string, string | undefined][] = [];
  let position = 0;
  for (const match of text.matchAll(LINK)) {
    let shown = match[1] ?? match[3];
    let url = match[2] ?? match[3];
    let trailing = '';
    if (!match[1]) {
      const stripped = url.replace(/[.,;:!?]+$/, '');
      trailing = url.slice(stripped.length);
      url = shown = stripped;
    }
    segments.push([text.slice(position, match.index), undefined], [shown, url], [trailing, undefined]);
    position = match.index + match[0].length;
  }
  segments.push([text.slice(position), undefined]);
  return segments.filter(([part]) => part);
}

/** Read the shared terminal layout settings: "width" (wrap column) and "show_time". */
export function loadOutputSettings(file = OUTPUT_FILE): OutputSettings {
  return JSON.parse(readFileSync(file, 'utf8')) as OutputSettings;
}

/**
 * Word-wrap text to a width, the same way in all three agents. Each line wraps on its own;
 * continuation lines start with `indent`. A word longer than the width (e.g. a URL) is kept whole.
 * @param text The text; may contain newlines.
 * @param width The column to wrap at.
 * @param indent Prefix for continuation lines.
 * @returns The wrapped lines.
 */
export function wrap(text: string, width: number, indent = '  '): string[] {
  const lines: string[] = [];
  for (const raw of text.split('\n')) {
    let line: string | undefined;
    for (const word of raw.split(' ')) {
      if (line === undefined) {
        line = word;
      } else if (line.trim() && line.length + 1 + word.length > width) {
        lines.push(line);
        line = indent + word;
      } else {
        line += ' ' + word;
      }
    }
    lines.push(line ?? '');
  }
  return lines;
}

/**
 * The terminal layout shared by the three agents (README.md, "Terminal output"):
 * - Everything except the model's answer (banner, hints, tool calls and results, timing) is a dark
 *   gray line.
 * - The model's streamed answer is word-wrapped as it arrives, with exactly one blank line before
 *   and after it.
 * - All output fits in the configured width (context/output.json), or the terminal's if narrower.
 * - Each response ends with how long it took.
 */
export class Output {
  inText = false; // A text block is open
  private pending = ''; // Trailing newlines held back until more text follows
  private column = 0; // Where the streamed answer's current line ends
  private word = ''; // The streamed word being collected
  private spaces = ''; // The spaces before it
  private started = performance.now();
  private readonly width: number;
  private readonly showTime: boolean;
  private readonly showTokens: boolean;
  private readonly links: boolean; // OSC 8 links, only when writing to a terminal
  private usage?: { input: number; output: number; requests: number }; // When the server reports it
  private readonly write: Write;
  private readonly debug: boolean; // Print the gray lines; without it, a spinner shows the activity instead
  private spinner?: Ora;
  private blankOwed = false; // The last text ended without its blank line after it

  /**
   * @param write Prints raw text (stdout by default).
   * @param settings The layout settings; undefined reads context/output.json.
   * @param debug Print the gray lines (banner, hints, tool calls and results); otherwise a spinner
   *   runs while the model thinks or a tool runs, and only the answer and the timing line print.
   */
  constructor(write: Write, settings: OutputSettings = loadOutputSettings(), debug = true) {
    this.write = write;
    this.debug = debug;
    this.width = Math.min(settings.width ?? 120, process.stdout.isTTY ? process.stdout.columns : Infinity);
    this.showTime = settings.show_time ?? true;
    this.showTokens = settings.show_tokens ?? false;
    this.links = (settings.links ?? false) && Boolean(process.stdout.isTTY) && !process.env.NO_COLOR;
  }

  /** Start timing a response, and the spinner when not in debug mode (on a terminal only). */
  start(): void {
    this.started = performance.now();
    this.usage = undefined;
    this.spin('Thinking…');
  }

  /** Show a label on the spinner, starting it if needed; only when not in debug mode, on a terminal. */
  spin(label: string): void {
    if (this.debug || !process.stdout.isTTY || !(process.stdout.columns > 0)) return; // ora needs the terminal's width
    if (this.spinner) this.spinner.text = gray(label);
    // discardStdin off: the chat's readline owns the input
    else this.spinner = ora({ text: gray(label), color: 'gray', stream: process.stdout, discardStdin: false }).start();
  }

  /** Stop the spinner, if it runs; it leaves nothing on the screen. */
  stopSpinner(): void {
    this.spinner?.stop();
    this.spinner = undefined;
  }

  /** Count the tokens of model calls made for this response (in: sent to the model, out: generated). */
  addUsage(inputTokens: number, outputTokens: number, requests = 1): void {
    this.usage ??= { input: 0, output: 0, requests: 0 };
    this.usage.input += inputTokens;
    this.usage.output += outputTokens;
    this.usage.requests += requests;
  }

  /** Print a chunk of streamed model text. */
  text(chunk: string): void {
    if (!this.inText) {
      chunk = chunk.replace(/^\n+/, '');
      if (!chunk) return;
      this.stopSpinner();
      this.write('\n'); // Blank line before the text
      this.blankOwed = false;
      this.inText = true;
    }
    const body = chunk.replace(/\n+$/, '');
    if (body) {
      this.write(this.render(this.wrapStream(this.pending + body)));
      this.pending = chunk.slice(body.length);
    } else {
      this.pending += chunk;
    }
  }

  /**
   * Print a whole dark gray line (a tool call or result, the banner) in debug mode; otherwise only
   * show the activity on the spinner: "Running <tool>…" for a call, "Thinking…" after it.
   */
  line(text: string): void {
    if (!this.debug) {
      if (text.startsWith('→ ')) {
        this.end(false); // A call after some answer text: end its line, and spin again
        this.spin(`Running ${text.slice(2).split('(')[0]}…`);
      } else if (this.spinner && /^[←✗] /.test(text)) {
        this.spin('Thinking…');
      }
      return;
    }
    this.note(text);
  }

  /** Print a whole dark gray line, also when not in debug mode (the timing line, replies to commands). */
  note(text: string): void {
    this.stopSpinner();
    this.end();
    if (this.blankOwed) this.write('\n'); // Text ended without its blank line (a tool call followed it)
    this.blankOwed = false;
    for (const line of wrap(text, this.width)) this.write(gray(this.render(line)) + '\n');
  }

  /**
   * Close the open text block, if any: end its line and add the blank line after it.
   * @param blank Add the blank line now; false leaves it to what follows (more text adds its own, and
   *   a gray line adds one first), so text around a hidden tool call has only one.
   */
  end(blank = true): void {
    if (this.inText) {
      this.write(this.render(this.takeWord()) + (blank ? '\n\n' : '\n'));
      this.blankOwed = !blank;
    }
    this.inText = false;
    this.pending = '';
    this.column = 0;
    this.word = this.spaces = '';
  }

  /** Close the response, and print how long it took since `start` and the tokens it used. */
  finish(): void {
    this.stopSpinner();
    this.end();
    const parts = this.showTime ? [`Response time: ${((performance.now() - this.started) / 1000).toFixed(1)}s`] : [];
    if (this.showTokens) {
      const usage = this.usage;
      parts.push(usage
        ? `tokens: ${usage.input.toLocaleString('en-US')} in, ${usage.output.toLocaleString('en-US')} out ` +
          `(${usage.requests} model call${usage.requests === 1 ? '' : 's'})`
        : 'tokens: not reported');
    }
    if (parts.length) this.note(parts.join(' · '));
  }

  /** Show the text's Markdown links and web addresses as clickable OSC 8 links, when links are on. */
  private render(text: string): string {
    if (!this.links) return text;
    return linkSegments(text).map(([part, url]) => url ? `\x1b]8;;${url}\x1b\\${part}\x1b]8;;\x1b\\` : part).join('');
  }

  /** Word-wrap streamed text: words are held until they end, so they can move to the next line. */
  private wrapStream(text: string): string {
    let printed = '';
    for (const char of text) {
      if (char === '\n') {
        printed += this.takeWord() + '\n';
        this.column = 0;
        this.spaces = '';
      } else if (char === ' ' && this.links && OPEN_LINK.test(this.word) && this.word.length < 300) {
        this.word += char; // Inside [text](url): keep the link together
      } else if (char === ' ') {
        printed += this.takeWord();
        this.spaces += ' ';
      } else {
        this.word += char;
      }
    }
    return printed;
  }

  /** Place the collected word on the current line, or on the next one if it does not fit. */
  private takeWord(): string {
    if (!this.word) return '';
    let placed: string;
    const length = this.links ? linkSegments(this.word).reduce((sum, [part]) => sum + part.length, 0) : this.word.length;
    if (this.column && this.column + this.spaces.length + length > this.width) {
      placed = '\n' + this.word;
      this.column = length;
    } else {
      placed = this.spaces + this.word;
      this.column += this.spaces.length + length;
    }
    this.word = this.spaces = '';
    return placed;
  }
}

/**
 * Run one user turn, printing streamed text and tool activity.
 * @param agent The agent to run.
 * @param prompt The user's message.
 * @param history The messages of earlier turns.
 * @param options trace: debug mode, print tool calls and results (otherwise a spinner shows them);
 *   write: where to print (stdout by default).
 * @returns The updated message history, including this turn.
 */
export async function ask(agent: Agent, prompt: string, history: ModelMessage[],
                          { trace = true, write = (text: string) => void process.stdout.write(text) }:
                            { trace?: boolean; write?: Write } = {}): Promise<ModelMessage[]> {
  const messages: ModelMessage[] = [...history, { role: 'user', content: prompt }];
  const output = new Output(write, undefined, trace);
  output.start();
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
          pendingCalls.set(part.toolCallId, `→ ${part.toolName}(${JSON.stringify(part.input)})`);
          if (!trace) output.line(pendingCalls.get(part.toolCallId)!); // Name the tool on the spinner now, while it runs
          break;
        case 'tool-result':
          showCall(part.toolCallId);
          output.line(`← ${part.toolName}: ${readable(part.output)}`);
          break;
        case 'tool-error':
          showCall(part.toolCallId);
          output.line(`✗ ${part.toolName}: ${readable(errorMessage(part.error))}`);
          break;
        case 'error':
          throw part.error;
      }
    }
    const usage = await result.totalUsage;
    if (usage.inputTokens || usage.outputTokens) {
      output.addUsage(usage.inputTokens ?? 0, usage.outputTokens ?? 0, (await result.steps).length);
    }
    // Every step's messages, the tool calls and results too: result.response holds only the last step's
    return [...messages, ...(await result.steps).flatMap((step) => step.response.messages)];
  } finally {
    for (const line of pendingCalls.values()) output.line(line); // Calls that never got a result
    output.finish();
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
  if (trace) console.log(gray('Ask me to use a tool. /history shows messages, /reset clears them, exit quits.'));
  const rl = createInterface({ input: process.stdin, output: process.stdout, prompt: gray('You > ') });
  const onExit = loadInstructions(INSTRUCTIONS_FILE, 'on_exit');
  try {
    rl.prompt();
    // Iterating queues lines that arrive while the agent is busy (e.g. piped input).
    for await (const raw of rl) {
      const line = raw.trim();
      if (['exit', 'quit', 'q'].includes(line.toLowerCase())) {
        // One last turn to save what is worth remembering (save_on_exit); Ctrl+C skips it
        if (AGENT_SETTINGS.save_on_exit && onExit && worthSaving(history)) {
          if (trace) console.log(gray('Before exiting: saving anything worth remembering (Ctrl+C skips).'));
          try {
            await ask(agent, onExit, history, { trace });
          } catch (error) {
            console.error(styleText('red', `\nError: ${errorMessage(error)}`));
          }
        }
        return;
      }
      if (line === '/reset') {
        history = [];
        console.log(gray('History cleared.'));
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
      debug: { type: 'boolean', short: 'd', default: false },
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
  -d, --debug        Print the banner, tool calls and results as gray lines, instead of a spinner
  --parallel         Run the tool calls from one model response concurrently`);
    return 0;
  }

  if ([values.profile, values.local, values.openai].filter(Boolean).length > 1) {
    console.error('Use only one of --profile, --local and --openai.');
    return 2;
  }

  if (values.debug) console.log(); // Blank line before the banner (hidden without debug, so the answer's own blank line is enough)
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
    if (settings.auto) settings.model = (await loadedModel(settings.baseURL, settings.apiKey)) ?? settings.model;
    const tools = mcpClient ? await mcpClient.tools() : localTools;
    const agent = buildAgent(buildModel(settings), tools, values.parallel, settings.timeout);
    const toolCount = mcpUrl ? `${Object.keys(tools).length} tools from MCP server ${mcpUrl}` : `${Object.keys(tools).length} tools`;
    new Output((text) => void process.stdout.write(text), undefined, values.debug).line(
      `${settings.name} model: ${settings.model} @ ${settings.baseURL}, ${toolCount} (${values.parallel ? 'parallel' : 'sequential'})`);
    await chat(agent, values.prompt, values.debug, values.history);
    return 0;
  } catch (error) {
    console.error(styleText('red', `Error: ${errorMessage(error)}`));
    return 1;
  } finally {
    await mcpClient?.close();
  }
}

if (import.meta.main) process.exitCode = await main();
