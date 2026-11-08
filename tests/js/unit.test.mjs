/**
 * 外壳的离线测试（node --test）。
 *
 * 不依赖 Harness：只测帧、内容提取、以及与真实 Python 内核的进程级往返
 * （health / tools.schema / shutdown）。llm 与 tools 两条桥依赖宿主包，
 * 在 Harness 内验证（见 release/README.md 的手工验收步骤）。
 */
import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { registerMemoryCommand } from '../../src/js/commands.js'
import { CorePrompt, CORE_SECTION, SELF_NOTES_CONTEXT }
  from '../../src/js/core-prompt.js'
import { buildFrame, decodeFrame, encodeFrame } from '../../src/js/frames.js'
import { IngestBridge, contentToText } from '../../src/js/ingest-bridge.js'
import { defaultSettings, mergeSettings, profileFor, chatCompletion, embedTexts }
  from '../../src/js/model-config.js'
import { registerLongmemoryRoutes, isLoopback, authorizeRequest,
  isAllowedHost, isAllowedOrigin, hostnameOf, tailLog }
  from '../../src/js/stats-route.js'
import { Kernel } from '../../src/js/kernel.js'

const PYTHON = process.env.LONGMEMORY_PYTHON
  || path.resolve('.venv/Scripts/python.exe')
const KERNEL_DIR = path.resolve('src')

function frameLine(frame) {
  return JSON.stringify(frame)
}

test('帧信封包含协议字段，且能原样解析', () => {
  const frame = buildFrame('health', {}, {
    request_id: 'req_1', session_id: 's1', agent_id: 'a1',
  })
  assert.equal(frame.protocol_version, '1.0')
  assert.equal(frame.type, 'health')
  assert.equal(frame.request_id, 'req_1')
  assert.equal(frame.session_id, 's1')
  assert.match(frame.ts, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/)

  const decoded = decodeFrame(frameLine(frame))
  assert.equal(decoded.type, 'health')
  assert.equal(decoded.request_id, 'req_1')
})

test('坏行解析返回 null 而不抛', () => {
  assert.equal(decodeFrame(''), null)
  assert.equal(decodeFrame('not json'), null)
  assert.equal(decodeFrame('{"no":"type"}'), null)
})

test('encodeFrame 输出单行且保留中文', () => {
  const line = encodeFrame('config.get', { 名称: '记忆' })
  assert.equal(line.split('\n').length, 2)
  assert.ok(line.endsWith('\n'))
  assert.ok(line.includes('记忆'))
})

test('内容块转文本：text / json / tool-call', () => {
  const message = {
    content: [
      { type: 'text', text: '第一段' },
      { type: 'json', value: { a: 1 } },
      { type: 'tool-call', name: 'bash', arguments: { command: 'pwd' } },
      { type: 'reasoning', text: '不应出现' },
    ],
  }
  const text = contentToText(message)
  assert.ok(text.includes('第一段'))
  assert.ok(text.includes('{"a":1}'))
  assert.ok(text.includes('[tool-call] bash'))
  assert.ok(!text.includes('不应出现'))
  assert.equal(contentToText({ content: '纯文本' }), '纯文本')
  assert.equal(contentToText({}), '')
})


test('/memory 命令：立即 flush 并回显条数', async () => {
  let definition
  const ctx = { commands: { register: (def) => { definition = def; return () => {} } } }
  const flushed = []
  const dispose = registerMemoryCommand(ctx, {
    bridge: { flush: async (sid) => { flushed.push(sid); return { accepted: true, new_messages: 3 } } },
    trace: () => {},
  })
  assert.equal(typeof dispose, 'function')
  assert.equal(definition.name, 'memory')
  const result = await definition.handler({ agent: { session: { id: 's1' } } })
  assert.equal(result.kind, 'success')
  assert.match(result.text, /3 条/)
  assert.deepEqual(flushed, ['s1'])
})

test('/memory 命令：无缓冲时如实说明', async () => {
  let definition
  const ctx = { commands: { register: (def) => { definition = def; return () => {} } } }
  registerMemoryCommand(ctx, { bridge: { flush: async () => null }, trace: () => {} })
  const result = await definition.handler({ agent: { session: { id: 's1' } } })
  assert.equal(result.kind, 'success')
  assert.match(result.text, /没有待写入/)
})

test('/memory 命令：内核失败转成 error 结果', async () => {
  let definition
  const ctx = { commands: { register: (def) => { definition = def; return () => {} } } }
  registerMemoryCommand(ctx, {
    bridge: { flush: async () => { throw new Error('boom') } }, trace: () => {},
  })
  const result = await definition.handler({ agent: { session: { id: 's1' } } })
  assert.equal(result.kind, 'error')
  assert.match(result.text, /boom/)
})

test('缺少 commands 服务时不注册、不抛', () => {
  const dispose = registerMemoryCommand({}, { bridge: {}, trace: () => {} })
  assert.equal(dispose, null)
})

test('管理路由：stats 端点', async () => {
  let route
  const ctx = { webServer: { register: (r) => { route = r; return () => {} } } }
  registerLongmemoryRoutes(ctx, {
    kernel: { request: async (type) => {
      assert.equal(type, 'stats')
      return { events: 2, experiences: 5, cognitions: 4, messages: 10, db_path: '/tmp/x.db' }
    } },
    trace: () => {},
  })
  assert.equal(route.kind, 'prefix')
  assert.equal(route.path, '/longmemory')
  let body = ''
  const res = { writeHead(status) { this.status = status }, end(text) { body = text } }
  await route.handler({ method: 'GET', url: '/longmemory/stats',
                        headers: { host: '127.0.0.1:51512' },
                        socket: { remoteAddress: '127.0.0.1' } }, res)
  assert.equal(res.status, 200)
  assert.equal(JSON.parse(body).events, 2)
})

test('管理路由：api 端点转发到内核 admin 帧', async () => {
  let route
  const ctx = { webServer: { register: (r) => { route = r; return () => {} } } }
  const seen = []
  registerLongmemoryRoutes(ctx, {
    kernel: { request: async (type, payload) => {
      seen.push([type, payload.op])
      return { ok: true, op: payload.op, result: { rows: [], total: 0 } }
    } },
    trace: () => {},
  })
  let body = ''
  const res = { writeHead(status) { this.status = status }, end(text) { body = text } }
  // 伪请求：只实现 readJsonBody 需要的事件接口
  const req = {
    method: 'POST', url: '/longmemory/api',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json',
               origin: 'http://127.0.0.1:51512' },
    socket: { remoteAddress: '127.0.0.1' },
    on(event, handler) {
      if (event === 'data') handler(Buffer.from(JSON.stringify({ op: 'list', table: 'event' })))
      if (event === 'end') handler()
      return this
    },
  }
  await route.handler(req, res)
  assert.equal(res.status, 200)
  assert.deepEqual(seen, [['admin', 'list']])
  assert.equal(JSON.parse(body).ok, true)
})

test('管理路由：非回环请求被拒', async () => {
  let route
  const ctx = { webServer: { register: (r) => { route = r; return () => {} } } }
  registerLongmemoryRoutes(ctx, { kernel: { request: async () => ({}) }, trace: () => {} })
  const res = { writeHead(status) { this.status = status }, end() {} }
  await route.handler({ method: 'GET', url: '/longmemory/stats',
                        socket: { remoteAddress: '10.0.0.8' } }, res)
  assert.equal(res.status, 403)
  assert.equal(isLoopback({ socket: { remoteAddress: '::1' } }), true)
  assert.equal(isLoopback({ socket: { remoteAddress: '10.0.0.8' } }), false)
})

test('管理路由：logs 端点返回行数组', async () => {
  let route
  const ctx = { webServer: { register: (r) => { route = r; return () => {} } } }
  registerLongmemoryRoutes(ctx, { kernel: { request: async () => ({}) }, trace: () => {} })
  let body = ''
  const res = { writeHead(status) { this.status = status }, end(text) { body = text } }
  await route.handler({ method: 'GET', url: '/longmemory/logs?source=shell&lines=5',
                        headers: { host: 'localhost:51512' },
                        socket: { remoteAddress: '127.0.0.1' } }, res)
  assert.equal(res.status, 200)
  assert.ok(Array.isArray(JSON.parse(body).lines))
})

test('准入判定：Host 必须是本机名（防 DNS rebinding）', () => {
  assert.equal(hostnameOf('127.0.0.1:51512'), '127.0.0.1')
  assert.equal(hostnameOf('LOCALHOST:8080'), 'localhost')
  assert.equal(hostnameOf('[::1]:51512'), '::1')
  assert.equal(hostnameOf(''), '')
  assert.equal(isAllowedHost('127.0.0.1:51512'), true)
  assert.equal(isAllowedHost('localhost'), true)
  assert.equal(isAllowedHost('evil.example:51512'), false)
  assert.equal(isAllowedHost('127.0.0.1.evil.example'), false)
})

test('准入判定：Origin 必须是本机页面', () => {
  assert.equal(isAllowedOrigin(undefined), true)
  assert.equal(isAllowedOrigin('http://127.0.0.1:51512'), true)
  assert.equal(isAllowedOrigin('http://localhost:51512'), true)
  assert.equal(isAllowedOrigin('https://evil.example'), false)
  assert.equal(isAllowedOrigin('null'), false)
})

test('准入判定：跨站写请求被拒（三类都验）', () => {
  const base = { socket: { remoteAddress: '127.0.0.1' } }
  // 1) 外来 Origin
  assert.equal(authorizeRequest({ ...base, method: 'POST',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json',
               origin: 'https://evil.example' } },
  { write: true }).status, 403)
  // 2) Sec-Fetch-Site: cross-site
  assert.equal(authorizeRequest({ ...base, method: 'POST',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json',
               'sec-fetch-site': 'cross-site' } }, { write: true }).error,
  'cross_site_blocked')
  // 3) text/plain 简单请求（免预检，必须挡）
  assert.equal(authorizeRequest({ ...base, method: 'POST',
    headers: { host: '127.0.0.1:51512', 'content-type': 'text/plain',
               origin: 'http://127.0.0.1:51512' } },
  { write: true }).status, 415)
  // 4) 本机正常写请求放行
  assert.equal(authorizeRequest({ ...base, method: 'POST',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json; charset=utf-8',
               origin: 'http://127.0.0.1:51512' } }, { write: true }), null)
  // 5) 读请求不要求 content-type
  assert.equal(authorizeRequest({ ...base, method: 'GET',
    headers: { host: '127.0.0.1:51512' } }, { write: false }), null)
})

test('投递失败不丢消息：回滚缓冲，重试只发一次', async () => {
  let attempts = 0
  const sent = []
  const kernel = {
    request: async (type, payload) => {
      attempts += 1
      if (attempts === 1) throw new Error('内核应答超时: ingest.request')
      sent.push(payload)
      return { accepted: true, new_messages: payload.messages.length }
    },
  }
  const bridge = new IngestBridge({}, kernel, { trace: () => {} })
  bridge.buffers.set('s1', {
    messages: [{ msg_id: 'm1', seq: 1, role: 'user', content: 'a' },
               { msg_id: 'm2', seq: 2, role: 'assistant', content: 'b' }],
    lastSeq: 2, timer: null,
  })

  await assert.rejects(() => bridge.flush('s1'), /超时/)
  assert.equal(bridge.buffers.get('s1').messages.length, 2, '失败的这批必须留在缓冲里')

  const result = await bridge.flush('s1')
  assert.equal(result.accepted, true)
  assert.equal(sent.length, 1)
  assert.deepEqual(sent[0].messages.map(item => item.msg_id), ['m1', 'm2'])
  assert.deepEqual(sent[0].cursor, { last_seq: 0 })
  assert.equal(bridge.buffers.get('s1').messages.length, 0, '成功后缓冲清空')
})

test('投递失败后新事件仍接在已回滚的消息后面', async () => {
  const calls = []
  const kernel = {
    request: async (type, payload) => {
      calls.push(payload.messages.map(item => item.msg_id))
      if (calls.length === 1) throw new Error('boom')
      return { accepted: true }
    },
  }
  const bridge = new IngestBridge({}, kernel, { trace: () => {} })
  bridge.buffers.set('s1', {
    messages: [{ msg_id: 'm1', seq: 1 }, { msg_id: 'm2', seq: 2 }],
    lastSeq: 2, timer: null,
  })
  await assert.rejects(() => bridge.flush('s1'))
  bridge.buffers.get('s1').messages.push({ msg_id: 'm3', seq: 3 })
  await bridge.flush('s1')
  assert.deepEqual(calls[1], ['m1', 'm2', 'm3'], '顺序不能乱')
})

test('准入判定：路由层真的拒掉外来 Origin（不是只测函数）', async () => {
  let route
  const ctx = { webServer: { register: (r) => { route = r; return () => {} } } }
  let called = 0
  registerLongmemoryRoutes(ctx, {
    kernel: { request: async () => { called += 1; return { ok: true } } },
    trace: () => {},
  })
  let body = ''
  const res = { writeHead(status) { this.status = status }, end(text) { body = text } }
  const req = {
    method: 'POST', url: '/longmemory/api',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json',
               origin: 'https://evil.example' },
    socket: { remoteAddress: '127.0.0.1' },
    on(event, handler) {
      if (event === 'data') handler(Buffer.from('{"op":"tables"}'))
      if (event === 'end') handler()
      return this
    },
  }
  await route.handler(req, res)
  assert.equal(res.status, 403)
  assert.equal(JSON.parse(body).error, 'origin_not_allowed')
  assert.equal(called, 0, '被拒的请求不应触达内核')
})

test('tailLog 只读文件尾部，且丢掉被截断的首行', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'longmemory-logs-'))
  try {
    const lines = []
    for (let index = 0; index < 500; index += 1) {
      lines.push(`2026-11-03T10:00:00.000Z 第 ${index} 行日志`)
    }
    // 真实日志以换行结尾：split 会多一个空串，不能被当成"最后一行"
    await writeFile(path.join(dir, 'shell.log'), `${lines.join('\n')}\n`, 'utf8')
    const tail = tailLog('shell', { baseDir: dir, maxBytes: 2000 })
    assert.ok(tail.length > 1)
    assert.ok(tail.length < 500, '不该把整文件读回来')
    assert.equal(tail[tail.length - 1], '2026-11-03T10:00:00.000Z 第 499 行日志')
    assert.ok(tail[0].startsWith('2026-11-03T10:00:00.000Z 第 '),
      '首行必须完整，不能是半截时间戳')
    assert.deepEqual(tailLog('shell', { baseDir: dir, maxBytes: 2000 }).slice(-1),
      ['2026-11-03T10:00:00.000Z 第 499 行日志'], '末尾空行不应占位')
    // 文件不存在时返回空数组，而不是抛
    assert.deepEqual(tailLog('kernel', { baseDir: dir }), [])
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('内核起不来：不抛、进程置空、请求快速失败', async () => {
  const kernel = new Kernel({
    python: path.join(tmpdir(), 'definitely-no-such-python-9d3f'),
    script: path.resolve('src/main.py'),
    logger: { warn() {}, debug() {}, error() {} },
  })
  assert.doesNotThrow(() => kernel.start())
  await new Promise(resolve => setTimeout(resolve, 300))
  assert.equal(kernel.process, null, 'spawn 失败必须清掉进程对象')
  await assert.rejects(() => kernel.request('health', {}, { timeoutMs: 500 }))
  kernel.stop()
})

/* ---------------- 路由的错误分支 ---------------- */

function makeRoute(kernel, extra = {}) {
  let route
  const ctx = { webServer: { register: (r) => { route = r; return () => {} } } }
  registerLongmemoryRoutes(ctx, { kernel, trace: () => {}, ...extra })
  return route
}

async function invoke(route, {
  method = 'GET', url = '/longmemory/stats', body = '',
  headers = { host: '127.0.0.1:51512' }, remoteAddress = '127.0.0.1',
} = {}) {
  const res = {
    status: 0,
    writeHead(status) { this.status = status },
    end(text) { this.body = text },
  }
  const req = {
    method, url, headers, socket: { remoteAddress },
    on(event, handler) {
      if (event === 'data' && body) handler(Buffer.from(body))
      if (event === 'end') handler()
      return this
    },
  }
  await route.handler(req, res)
  return { status: res.status, body: JSON.parse(res.body || '{}') }
}

test('路由错误分支：405 / 404 / 503 / 400', async () => {
  const kernel = {
    request: async () => { throw new Error('内核应答超时: admin') },
  }
  const route = makeRoute(kernel)

  const wrongMethod = await invoke(route, {
    method: 'GET', url: '/longmemory/api',
  })
  assert.equal(wrongMethod.status, 405)
  assert.equal(wrongMethod.body.error, 'method_not_allowed')

  const notFound = await invoke(route, { url: '/longmemory/nope' })
  assert.equal(notFound.status, 404)
  assert.equal(notFound.body.error, 'not_found')

  const unavailable = await invoke(route, {
    method: 'POST', url: '/longmemory/api', body: '{"op":"tables"}',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json' },
  })
  assert.equal(unavailable.status, 503)
  assert.equal(unavailable.body.error, 'kernel_unavailable')

  const badJson = await invoke(route, {
    method: 'POST', url: '/longmemory/api', body: '{oops',
    headers: { host: '127.0.0.1:51512', 'content-type': 'application/json' },
  })
  assert.equal(badJson.status, 400, '坏 JSON 是客户端错误，不是内核不可用')
  assert.equal(badJson.body.error, 'bad_request')
})

test('模型测试路由：宿主槽借外壳的 ctx.llm，自定义端点直连', async () => {
  const settings = { 'model.jev': { mode: 'host', provider: 'p', model: 'm' } }
  const route = makeRoute({ request: async () => ({}) }, {
    getSettings: () => settings,
    testLlm: async profile => {
      assert.equal(profile.mode, 'host')
      return { content: 'OK', model: 'host-model' }
    },
  })
  const headers = { host: '127.0.0.1:1', 'content-type': 'application/json' }
  const host = await invoke(route, {
    method: 'POST', url: '/longmemory/test', body: '{"kind":"jev"}', headers,
  })
  assert.equal(host.status, 200)
  assert.equal(host.body.ok, true)
  assert.equal(host.body.sample, 'OK')
  assert.equal(host.body.model, 'host-model')
  assert.equal(typeof host.body.ms, 'number')

  settings['model.jev'] = { mode: 'custom', baseUrl: 'http://h/v1', model: 'c1' }
  const original = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: true,
    text: async () => JSON.stringify({
      model: 'c1', choices: [{ message: { content: 'OK' } }],
    }),
  })
  try {
    const custom = await invoke(route, {
      method: 'POST', url: '/longmemory/test', body: '{"kind":"jev"}', headers,
    })
    assert.equal(custom.status, 200)
    assert.equal(custom.body.ok, true)
    assert.equal(custom.body.model, 'c1')
  } finally {
    globalThis.fetch = original
  }
})

test('核心记忆：注册 section/context，按版本更新文本', async () => {
  const calls = []
  const disposers = []
  const ctx = {
    effect: fn => { const disposer = fn(); disposers.push(disposer); return disposer },
    systemPrompt: {
      section: options => { calls.push(['section', options]); return () => {} },
      context: options => { calls.push(['context', options]); return () => {} },
    },
  }
  let payload = {
    enabled: true,
    version: 'v1',
    text: '<long_term_memory><self_configured>x</self_configured></long_term_memory>',
    notes_version: 'n1',
    notes_text: '<self_notes source="assistant" revision="1">y</self_notes>',
  }
  const kernel = { request: async type => { assert.equal(type, 'core.get'); return payload } }
  const prompt = new CorePrompt(ctx, kernel, { trace: () => {} })

  assert.equal(prompt.attach(), true)
  const section = calls.find(item => item[0] === 'section')[1]
  const context = calls.find(item => item[0] === 'context')[1]
  assert.equal(section.name, CORE_SECTION)
  assert.equal(section.order, -2000)
  assert.equal(section.interpolate, false)
  assert.equal(context.name, SELF_NOTES_CONTEXT)
  assert.equal(context.order, 850)
  assert.equal(section.text(), '', '刷新前为空')
  assert.equal(context.text(), '', '刷新前为空')

  await prompt.refresh()
  assert.match(section.text(), /<long_term_memory>/)
  assert.match(context.text(), /<self_notes/)

  payload = { ...payload, text: '<changed/>' }   // 版本不变时不应替换
  await prompt.refresh()
  assert.match(section.text(), /<long_term_memory>/)

  payload = { ...payload, version: 'v2', text: '<long_term_memory>new</long_term_memory>' }
  await prompt.refresh()
  assert.match(section.text(), /new/)
})

test('核心记忆：宿主没有 systemPrompt 时安全跳过', () => {
  const prompt = new CorePrompt({}, {}, { trace: () => {} })
  assert.equal(prompt.attach(), false)
  prompt.dispose()
})

/* ---------------- 三槽模型配置 ---------------- */

test('模型三槽：默认继承宿主，存储值只覆盖给出的字段', () => {
  const defaults = defaultSettings({ provider: 'workbuddy', model: 'deepseek-v4.1-flash' })
  assert.equal(defaults['model.jev'].mode, 'host')
  assert.equal(defaults['model.jev'].provider, 'workbuddy')
  assert.equal(defaults['model.extract'].model, 'deepseek-v4.1-flash')
  assert.equal(defaults.embedding.model, 'bge-m3')
  assert.equal(defaults.embedding.dim, 1024)

  const merged = mergeSettings({ 'model.jev': {
    mode: 'custom', baseUrl: 'http://h/v1', apiKey: 'sk-x',
  } }, defaults)
  assert.equal(merged['model.jev'].mode, 'custom')
  assert.equal(merged['model.jev'].apiKey, 'sk-x')
  assert.equal(merged['model.jev'].model, 'deepseek-v4.1-flash', '缺字段沿用默认')
  assert.equal(merged['model.extract'].mode, 'host')
  assert.equal(merged.embedding.baseUrl, defaults.embedding.baseUrl)
  assert.equal(mergeSettings(null, defaults).embedding.dim, 1024, '没存过就全默认')
})

test('profileFor：jev 用 JEV 槽，其余文本调用用提取槽', () => {
  const settings = { 'model.jev': { model: 'a' }, 'model.extract': { model: 'b' } }
  assert.equal(profileFor(settings, 'jev').model, 'a')
  for (const purpose of ['summary', 'consolidate', 'insight', 'self', undefined]) {
    assert.equal(profileFor(settings, purpose).model, 'b')
  }
  assert.deepEqual(profileFor(null, 'jev'), {})
})

test('chatCompletion：OpenAI 兼容端点带 Bearer，取 choices[0]', async () => {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({
        model: 'm1', usage: { total_tokens: 3 },
        choices: [{ message: { content: 'OK' } }],
      }),
    }
  }
  try {
    const result = await chatCompletion({
      baseUrl: 'http://127.0.0.1:11434/v1/', model: 'm1', apiKey: 'sk-1', timeoutMs: 5000,
    }, { system: 'sys', user: '只回复 OK' })
    assert.equal(result.content, 'OK')
    assert.equal(result.model, 'm1')
    assert.equal(calls[0].url, 'http://127.0.0.1:11434/v1/chat/completions',
      '拼接前去掉尾部斜杠')
    const body = JSON.parse(calls[0].options.body)
    assert.equal(body.model, 'm1')
    assert.equal(body.temperature, 0)
    assert.deepEqual(body.messages.map(item => item.role), ['system', 'user'])
    assert.equal(body.messages[1].content, '只回复 OK')
    assert.equal(calls[0].options.headers.authorization, 'Bearer sk-1')
  } finally {
    globalThis.fetch = original
  }
})

test('chatCompletion：缺 baseUrl 直接报错；HTTP 失败带状态码', async () => {
  await assert.rejects(() => chatCompletion({ model: 'm1' }, { user: 'x' }), /baseUrl/)

  const original = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: false, status: 500, text: async () => 'internal error',
  })
  try {
    await assert.rejects(
      () => chatCompletion({ baseUrl: 'http://h/v1', model: 'm1' }, { user: 'x' }),
      /HTTP 500/)
  } finally {
    globalThis.fetch = original
  }
})

test('embedTexts：openai 走 /embeddings，ollama 走 /api/embed', async () => {
  const urls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    urls.push(url)
    const isOllama = url.endsWith('/api/embed')
    assert.equal(JSON.parse(options.body).input.length, 1)
    return {
      ok: true,
      text: async () => JSON.stringify(isOllama
        ? { embeddings: [[0.1, 0.2]] }
        : { data: [{ embedding: [0.1, 0.2] }] }),
    }
  }
  try {
    const openai = await embedTexts(
      { baseUrl: 'http://h/v1', model: 'bge-m3', dim: 2 }, ['x'])
    assert.deepEqual(openai, [[0.1, 0.2]])
    const ollama = await embedTexts(
      { baseUrl: 'http://h/v1', model: 'bge-m3', flavor: 'ollama', dim: 2 }, ['x'])
    assert.deepEqual(ollama, [[0.1, 0.2]])
    assert.deepEqual(urls, ['http://h/v1/embeddings', 'http://h/api/embed'],
      'ollama 去掉 /v1 用原生接口')
    assert.deepEqual(await embedTexts({ baseUrl: 'http://h/v1', model: 'm' }, []), [],
      '空输入不发请求')
  } finally {
    globalThis.fetch = original
  }
})

test('embedTexts：维度与配置不符时报错', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: true,
    text: async () => JSON.stringify({ data: [{ embedding: [0.1, 0.2, 0.3] }] }),
  })
  try {
    await assert.rejects(
      () => embedTexts({ baseUrl: 'http://h/v1', model: 'm', dim: 1024 }, ['x']),
      /维度/)
  } finally {
    globalThis.fetch = original
  }
})
