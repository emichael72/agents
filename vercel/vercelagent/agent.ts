// An AI SDK (Vercel) terminal agent, the counterpart of the pydantic agent and MCPAgent.
//
// Same model profiles (agents/context/models.json), instructions (agents/context/instructions.json)
// and tools (agents/tools) as the other two agents. The agent loop
// (call the model, run requested tools, send results back, repeat until it answers) is done by
// the AI SDK's ToolLoopAgent; this file only builds the agent and renders its stream in the terminal.
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { parseArgs, styleText } from 'node:util';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { isStepCount, ToolLoopAgent, type LanguageModel, type ModelMessage, type ToolSet } from 'ai';
import { highlight } from 'cli-highlight';
import { Marked } from 'marked';
import { markedTerminal } from 'marked-terminal';
import ora, { type Ora } from 'ora';
import { localTools, oneAtATime, REPO_ROOT, repeatGuard } from './tools.ts';


// Instructions (system prompt) and model profiles, shared by all three agents
const CONTEXT_DIR = path.join(REPO_ROOT, 'context');
const INSTRUCTIONS_FILE = path.join(CONTEXT_DIR, 'instructions.json');
const MODELS_FILE = path.join(CONTEXT_DIR, 'models.json');
const OUTPUT_FILE = path.join(CONTEXT_DIR, 'output.json'); // Terminal layout
const AGENT_FILE = path.join(CONTEXT_DIR, 'agent.json'); // Agent loop settings
// This agent's name, settings and own instructions, added to the shared ones
const OWN_FILE = path.join(REPO_ROOT, 'vercel', 'instructions.json');

// Tool calls per prompt, shared with the other agents (context/agent.json). Each step is one model
// call plus the tools it requested, so this allows at least max_tool_calls calls and a final answer.
// 0 means no limit: the loop then ends only when the model answers without calling a tool.
const AGENT_SETTINGS = JSON.parse(readFileSync(AGENT_FILE, 'utf8')) as
  { max_tool_calls?: number; max_repeated_calls?: number; memory_index?: string; save_on_exit?: boolean;
    skills_dir?: string; out_of_tokens_retries?: number; thinking_dir?: string };
const OWN = JSON.parse(readFileSync(OWN_FILE, 'utf8')) as
  { name?: string; parallel_tool_calls?: boolean; instructions?: string[] };
const MAX_TOOL_CALLS = AGENT_SETTINGS.max_tool_calls ?? 8;
repeatGuard.limit = AGENT_SETTINGS.max_repeated_calls ?? 0; // The same call at most this many times in a row
// The memory index (relative to the repository), loaded into the instructions
const MEMORY_INDEX = AGENT_SETTINGS.memory_index && path.join(REPO_ROOT, AGENT_SETTINGS.memory_index);
// The skills folder (relative to the repository), whose skills are listed in the instructions
const SKILLS_DIR = AGENT_SETTINGS.skills_dir && path.join(REPO_ROOT, AGENT_SETTINGS.skills_dir);
// With -d, the thinking of a reply that ran out of tokens is saved here (relative to the repository)
const THINKING_DIR = AGENT_SETTINGS.thinking_dir && path.join(REPO_ROOT, AGENT_SETTINGS.thinking_dir);
const MAX_STEPS = MAX_TOOL_CALLS ? MAX_TOOL_CALLS + 1 : 0;

const gray = (text: string) => styleText('gray', text); // Everything except the model's answer

type Profile = {
  name?: string; base_url: string; base_url_env?: string; model?: string; model_env?: string; model_auto?: boolean;
  api_key_env?: string; api_key?: string; timeout?: number; max_tokens?: number; sampling?: Record<string, unknown>;
};
type Models = { default?: string; profiles?: Record<string, Profile> };
export type ModelSettings = {
  profile: string; name: string; baseURL: string; model: string; apiKey: string; timeout: number;
  auto: boolean; // Ask the server which model is loaded (see loadedModel)
  maxTokens?: number; // The most tokens one reply may use, thinking included; undefined leaves it to the server
  sampling?: Record<string, unknown>; // Request settings sent as they are with each model call (temperature, top_k, ...)
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
  // With model_auto the model may be left out: the one loaded on the server is used
  const missing = (settings.model_auto ? ['base_url'] as const : ['base_url', 'model'] as const).filter((key) => !settings[key]);
  if (missing.length) throw new Error(`Model profile '${name}' is missing ${missing.join(', ')}.`);
  const env = (variable?: string) => (variable ? process.env[variable] : undefined) || undefined;
  const apiKey = (env(settings.api_key_env) ?? settings.api_key ?? '').trim();
  if (!apiKey) throw new Error(`Set ${settings.api_key_env ?? 'an API key'} in the environment for the '${name}' profile.`);
  return {
    profile: name,
    name: settings.name ?? name,
    baseURL: overrides.baseURL || env(settings.base_url_env) || settings.base_url,
    model: overrides.model || env(settings.model_env) || settings.model || '',
    apiKey,
    timeout: settings.timeout ?? 60,
    maxTokens: settings.max_tokens || undefined,
    sampling: settings.sampling,
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

/**
 * Create a model on an OpenAI-compatible server (LM Studio, OpenAI, ...) from resolved settings, with
 * the profile's sampling settings added to each request as they are (top_k and min_p too, which the
 * AI SDK does not send).
 */
export function buildModel(settings: ModelSettings): LanguageModel {
  // includeUsage: streamed replies report their token counts (shown after each response)
  return createOpenAICompatible({ name: settings.profile, baseURL: settings.baseURL, apiKey: settings.apiKey,
                                  includeUsage: true,
                                  transformRequestBody: (body) => ({ ...body, ...settings.sampling }) })(settings.model);
}

/**
 * Read the shared instructions file.
 * @param file The JSON file (default: agents/context/instructions.json).
 * @param key Which lines to read: 'instructions', 'on_exit' (the prompt sent before exit), or
 *   'out_of_tokens' (sent to ask again after a reply ran out of tokens).
 * @returns Those lines, joined with newlines ('' if the file has none).
 */
export function loadInstructions(file = INSTRUCTIONS_FILE, key = 'instructions'): string {
  return ((JSON.parse(readFileSync(file, 'utf8')) as Record<string, string[] | undefined>)[key] ?? []).join('\n');
}

/**
 * The identity lines that open the instructions, naming the agent.
 * @param name The agent's name (its own file's name); undefined when not configured.
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

/**
 * The agents' skills, to append to their instructions: each skill's name and description, from the
 * header of its <name>/SKILL.md, so the model knows when to read one with the skill tool.
 * @param folder The skills folder (context/agent.json's skills_dir); undefined when not configured.
 * @returns A paragraph listing the skills; '' without a folder or skills.
 */
export function skillsText(folder?: string): string {
  if (!folder || !existsSync(folder)) return '';
  const lines = readdirSync(folder, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && existsSync(path.join(folder, entry.name, 'SKILL.md')))
    .map((entry) => entry.name)
    .sort()
    .map((name) => {
      const header = readFileSync(path.join(folder, name, 'SKILL.md'), 'utf8').split('\n---')[0]; // Ends at its second ---
      const line = header.split('\n').find((l) => l.startsWith('description:'));
      return `- ${name}: ${line ? line.slice(line.indexOf(':') + 1).trim() : ''}`;
    });
  if (!lines.length) return '';
  return '\n\nYour skills (before a task that matches one, read it with the skill tool and follow it):\n' +
    lines.join('\n');
}

/**
 * What to tell the user when a reply hit the profile's max_tokens (the same in all three agents).
 * @param settings The resolved profile (its name and maxTokens).
 * @returns The message.
 */
export function outOfTokens(settings: { profile?: string; maxTokens?: number } = {}): string {
  const limit = settings.maxTokens ? settings.maxTokens.toLocaleString('en-US') : "the server's limit of";
  return `The model ran out of tokens: it may use ${limit} tokens per reply, thinking included ` +
    `(max_tokens in the '${settings.profile}' profile, context/models.json). Raise it, or ask for a smaller step.`;
}

/**
 * Build the agent: the instructions (identity, shared, this agent's own, skills, memory) and the tools.
 * @param model The model to drive it.
 * @param tools The tools.
 * @param parallel Run the tool calls of one model response concurrently (default:
 *   parallel_tool_calls in vercel/instructions.json, true when unset), else one at a time.
 * @param timeoutSeconds The limit for one model call plus the tools it requested.
 * @param maxTokens The most tokens one reply may use, thinking included (the profile's max_tokens);
 *   undefined leaves it to the server.
 * @returns The agent.
 */
export function buildAgent(model: LanguageModel, tools: ToolSet, parallel = OWN.parallel_tool_calls ?? true,
                           timeoutSeconds = 60, maxTokens?: number) {
  const own = (OWN.instructions ?? []).join('\n');
  return new ToolLoopAgent({
    model,
    timeout: { stepMs: timeoutSeconds * 1000 }, // one model call plus the tools it requested
    maxOutputTokens: maxTokens,
    instructions: identityText(OWN.name) + loadInstructions() + (own ? '\n\n' + own : '') + skillsText(SKILLS_DIR) +
      memoryText(MEMORY_INDEX),
    tools: parallel ? tools : oneAtATime(tools),
    stopWhen: MAX_STEPS ? isStepCount(MAX_STEPS) : () => false,
  });
}

type Agent = ReturnType<typeof buildAgent>;
type Write = (text: string) => void;

export type OutputSettings = { width?: number; show_time?: boolean; show_tokens?: boolean; links?: boolean; render?: boolean };

// A file named in a tool call, and the language its contents are highlighted as
const FILE_LANGUAGES: Record<string, string> = {
  '.c': 'c', '.h': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.hpp': 'cpp', '.py': 'python', '.ts': 'typescript',
  '.js': 'javascript', '.json': 'json', '.md': 'markdown', '.sh': 'bash', '.mk': 'makefile', '.yaml': 'yaml',
  '.yml': 'yaml', '.toml': 'toml',
};
const NAMED_FILE = /[\w./-]*?(Makefile|\.(?:c|h|cc|cpp|hpp|py|ts|js|json|md|sh|mk|yaml|yml|toml))\b/;
const NUMBERED_LINE = /^(\s*\d+)(\t| {2})(.*)$/; // cat -n's lines, and ed's
// Dimmed colors for code a tool shows: each token keeps dim, since its own codes end with a reset
const dimmed = (...styles: Parameters<typeof styleText>[0][]) => (text: string) => styleText(['dim', ...styles.flat()] as never, text);
const DIM_THEME = {
  keyword: dimmed('blue'), built_in: dimmed('cyan'), type: dimmed('cyan'), literal: dimmed('yellow'),
  number: dimmed('yellow'), string: dimmed('green'), comment: dimmed('gray'), meta: dimmed('magenta'),
  title: dimmed('cyan'), attr: dimmed('yellow'), symbol: dimmed('magenta'), section: dimmed('bold'),
  default: dimmed([]),
};

/**
 * Find where the first complete Markdown block ends: at a blank line outside a code block, or after
 * the line that closes a code block.
 * @param text The streamed text of the current block, and what follows it.
 * @returns The offset just past the block; undefined while it is still open.
 */
export function blockEnd(text: string): number | undefined {
  let fenced = false;
  let offset = 0;
  for (const line of text.split(/(?<=\n)/)) {
    if (!line.endsWith('\n')) return undefined; // The line is not complete yet
    offset += line.length;
    const stripped = line.trim();
    if (stripped.startsWith('```') || stripped.startsWith('~~~')) {
      if (fenced) return offset;
      fenced = true;
    } else if (!fenced && !stripped && offset > line.length) {
      return offset;
    }
  }
  return undefined;
}

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
  private thinkingSince?: number; // When the current stretch of thinking began
  private thinkingShown = -1; // The seconds the spinner shows for it
  private thought = ''; // The thinking since the spinner last changed: the current model call's
  private readonly renderMarkdown: boolean; // Render the answer's Markdown and highlight code, on a terminal
  private readonly markdown: Marked;
  private block = ''; // The answer's Markdown block being streamed
  private blockLines = 0; // The lines it takes on screen, to redraw it in place
  private blocks = 0; // Blocks of this answer already shown
  private lastCall = ''; // The latest "→ tool(...)" line, to tell the language of what the tool shows

  /**
   * @param write Prints raw text (stdout by default).
   * @param settings The layout settings; undefined reads context/output.json.
   * @param debug Print the gray lines (banner, hints, tool calls and results); otherwise a spinner
   *   runs while the model thinks or a tool runs, and only the answer and the timing line print.
   * @param terminal Writing to a terminal, where the answer's Markdown is rendered ("render", on by
   *   default; --plain turns it off): block by block, each redrawn in place as it streams, with
   *   highlighted code; with debug, code a tool shows from a named file is highlighted too, dimmed.
   */
  constructor(write: Write, settings: OutputSettings = loadOutputSettings(), debug = true,
              terminal = Boolean(process.stdout.isTTY)) {
    this.write = write;
    this.debug = debug;
    this.width = Math.min(settings.width ?? 120, process.stdout.isTTY ? process.stdout.columns : Infinity);
    this.showTime = settings.show_time ?? true;
    this.showTokens = settings.show_tokens ?? false;
    this.links = (settings.links ?? false) && Boolean(process.stdout.isTTY) && !process.env.NO_COLOR;
    this.renderMarkdown = (settings.render ?? true) && terminal && !process.env.NO_COLOR;
    this.markdown = new Marked();
    this.markdown.use(markedTerminal({ width: this.width, reflowText: true, tab: 2, showSectionPrefix: false }) as never);
  }

  /** Start timing a response, and the spinner (on a terminal only). */
  start(): void {
    this.started = performance.now();
    this.usage = undefined;
    this.block = '';
    this.blockLines = this.blocks = 0;
    this.spin('Thinking…');
  }

  /**
   * Show a label on the spinner, starting it if needed; on a terminal only (piped output has none).
   * A new label also ends a stretch of thinking (see thinking).
   */
  spin(label: string): void {
    this.thinkingSince = undefined;
    this.thinkingShown = -1;
    this.thought = '';
    this.label(label);
  }

  /**
   * A chunk of the model's thinking arrived: show on the spinner how long it has been thinking, so a
   * reasoning model's long silence is visibly work, not a hang. Ignored once the answer streams.
   * The text is kept, for outOfTokens to save.
   * @param text The chunk.
   */
  thinking(text = ''): void {
    this.thought += text;
    if (this.inText) return;
    const now = performance.now();
    this.thinkingSince ??= now;
    const seconds = Math.floor((now - this.thinkingSince) / 1000);
    if (seconds !== this.thinkingShown) {
      this.thinkingShown = seconds;
      this.label(seconds ? `Thinking… ${seconds}s` : 'Thinking…');
    }
  }

  /**
   * A reply ran out of tokens (max_tokens): with debug, say so in a gray line and save its thinking in
   * the folder (thinking_dir), to see whether the model went in circles; then show the retry, if one
   * follows, on the spinner.
   * @param folder Where the thinking is saved, as thinking-<date>-<time>.log; undefined saves nothing.
   * @param retry The model is asked again.
   */
  outOfTokens(folder: string | undefined, retry: boolean): void {
    if (this.debug) {
      let saved = '';
      if (folder && this.thought) {
        mkdirSync(folder, { recursive: true });
        const now = new Date();
        const two = (n: number) => String(n).padStart(2, '0'); // Local time, as the other agents name the file
        const stamp = `${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}-` +
          `${two(now.getHours())}${two(now.getMinutes())}${two(now.getSeconds())}`;
        const file = path.join(folder, `thinking-${stamp}.log`);
        writeFileSync(file, this.thought);
        saved = `; its thinking is in ${file}`;
      }
      this.note(`✗ out of tokens${saved}` + (retry ? '; asking again for a direct answer' : ''));
    }
    if (retry) this.spin('Asking again…');
  }

  /** Show a label on the spinner, starting it if needed. */
  private label(label: string): void {
    if (!process.stdout.isTTY || !(process.stdout.columns > 0)) return; // ora needs the terminal's width
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
    if (this.renderMarkdown) {
      this.streamMarkdown(chunk);
      return;
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
   * Stream rendered Markdown: add the chunk to the current block and redraw it; each block that is
   * complete stays on screen, and the next starts below it.
   */
  private streamMarkdown(chunk: string): void {
    this.block += chunk;
    for (let end = blockEnd(this.block); end !== undefined; end = blockEnd(this.block)) {
      const done = this.block.slice(0, end);
      this.block = this.block.slice(end).replace(/^\n+/, '');
      this.showBlock(done, true);
    }
    if (this.block.trim()) this.showBlock(this.block, false);
  }

  /**
   * Draw a block rendered: in place while it streams (the cursor goes back over its lines and they are
   * written again), and for good once it is complete. A block taller than the terminal is not redrawn
   * until it is complete, since the lines above the screen cannot be reached.
   */
  private showBlock(text: string, final: boolean): void {
    const rendered = (this.markdown.parse(text.replace(/^\n+|\n+$/g, '')) as string).replace(/^\n+|\n+$/g, '');
    const lines = rendered.split('\n').length;
    const rows = process.stdout.rows || 24;
    if (!final && lines > rows - 2) return;
    if (this.blockLines) this.write(`\x1b[${this.blockLines}F\x1b[J`); // Back to the block's first line, clear below
    else if (this.blocks) this.write('\n'); // One blank line between blocks
    this.write(rendered + '\n');
    this.blockLines = final ? 0 : lines;
    if (final) this.blocks += 1;
  }

  /**
   * With rendering on, show a tool's result highlighted and dimmed when it is code from a file its call
   * names (cat -n src/main.c, ed on a Makefile): the gray first line, then each line of code, with its
   * number kept gray.
   * @returns True if it was shown; false to show it as a plain gray line.
   */
  private toolCode(text: string): boolean {
    const named = this.renderMarkdown ? NAMED_FILE.exec(this.lastCall) : null;
    const at = text.indexOf(': ');
    const body = at >= 0 ? text.slice(at + 2) : '';
    if (!named || !body.includes('\n')) return false;
    const language = named[1] === 'Makefile' ? 'makefile' : FILE_LANGUAGES[named[1]];
    const [first, ...rest] = body.split('\n');
    this.note(text.slice(0, at + 2) + first);
    for (const line of rest) {
      const numbered = NUMBERED_LINE.exec(line);
      const [number, code] = numbered ? [numbered[1] + numbered[2].replace('\t', '  '), numbered[3]] : ['', line];
      let shown: string;
      try {
        shown = highlight(code.replaceAll('\t', '    '), { language, ignoreIllegals: true, theme: DIM_THEME as never });
      } catch {
        shown = styleText('dim', code);
      }
      this.write('  ' + gray(number) + shown + '\n');
    }
    return true;
  }

  /**
   * Print a whole dark gray line (a tool call or result, the banner) in debug mode, then spin on while
   * the tool or the model works; otherwise only show the activity on the spinner: "Running <tool>…"
   * for a call, "Thinking…" after it.
   */
  line(text: string): void {
    if (text.startsWith('→ ')) this.lastCall = text;
    if (!this.debug) {
      if (text.startsWith('→ ')) {
        this.end(false); // A call after some answer text: end its line, and spin again
        this.spin(`Running ${text.slice(2).split('(')[0]}…`);
      } else if (this.spinner && /^[←✗] /.test(text)) {
        this.spin('Thinking…');
      }
      return;
    }
    if (!(text.startsWith('← ') && this.toolCode(text))) this.note(text);
    if (/^[←✗] /.test(text)) this.spin('Thinking…'); // A result goes back to the model, which works on it
  }

  /** Print a whole dark gray line, also when not in debug mode (the timing line, replies to commands). */
  note(text: string): void {
    this.stopSpinner();
    this.end();
    if (this.blankOwed) this.write('\n'); // Text ended without its blank line (a tool call followed it)
    this.blankOwed = false;
    for (const line of wrap(text, this.width)) this.write(this.render(line, gray) + '\n');
  }

  /**
   * Close the open text block, if any: end its line and add the blank line after it.
   * @param blank Add the blank line now; false leaves it to what follows (more text adds its own, and
   *   a gray line adds one first), so text around a hidden tool call has only one.
   */
  end(blank = true): void {
    if (this.inText && this.renderMarkdown) { // The last block stays; the cursor is already on a new line
      if (this.block.trim()) this.showBlock(this.block, true);
      this.block = '';
      this.blockLines = 0;
      if (blank) this.write('\n');
      this.blankOwed = !blank;
    } else if (this.inText) {
      this.write(this.render(this.takeWord()) + (blank ? '\n\n' : '\n'));
      this.blankOwed = !blank;
    }
    this.inText = false;
    this.pending = '';
    this.column = 0;
    this.word = this.spaces = '';
  }

  /**
   * Close the response: the line with how long it took since `start` and the tokens it used goes
   * right under the answer, and a blank line after it sets the response off from the next prompt.
   */
  finish(): void {
    this.stopSpinner();
    this.end(false);
    this.blankOwed = false; // The timing line belongs to the answer: no blank line between them
    const parts = this.showTime ? [`Response time: ${((performance.now() - this.started) / 1000).toFixed(1)}s`] : [];
    if (this.showTokens) {
      const usage = this.usage;
      parts.push(usage
        ? `tokens: ${usage.input.toLocaleString('en-US')} in, ${usage.output.toLocaleString('en-US')} out · ` +
          `${usage.requests} model call${usage.requests === 1 ? '' : 's'}`
        : 'tokens: not reported');
    }
    if (parts.length) this.note(parts.join(' · '));
    this.write('\n');
  }

  /**
   * Show the text's Markdown links and web addresses as clickable OSC 8 links, when links are on,
   * in bright cyan: the one vivid color, in the gray lines too, so a link such as the quiz's stands out.
   * @param text The text.
   * @param style Styles the rest of the text (gray for the gray lines).
   */
  private render(text: string, style = (part: string) => part): string {
    if (!this.links) return style(text);
    return linkSegments(text).map(([part, url]) =>
      url ? styleText('cyanBright', `\x1b]8;;${url}\x1b\\${part}\x1b]8;;\x1b\\`) : style(part)).join('');
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
                          { trace = true, write = (text: string) => void process.stdout.write(text), limit = {}, plain = false }:
                            { trace?: boolean; write?: Write; limit?: { profile?: string; maxTokens?: number };
                              plain?: boolean } = {}):
  Promise<ModelMessage[]> {
  const messages: ModelMessage[] = [...history, { role: 'user', content: prompt }];
  const output = new Output(write, plain ? { ...loadOutputSettings(), render: false } : undefined, trace); // --plain
  output.start();
  repeatGuard.reset(); // Only this turn's calls count as repeats
  // The AI SDK reports every call of a model response before their results; hold each call line
  // until its result arrives, so the two print together (as in the other agents).
  const pendingCalls = new Map<string, string>();
  const showCall = (toolCallId: string) => {
    const line = pendingCalls.get(toolCallId);
    if (line) output.line(line);
    pendingCalls.delete(toolCallId);
  };
  let cutOff = false;
  let retries = AGENT_SETTINGS.out_of_tokens_retries ?? 0;
  const retryPrompt = loadInstructions(INSTRUCTIONS_FILE, 'out_of_tokens');
  let turn = messages;
  try {
    for (;;) {
      cutOff = false;
      const result = await agent.stream({ messages: turn });
      for await (const part of result.fullStream) {
        switch (part.type) {
          case 'text-delta':
            output.text(part.text);
            break;
          case 'reasoning-delta':
            output.thinking(part.text); // A reasoning model thinks: the spinner shows for how long
            break;
          case 'tool-call':
            pendingCalls.set(part.toolCallId, `→ ${part.toolName}(${JSON.stringify(part.input)})`);
            if (!trace) output.line(pendingCalls.get(part.toolCallId)!); // Name the tool on the spinner now, while it runs
            else output.spin(`Running ${part.toolName}…`); // The call prints with its result; meanwhile the spinner names it
            break;
          case 'finish-step':
            if (part.finishReason === 'length') cutOff = true; // The reply hit max_tokens
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
      const steps = await result.steps;
      if (usage.inputTokens || usage.outputTokens) {
        output.addUsage(usage.inputTokens ?? 0, usage.outputTokens ?? 0, steps.length);
      }
      // Out of tokens while still thinking, nothing of the answer shown: ask again (out_of_tokens_retries)
      const retry = cutOff && retries > 0 && Boolean(retryPrompt) && !output.inText;
      if (cutOff) output.outOfTokens(THINKING_DIR, retry);
      if (retry) {
        retries -= 1;
        cutOff = false;
        // The turn's tool calls and results are kept; the cut-off step's thinking is dropped, and the
        // model starts over, told to be brief
        turn = [...turn, ...steps.slice(0, -1).flatMap((step) => step.response.messages),
                { role: 'user', content: retryPrompt }];
        continue;
      }
      // Every step's messages, the tool calls and results too: result.response holds only the last step's
      return [...turn, ...steps.flatMap((step) => step.response.messages)];
    }
  } finally {
    for (const line of pendingCalls.values()) output.line(line); // Calls that never got a result
    output.finish();
    if (cutOff) console.error(styleText('red', outOfTokens(limit)));
  }
}

/**
 * Make a tool's output readable for the terminal.
 * @param output Plain text, or JSON text such as MCPAgent's {"status", "logs", "summary"} result or
 *   an {"error": ...} failure.
 * @returns The "logs" lines or the error message when the output is such JSON, else the text.
 */
export function readable(output: unknown): string {
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

async function chat(agent: Agent, prompt: string | undefined, trace: boolean, showHistory: boolean,
                    limit: { profile?: string; maxTokens?: number } = {}, plain = false) {
  let history: ModelMessage[] = [];
  if (prompt !== undefined) {
    history = await ask(agent, prompt, history, { trace, limit, plain });
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
            await ask(agent, onExit, history, { trace, limit, plain });
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
          history = await ask(agent, line, history, { trace, limit, plain });
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

// The command-line options (main)
const CLI_OPTIONS = {
  profile: { type: 'string' },
  local: { type: 'boolean', default: false },
  openai: { type: 'boolean', default: false },
  model: { type: 'string' },
  'base-url': { type: 'string' },
  prompt: { type: 'string' },
  history: { type: 'boolean', default: false },
  debug: { type: 'boolean', short: 'd', default: false },
  plain: { type: 'boolean', default: false },
  help: { type: 'boolean', short: 'h', default: false },
} as const;

/**
 * The command line: parse the options, build the model and the agent, then chat or run one prompt.
 * @returns The exit status: 0 on success, 1 on an error, 2 for a usage error.
 */
export async function main(): Promise<number> {
  let parsed;
  try {
    parsed = parseArgs({ options: CLI_OPTIONS });
  } catch (error) { // An unknown option or a missing value: a usage error, not a crash
    console.error(styleText('red', `Error: ${errorMessage(error)}`) + ' (see --help)');
    return 2;
  }
  const { values } = parsed;
  if (values.help) {
    console.log(`Usage: node vercel/agent.ts [options]
  --profile NAME     Model profile from context/models.json (default: its "default")
  --local, --openai  Shortcuts for --profile local and --profile openai
  --model ID         Override the profile's model for this run
  --base-url URL     Override the profile's OpenAI-compatible base URL for this run
  --prompt TEXT      Run one prompt and exit
  --history          With --prompt, print the message history
  --plain            Print the answer as plain text: no Markdown rendering or code highlighting
  -d, --debug        Print the banner, tool calls and results as gray lines, instead of a spinner`);
    return 0;
  }

  if ([values.profile, values.local, values.openai].filter(Boolean).length > 1) {
    console.error('Use only one of --profile, --local and --openai.');
    return 2;
  }

  if (values.debug) console.log(); // Blank line before the banner (hidden without debug, so the answer's own blank line is enough)
  try {
    const profile = values.profile ?? (values.local ? 'local' : values.openai ? 'openai' : undefined);
    const settings = resolveModel(loadModels(), profile, { model: values.model, baseURL: values['base-url'] });
    if (settings.auto) settings.model = (await loadedModel(settings.baseURL, settings.apiKey)) ?? settings.model;
    if (!settings.model) { // Never ask the server to load a model it has not loaded
      const variable = loadModels().profiles?.[settings.profile]?.model_env ?? 'model in the profile';
      throw new Error(`No model is loaded on ${settings.baseURL}, and the '${settings.profile}' profile names none ` +
        `(it uses the loaded model): load one in LM Studio, or name one with --model or ${variable}.`);
    }
    const parallel = OWN.parallel_tool_calls ?? true;
    const agent = buildAgent(buildModel(settings), localTools, parallel, settings.timeout, settings.maxTokens);
    const toolCount = `${Object.keys(localTools).length} tools`;
    new Output((text) => void process.stdout.write(text), undefined, values.debug).line(
      `${settings.name} model: ${settings.model} @ ${settings.baseURL}, ${toolCount} (${parallel ? 'parallel' : 'sequential'})`);
    await chat(agent, values.prompt, values.debug, values.history, settings, values.plain);
    return 0;
  } catch (error) {
    console.error(styleText('red', `Error: ${errorMessage(error)}`));
    return 1;
  }
}
