// Offline tests: a scripted mock model stands in for LM Studio; the local tools run for real.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tool, type ToolSet } from 'ai';
import { convertArrayToReadableStream, MockLanguageModelV4 } from 'ai/test';
import { z } from 'zod';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ask, buildAgent, loadInstructions, loadModels, resolveModel } from '../agent.ts';
import { loadTools, localTools } from '../tools.ts';

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

test('local tools run for real and failures reach the model', async () => {
  const agent = buildAgent(scriptedModel([
    ['greet_user', { name: 'Alice' }], ['count_lines', { file: 'missing-file' }], ['echo_message', { message: 'hi', repeat: 2 }],
  ]), localTools);
  const history = await ask(agent, 'Do everything', [], quiet);
  const answer = JSON.stringify(history.at(-1));
  assert.match(answer, /Hello, Alice!/);
  assert.match(answer, /file not found/);
  assert.match(answer, /HI\\\\nHI/);
});

test('each tool call prints next to its result', async () => {
  let printed = '';
  const agent = buildAgent(scriptedModel([
    ['greet_user', { name: 'Alice' }], ['count_lines', { file: 'missing-file' }], ['echo_message', { message: 'hi' }],
  ]), localTools);
  await ask(agent, 'Do everything', [], { trace: true, write: (text) => { printed += text; } });
  const lines = printed.replace(/\x1b\[[0-9;]*m/g, '').split('\n')
    .filter((line) => /^[→←✗] /.test(line))
    .map((line) => line.slice(0, 2) + line.slice(2).split(/[(:]/)[0]);
  assert.deepEqual(lines, ['→ greet_user', '← greet_user', '→ count_lines', '✗ count_lines',
                           '→ echo_message', '← echo_message']);
});

test('greet_user without a name greets the shell user', async () => {
  const agent = buildAgent(scriptedModel([['greet_user', {}]]), localTools);
  const answer = JSON.stringify((await ask(agent, 'Greet me', [], quiet)).at(-1));
  assert.match(answer, new RegExp(`Hello, ${process.env.USER}!`));
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
    assert.deepEqual([local.baseURL, local.model], ['http://boba:1234/v1', 'qwen/qwen3-coder-30b']);
    assert.equal(local.apiKey, 'lm-studio'); // the OpenAI key is never used for another server
    process.env.LOCAL_LLM_MODEL = 'from-env';
    assert.equal(resolveModel(models, 'local').model, 'from-env');
    assert.equal(resolveModel(models, 'local', { model: 'from-cli' }).model, 'from-cli');
    assert.equal(resolveModel(models, 'openai').apiKey, 'test-key-not-real');
    process.env.OPENAI_API_KEY = '';
    assert.throws(() => resolveModel(models, 'openai'), /OPENAI_API_KEY/);
    assert.throws(() => resolveModel(models, 'nope'), /Unknown model profile 'nope'/);
  } finally {
    process.env = saved;
  }
});

test('instructions come from the shared context file', () => {
  const instructions = loadInstructions();
  assert.match(instructions, /^You are an agent/);
  assert.match(instructions, /shared tools folder/);
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
