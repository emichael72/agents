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
  ask, buildAgent, identityText, linkSegments, loadInstructions, loadModels, loadOutputSettings, memoryText, Output, resolveModel, worthSaving, wrap,
  loadTools, localTools, REPO_ROOT, runScript, TOOLS_DIR,
} from '../vercelagent/index.ts';

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: undefined, cacheWrite: undefined },
  outputTokens: { total: 1, text: 1, reasoning: undefined },
};
const finish = (reason: 'stop' | 'tool-calls') => ({ type: 'finish' as const, finishReason: { unified: reason, raw: reason }, usage });

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
  assert.deepEqual(lines, SCENARIO.calls.flatMap((step) =>
    [`→ ${step.tool}`, `${step.outcome === 'ok' ? '←' : '✗'} ${step.tool}`]));
});

test('tools are told which agent runs them', async () => {
  assert.equal(await runScript(['printenv', 'AGENT_NAME']), 'Vercel Agent');
});

test('an omitted optional argument runs the tool without it', async () => {
  const agent = buildAgent(scriptedModel([['time', {}]]), localTools);
  const answer = JSON.stringify((await ask(agent, 'What time is it?', [], quiet)).at(-1));
  assert.match(answer, /\d{2}:\d{2}/);
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
  assert.equal(await execute({ who: 'world' }, options), 'hello world');
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

test('tools run one at a time by default', async () => {
  const { overlaps, tools } = trackedTools();
  await ask(buildAgent(scriptedModel([['a', {}], ['b', {}], ['c', {}]]), tools), 'go', [], quiet);
  assert.deepEqual(overlaps, [1, 1, 1]);
});

test('parallel runs tools concurrently', async () => {
  const { overlaps, tools } = trackedTools();
  await ask(buildAgent(scriptedModel([['a', {}], ['b', {}], ['c', {}]]), tools, true), 'go', [], quiet);
  assert.ok(Math.max(...overlaps) > 1, `expected overlap, got ${overlaps}`);
});

test('model profiles come from the shared models file', () => {
  const saved = { ...process.env };
  try {
    process.env.OPENAI_API_KEY = 'test-key-not-real';
    for (const name of ['LOCAL_LLM_BASE_URL', 'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY']) delete process.env[name];
    const models = loadModels();
    const local = resolveModel(models); // "default": "local"
    const fallback = models.profiles!.local; // Whatever the file names, so editing it never breaks this test
    assert.deepEqual([local.baseURL, local.model], [fallback.base_url, fallback.model]);
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
