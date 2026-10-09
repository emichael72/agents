// Offline tests: a scripted mock model stands in for LM Studio; the local tools run for real.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tool, type ModelMessage, type ToolSet } from 'ai';
import { convertArrayToReadableStream, MockLanguageModelV4 } from 'ai/test';
import { z } from 'zod';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { stripVTControlCharacters } from 'node:util';
import {
  ask, buildAgent, identityText, outOfTokens, linkSegments, loadInstructions, loadModels, loadOutputSettings, memoryText, Output, resolveModel, skillsText, worthSaving, wrap,
  loadTools, localTools, repeatGuard, REPO_ROOT, runScript, TOOLS_DIR,
} from '../vercelagent/index.ts';

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: undefined, cacheWrite: undefined },
  outputTokens: { total: 1, text: 1, reasoning: undefined },
};
const finish = (reason: 'stop' | 'tool-calls' | 'length') => ({ type: 'finish' as const, finishReason: { unified: reason, raw: reason }, usage });

/** First call: request every tool in one response. Later calls: answer with the tool outputs it was sent. */
function scriptedModel(calls: [string, object][]) {
  return new MockLanguageModelV4({
    doStream: async ({ prompt }) => {
      const toolMessage = prompt.findLast((message) => message.role === 'tool');
      if (!toolMessage) {
        return {
          stream: convertArrayToReadableStream([
            ...calls.map(([toolName, input], i) => ({
              type: 'tool-call' as const, toolCallId: `call-${i}`, toolName, input: JSON.stringify(input),
            })),
            finish('tool-calls'),
          ]),
        };
      }
      const outputs = toolMessage.content.map((part) => JSON.stringify((part as { output: unknown }).output));
      return {
        stream: convertArrayToReadableStream([
          { type: 'text-start', id: 't' },
          { type: 'text-delta', id: 't', delta: outputs.join(' | ') },
          { type: 'text-end', id: 't' },
          finish('stop'),
        ]),
      };
    },
  });
}

const quiet = { trace: false, write: () => {} };

/** The scripted turn every agent's tests replay (tests/scenario.json). */
type Step = { tool: string; arguments: object; outcome: 'ok' | 'error'; output: string };
const SCENARIO = JSON.parse(readFileSync(path.join(REPO_ROOT, 'tests', 'scenario.json'), 'utf8')) as
  { prompt: string; calls: Step[] };
const CALLS: [string, object][] = SCENARIO.calls.map((step) => [step.tool, step.arguments]);

test('the package finds the shared folders from the repository root', () => {
  assert.equal(REPO_ROOT, path.resolve(import.meta.dirname, '..', '..')); // tests/ -> vercel/ -> the root
  assert.equal(TOOLS_DIR, path.join(REPO_ROOT, 'tools'));
  assert.ok(readFileSync(path.join(REPO_ROOT, 'pyproject.toml'), 'utf8').includes('name = "agents"'));
});

test('local tools run for real and failures reach the model', async () => {
  const agent = buildAgent(scriptedModel(CALLS), localTools);
  const history = await ask(agent, SCENARIO.prompt, [], quiet);
  const answer = JSON.stringify(history.at(-1));
  for (const step of SCENARIO.calls) assert.ok(answer.includes(step.output), step.output);
  // The history keeps every step, the tool calls and their results too, not only the final answer
  assert.deepEqual(history.map((message) => message.role), ['user', 'assistant', 'tool', 'assistant']);
});

test('each tool call prints next to its result', async () => {
  let printed = '';
  const agent = buildAgent(scriptedModel(CALLS), localTools);
  await ask(agent, SCENARIO.prompt, [], { trace: true, write: (text) => { printed += text; } });
  const lines = stripVTControlCharacters(printed).split('\n')
    .filter((line) => /^[→←✗] /.test(line))
    .map((line) => line.slice(0, 2) + line.slice(2).split(/[(:]/)[0]);
  // The calls run at the same time, so they print in the order they finish: compare the pairs
  const pairs = [];
  for (let i = 0; i < lines.length; i += 2) pairs.push(`${lines[i]} ${lines[i + 1]}`);
  assert.deepEqual(pairs.sort(), SCENARIO.calls.map((step) =>
    `→ ${step.tool} ${step.outcome === 'ok' ? '←' : '✗'} ${step.tool}`).sort());
});

test('a call repeated too often is not run', async () => {
  const saved = repeatGuard.limit;
  repeatGuard.limit = 2;
  try {
    const read: [string, object] = ['skill', { name: 'pull-request' }];
    const same: [string, object][] = [read, read, read];
    const messages = await ask(buildAgent(scriptedModel(same), localTools, false), 'Keep reading the skill', [], quiet);
    const answer = JSON.stringify(messages.at(-1));
    assert.equal(answer.match(/# Change code and open a pull request/g)?.length, 2); // Two calls ran
    assert.match(answer, /Not run: you made this same skill call, with the same arguments, 2 times in a row/);
    const again = await ask(buildAgent(scriptedModel(same.slice(0, 1)), localTools, false), 'And now?', [], quiet);
    assert.doesNotMatch(JSON.stringify(again.at(-1)), /Not run/); // A new turn: the count starts again
  } finally {
    repeatGuard.limit = saved;
  }
});

test('a reply cut off at max_tokens says so, and the limit reaches the model', async () => {
  let maxOutputTokens: number | undefined;
  const thinker = new MockLanguageModelV4({
    doStream: async (options) => {
      maxOutputTokens = options.maxOutputTokens;
      return {
        stream: convertArrayToReadableStream([
          { type: 'reasoning-start' as const, id: 'r' },
          { type: 'reasoning-delta' as const, id: 'r', delta: 'hmm' },
          { type: 'reasoning-end' as const, id: 'r' },
          finish('length'),
        ]),
      };
    },
  });
  const errors: string[] = [];
  const saved = console.error;
  console.error = (text: string) => void errors.push(stripVTControlCharacters(text));
  try {
    await ask(buildAgent(thinker, {}, true, 60, 150), 'Think hard', [], { ...quiet, limit: { profile: 'local', maxTokens: 150 } });
  } finally {
    console.error = saved;
  }
  assert.equal(maxOutputTokens, 150);
  assert.deepEqual(errors, [outOfTokens({ profile: 'local', maxTokens: 150 })]);
  assert.equal(outOfTokens({ profile: 'local', maxTokens: 16000 }),
    "The model ran out of tokens: it may use 16,000 tokens per reply, thinking included " +
    "(max_tokens in the 'local' profile, context/models.json). Raise it, or ask for a smaller step.");
});

test('tools are told which agent runs them', async () => {
  assert.equal(await runScript(['printenv', 'AGENT_NAME']), 'Vercel Agent');
});

test('an omitted optional argument runs the tool without it', async () => {
  const agent = buildAgent(scriptedModel([['skill', {}]]), localTools);
  const answer = JSON.stringify((await ask(agent, 'Which skills are there?', [], quiet)).at(-1));
  assert.match(answer, /- pull-request: /);
});

test('a tool added to the tools folder is discovered and validated', async () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'tools-'));
  mkdirSync(path.join(dir, 'hello'));
  writeFileSync(path.join(dir, 'hello', 'tool.json'), JSON.stringify({
    description: 'Say hello', command: 'echo', args: ['hello'],
    params: [{ name: 'who', type: 'string', style: 'positional' }],
  }));
  const tools = loadTools(dir);
  assert.deepEqual(Object.keys(tools), ['hello']);
  const options = { toolCallId: 'call-0', messages: [] };
  const execute = tools.hello.execute as unknown as (input: unknown, opts: typeof options) => Promise<string>;
  assert.equal(await execute({ who: 'world' }, options), 'hello -- world'); // Positionals follow "--"
  const schema = tools.hello.inputSchema as z.ZodType;
  assert.equal(schema.safeParse({ who: 1 }).success, false);
  assert.equal(schema.safeParse({}).success, false);
});

/** Tools that record how many of them were running as each one started. */
function trackedTools() {
  const overlaps: number[] = [];
  let running = 0;
  const slow = tool({
    inputSchema: z.object({}),
    execute: async () => {
      overlaps.push(++running);
      await new Promise((resolve) => setTimeout(resolve, 50));
      running--;
      return 'ok';
    },
  });
  return { overlaps, tools: { a: slow, b: slow, c: slow } satisfies ToolSet };
}

test('tools run concurrently by default', async () => {
  const own = JSON.parse(readFileSync(path.join(import.meta.dirname, '..', 'instructions.json'), 'utf8'));
  assert.equal(own.parallel_tool_calls, true); // vercel/instructions.json
  assert.equal(own.name, 'vercel');
  const { overlaps, tools } = trackedTools();
  await ask(buildAgent(scriptedModel([['a', {}], ['b', {}], ['c', {}]]), tools), 'go', [], quiet);
  assert.ok(Math.max(...overlaps) > 1, `expected overlap, got ${overlaps}`);
});

test('tools run one at a time when parallel is off', async () => {
  const { overlaps, tools } = trackedTools();
  await ask(buildAgent(scriptedModel([['a', {}], ['b', {}], ['c', {}]]), tools, false), 'go', [], quiet);
  assert.deepEqual(overlaps, [1, 1, 1]);
});

test('model profiles come from the shared models file', () => {
  const saved = { ...process.env };
  try {
    process.env.OPENAI_API_KEY = 'test-key-not-real';
    for (const name of ['LOCAL_LLM_BASE_URL', 'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY']) delete process.env[name];
    const models = loadModels();
    const local = resolveModel(models); // "default": "local"
    // No model named: main() uses the loaded one, and stops if none is loaded rather than load one
    assert.deepEqual([local.baseURL, local.model], ['http://boba:1234/v1', '']);
    assert.equal(local.auto, true); // model_auto: main() asks the server for its loaded model
    assert.equal(local.apiKey, 'lm-studio'); // the OpenAI key is never used for another server
    process.env.LOCAL_LLM_MODEL = 'from-env';
    assert.equal(resolveModel(models, 'local').model, 'from-env');
    assert.equal(resolveModel(models, 'local', { model: 'from-cli' }).model, 'from-cli');
    assert.equal(resolveModel(models, 'local', { model: 'from-cli' }).auto, false); // An explicit model wins
    assert.equal(resolveModel(models, 'openai').apiKey, 'test-key-not-real');
    process.env.OPENAI_API_KEY = '';
    assert.throws(() => resolveModel(models, 'openai'), /OPENAI_API_KEY/);
    assert.throws(() => resolveModel(models, 'nope'), /Unknown model profile 'nope'/);
  } finally {
    process.env = saved;
  }
});

test('by default only the answer and the timing line print', () => {
  let printed = '';
  const output = new Output((text) => { printed += text; }, { width: 120, show_time: true }, false);
  output.start(); // Not a terminal: no spinner
  output.line('→ shell({"command":"ls"})');
  output.line('← shell: a.c');
  output.text('Two files.');
  output.line('banner or hint');
  output.finish();
  output.note('History cleared.');
  assert.deepEqual(stripVTControlCharacters(printed).split('\n'),
                   ['', 'Two files.', 'Response time: 0.0s', '', 'History cleared.', '']);
});

test('text around hidden tool calls has one blank line between', () => {
  for (const moreText of [true, false]) {
    let printed = '';
    const output = new Output((text) => { printed += text; }, { width: 120, show_time: true }, false);
    output.start();
    output.text('Let me check the build.');
    output.line('→ shell({"command":"make"})');
    output.line('← shell: ok');
    if (moreText) output.text('It builds cleanly.');
    output.finish();
    const expected = ['', 'Let me check the build.', ...(moreText ? ['', 'It builds cleanly.'] : [])];
    assert.deepEqual(stripVTControlCharacters(printed).split('\n'), [...expected, 'Response time: 0.0s', '', '']);
  }
});

test('instructions come from the shared context file', () => {
  const instructions = loadInstructions();
  assert.match(instructions, /^You are an agent/);
  assert.match(instructions, /allowed folder/);
  assert.match(loadInstructions(undefined, 'on_exit'), /Nothing to save/);
  assert.match(identityText('vercel'), /^Your name is vercel\./);
  assert.equal(identityText(undefined), '');
});

test('exit saves only after a tool call or several exchanges', () => {
  const user: ModelMessage = { role: 'user', content: 'hi' };
  const reply: ModelMessage = { role: 'assistant', content: 'hello' };
  const result: ModelMessage = { role: 'tool', content: [] };
  assert.equal(worthSaving([]), false);
  assert.equal(worthSaving([user, reply]), false);
  assert.equal(worthSaving([user, reply, user, reply]), true);
  assert.equal(worthSaving([user, reply, result, reply]), true);
});

test('history carries across turns', async () => {
  const model = new MockLanguageModelV4({
    doStream: async ({ prompt }) => ({
      stream: convertArrayToReadableStream([
        { type: 'text-start', id: 't' },
        { type: 'text-delta', id: 't', delta: `${prompt.filter((m) => m.role !== 'system').length} messages so far` },
        { type: 'text-end', id: 't' },
        finish('stop'),
      ]),
    }),
  });
  const agent = buildAgent(model, {});
  let history = await ask(agent, 'one', [], quiet);
  history = await ask(agent, 'two', history, quiet);
  assert.equal(history.length, 4);
  assert.match(JSON.stringify(history.at(-1)), /3 messages so far/);
});

test('lines and the streamed answer wrap to the width, and the response is timed', () => {
  assert.deepEqual(wrap('one two three four five six seven', 15), ['one two three', '  four five six', '  seven']);
  let printed = '';
  const output = new Output((text) => { printed += text; }, { width: 30, show_time: true });
  const answer = 'The quiz service is running and pull request number one is still waiting for its quiz.';
  for (let i = 0; i < answer.length; i += 7) output.text(answer.slice(i, i + 7));
  output.finish();
  const plain = stripVTControlCharacters(printed);
  const body = plain.split('\n').filter((line) => line && !line.startsWith('Response time'));
  assert.ok(body.every((line) => line.length <= 30));
  assert.equal(body.join(' '), answer);
  assert.match(plain, /[^\n]\nResponse time: \d+\.\ds\n\n$/);
});

test('layout settings come from the shared context file', () => {
  assert.deepEqual(loadOutputSettings(), { ...loadOutputSettings(), width: 120, show_time: true });
});

test('token counts follow the response time', () => {
  let printed = '';
  const output = new Output((text) => { printed += text; }, { width: 120, show_time: true, show_tokens: true });
  output.addUsage(1200, 34);
  output.addUsage(1300, 56, 2);
  output.finish();
  assert.match(stripVTControlCharacters(printed), /Response time: \d+\.\ds · tokens: 2,500 in, 90 out · 3 model calls\n\n$/);
});

test('links split into shown text and address', () => {
  assert.deepEqual(linkSegments('see [PR #5](https://x/y) and http://a.b/c.'),
                   [['see ', undefined], ['PR #5', 'https://x/y'], [' and ', undefined], ['http://a.b/c', 'http://a.b/c'], ['.', undefined]]);
});


test('the memory index joins the instructions', () => {
  const folder = mkdtempSync(path.join(os.tmpdir(), 'memory-'));
  const index = path.join(folder, 'index.md');
  assert.match(memoryText(index), /memory is empty/);
  writeFileSync(index, '# Memory index\n\n- **preferences**: Prefers short answers (updated 2026-10-07)\n');
  assert.match(memoryText(index), /- preferences: Prefers short answers/);
  assert.equal(memoryText(undefined), '');
});

test('the skills join the instructions', () => {
  const folder = mkdtempSync(path.join(os.tmpdir(), 'skills-'));
  assert.equal(skillsText(folder), ''); // No skills yet
  mkdirSync(path.join(folder, 'build'));
  writeFileSync(path.join(folder, 'build', 'SKILL.md'), '---\nname: build\ndescription: Build and test a project.\n---\n\n# Build\n\n---\n\ndescription: not a header line\n');
  mkdirSync(path.join(folder, 'notes')); // A folder without SKILL.md is not a skill
  assert.ok(skillsText(folder).endsWith('read it with the skill tool and follow it):\n- build: Build and test a project.'));
  assert.equal(skillsText(undefined), '');
});
