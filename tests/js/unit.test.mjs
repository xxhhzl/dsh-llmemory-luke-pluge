/**
 * 外壳的离线测试（node --test）。
 *
 * 不依赖 Harness：只测帧、内容提取、以及与真实 Python 内核的进程级往返
 * （health / tools.schema / shutdown）。llm 与 tools 两条桥依赖宿主包，
 * 在 Harness 内验证（见 release/README.md 的手工验收步骤）。
 */
import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { registerMemoryCommand } from '../../src/js/commands.js'
import { CorePrompt, CORE_SECTION, SELF_NOTES_CONTEXT, lastUserText, visibleMessageIds }
  from '../../src/js/core-prompt.js'
import { executeMemoryNote } from '../../src/js/note-call.js'
import { buildFrame, decodeFrame, encodeFrame } from '../../src/js/frames.js'
import { IngestBridge, contentToText } from '../../src/js/ingest-bridge.js'
import { redact } from '../../src/js/redact.js'
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

test('seq=0 的事件不能被当成重复丢掉', () => {
  // 宿主事件序号从 0 开始（session/permission 占 0）；缓冲初始 lastSeq=0
  // 会把真正的 seq=0 误判为「已见过」。
  let handler
  const bridge = new IngestBridge(
    { on: (name, fn) => { handler = fn; return () => {} } },
    { request: async () => ({ accepted: true }) },
    { trace: () => {} })
  bridge.attach()

  handler({ id: 's0' }, {
    type: 'user/message', seq: 0,
    data: { id: 'm0', content: [{ type: 'text', text: '第一条消息' }] },
  })

  const buffer = bridge.buffers.get('s0')
  assert.ok(buffer, '收到事件应建立会话缓冲')
  assert.equal(buffer.messages.length, 1, 'seq=0 的消息必须进缓冲')
  assert.equal(buffer.messages[0].seq, 0)
  assert.equal(buffer.messages[0].content, '第一条消息')
  assert.equal(buffer.lastSeq, 0)
  if (buffer.timer) clearTimeout(buffer.timer)
})

test('重复投递同一 seq 仍然去重', () => {
  let handler
  const bridge = new IngestBridge(
    { on: (name, fn) => { handler = fn; return () => {} } },
    { request: async () => ({ accepted: true }) },
    { trace: () => {} })
  bridge.attach()

  const event = {
    type: 'user/message', seq: 5,
    data: { id: 'm5', content: [{ type: 'text', text: '重复' }] },
  }
  handler({ id: 's5' }, event)
  handler({ id: 's5' }, event)

  const buffer = bridge.buffers.get('s5')
  assert.equal(buffer.messages.length, 1, '同一 seq 不能重复入缓冲')
  if (buffer.timer) clearTimeout(buffer.timer)
})

test('回执只说明「接下了」：要等 completed 才从待确认里删', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-pending-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    const bridge = new IngestBridge({}, {
      request: async () => ({ accepted: true, job_id: 'job_1' }),
    }, { trace: () => {}, pendingPath })
    bridge.buffers.set('s1', {
      messages: [{ msg_id: 'm1', seq: 3, content: 'a' }],
      lastSeq: 3, timer: null,
    })

    await bridge.flush('s1')
    assert.equal(bridge.pendingCount, 1, '回执后必须留在待确认里')
    const jobId = [...bridge.pending.keys()][0]
    assert.ok(readFileSync(pendingPath, 'utf8').includes(jobId), '待确认要落盘')

    assert.equal(bridge.onCommitted(jobId), true)
    assert.equal(bridge.pendingCount, 0, '收到 completed 才删')
    assert.equal(bridge.onCommitted(jobId), false, '重复确认是幂等的')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('完成帧早于回执：任务不能残留在待确认里', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-race-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    const bridge = new IngestBridge({}, {
      request: async (type, payload) => {
        // 内核先推 completed（外壳的 frame 监听据此调 onCommitted），
        // 之后才让回执 resolve——真实时序里完全可能如此。
        bridge.onCommitted(payload.job_id)
        return { accepted: true, job_id: payload.job_id }
      },
    }, { trace: () => {}, pendingPath })
    bridge.buffers.set('s1', {
      messages: [{ msg_id: 'm1', seq: 3, content: 'a' }],
      lastSeq: 3, timer: null,
    })

    await bridge.flush('s1')
    assert.equal(bridge.pendingCount, 0,
      '登记必须先于发送，否则早到的完成帧扑空、任务永久残留')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('外壳自己生成 job_id，并在发送前就登记', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-jobid-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    const sent = []
    const bridge = new IngestBridge({}, {
      request: async (type, payload) => {
        sent.push(payload.job_id)
        // 发送这一刻磁盘上就应该已经有这条登记
        assert.equal(bridge.pendingCount, 1, '发送前必须已登记')
        return { accepted: true, job_id: payload.job_id }
      },
    }, { trace: () => {}, pendingPath })
    bridge.buffers.set('s1', {
      messages: [{ msg_id: 'm1', seq: 3, content: 'a' }],
      lastSeq: 3, timer: null,
    })
    await bridge.flush('s1')
    assert.match(sent[0], /^job_/, '外壳要带上自己的任务 id')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('投递失败会退避重试，而不是放着不管', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-retry-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    let attempts = 0
    const bridge = new IngestBridge({}, {
      request: async (type, payload) => {
        attempts += 1
        if (attempts === 1) throw new Error('内核应答超时: ingest.request')
        return { accepted: true, job_id: payload.job_id }
      },
    }, { trace: () => {}, pendingPath, retryBaseMs: 10, maxRetries: 3 })
    bridge.buffers.set('s1', {
      messages: [{ msg_id: 'm1', seq: 3, content: 'a' }],
      lastSeq: 3, timer: null,
    })

    await assert.rejects(() => bridge.flush('s1'), /超时/)
    assert.equal(bridge.buffers.get('s1').messages.length, 1, '失败的批次要回到缓冲')

    await new Promise(resolve => setTimeout(resolve, 90))
    assert.equal(attempts, 2, '失败后要自己退避重试，不能等下一个事件')
    assert.equal(bridge.buffers.get('s1').messages.length, 0, '重试成功后缓冲清空')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('队列落盘前脱敏：凭据不进明文文件', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-redact-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    const bridge = new IngestBridge({}, {
      request: async (type, payload) => ({ accepted: true, job_id: payload.job_id }),
    }, { trace: () => {}, pendingPath })
    bridge.buffers.set('s1', {
      messages: [{
        msg_id: 'm1', seq: 3,
        content: 'DB password=hunter2hunter，另外 api_key: sk-abcdef0123456789',
      }],
      lastSeq: 3, timer: null,
    })
    bridge._saveQueue()
    const raw = readFileSync(pendingPath, 'utf8')
    assert.ok(!raw.includes('hunter2hunter'), '磁盘上不能留完整口令')
    assert.ok(!raw.includes('sk-abcdef0123456789'), '磁盘上不能留完整密钥')
    assert.ok(raw.includes('[REDACTED]'), '要留下遮盖标记')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('投递中收到新消息：结束后自动补投，不留在缓冲里', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-inflight-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    const sent = []
    let open
    const gate = new Promise(resolve => { open = resolve })
    const bridge = new IngestBridge({}, {
      request: async (type, payload) => {
        sent.push(payload.messages.map(item => item.msg_id))
        if (sent.length === 1) await gate       // 第一批卡住不返回
        return { accepted: true, job_id: payload.job_id }
      },
    }, { trace: () => {}, pendingPath })
    bridge.buffers.set('s1', {
      messages: [{ msg_id: 'm1', seq: 1, content: 'a' }],
      lastSeq: 1, timer: null,
    })

    const first = bridge.flush('s1')            // 不 await：模拟在途
    // 第一批还没结束，新消息进来；它的去抖定时器到期会被 flushing 挡回
    bridge.buffers.get('s1').messages.push({ msg_id: 'm2', seq: 2, content: 'b' })
    open()
    await first
    await new Promise(resolve => setTimeout(resolve, 30))

    assert.deepEqual(sent, [['m1'], ['m2']], '第二批必须在第一批结束后自动补投')
    assert.equal(bridge.buffers.get('s1').messages.length, 0, '缓冲要清空')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('内核在回执后退出：重启从磁盘对账并补投', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-resume-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    const first = new IngestBridge({}, {
      request: async () => ({ accepted: true, job_id: 'job_1' }),
    }, { trace: () => {}, pendingPath })
    first.buffers.set('s1', {
      messages: [{ msg_id: 'm1', seq: 3, content: 'a' }],
      lastSeq: 3, timer: null,
    })
    await first.flush('s1')

    // 模拟外壳重启：内存状态全丢，只剩磁盘上的待确认
    const sent = []
    const second = new IngestBridge({}, {
      request: async (type, payload) => {
        sent.push(payload.messages.map(item => item.msg_id))
        return { accepted: true, job_id: 'job_2' }
      },
    }, { trace: () => {}, pendingPath })
    assert.equal(second.pendingCount, 1, '重启后要能从磁盘恢复待确认')

    const resumed = await second.resumePending()
    assert.equal(resumed, 1)
    assert.deepEqual(sent, [['m1']], '重启后必须补投同一批')
    assert.equal(second.pendingCount, 1, '补投后换成新 job 继续等确认')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('去抖缓冲也落盘：外壳被强杀后重启能继续投递', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-unsent-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    let handler
    const first = new IngestBridge(
      { on: (name, fn) => { handler = fn; return () => {} } },
      { request: async () => ({ accepted: true, job_id: 'job_x' }) },
      { trace: () => {}, pendingPath })
    first.attach()
    handler({ id: 's1' }, {
      type: 'user/message', seq: 3,
      data: { id: 'm1', content: [{ type: 'text', text: '还没发出去' }] },
    })
    const buffer = first.buffers.get('s1')
    if (buffer.timer) clearTimeout(buffer.timer)   // 模拟还没到去抖时间就被强杀

    // 新实例内存全空，只能从磁盘恢复
    const sent = []
    const second = new IngestBridge({}, {
      request: async (type, payload) => {
        sent.push(payload.messages.map(item => item.msg_id))
        return { accepted: true, job_id: 'job_y' }
      },
    }, { trace: () => {}, pendingPath })
    assert.equal(second.buffers.get('s1')?.messages.length, 1,
      '重启要恢复还没发出去的缓冲')
    await second.flushAll()
    assert.deepEqual(sent, [['m1']], '恢复的缓冲要继续投递')
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
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

test('内核起不来：不抛、进程置空、请求快速失败', { skip: process.env.LONGMEMORY_SKIP_SPAWN }, async () => {
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

test('连不上端点：把 cause 的 code 带进错误信息', async () => {
  const original = globalThis.fetch
  const failure = Object.assign(new TypeError('fetch failed'),
    { cause: { code: 'ECONNREFUSED' } })
  globalThis.fetch = async () => { throw failure }
  try {
    // 只有 'fetch failed' 的话，界面上根本看不出是端点没起来
    await assert.rejects(
      () => embedTexts({ baseUrl: 'http://127.0.0.1:11434/v1', model: 'bge-m3' }, ['x']),
      /连不上[\s\S]*ECONNREFUSED/)
  } finally {
    globalThis.fetch = original
  }
})

/* ---------------- 工具桥：HyDE 重试（executeMemorySearch 纯函数） ---------------- */

// tools-bridge.js 顶层 import 宿主包（@deepseek-ai/dsh-tools），离线环境没有它，
// 所以 executeMemorySearch 抽成了纯函数并单独导出；这里剥掉宿主导入后
// 写临时文件再加载（data: URL 与相对导入不兼容，必须走真实文件；
// hyde.js / model-config.js 是它的相对依赖，一并复制到同目录）。
const { executeMemorySearch } = await (async () => {
  const fs = await import('node:fs/promises')
  const path = await import('node:path')
  const dir = path.resolve('.tmp', 'js-test')
  await fs.mkdir(dir, { recursive: true })
  for (const name of ['tools-bridge.js', 'note-call.js', 'hyde.js', 'model-config.js']) {
    const source = await fs.readFile(path.resolve('src/js', name), 'utf8')
    const stripped = source.replace(
      /^import .*@deepseek-ai\/dsh-tools.*\n/m, '')
    await fs.writeFile(path.join(dir, name), stripped, 'utf8')
  }
  return import(`file://${path.join(dir, 'tools-bridge.js').replace(/\\/g, '/')}`)
})()

import { HYDE_SYSTEM_PROMPT, embedQuery, hydeVector } from '../../src/js/hyde.js'

test('HyDE：零命中 → 假想向量重试命中则采用重试结果', async () => {
  const calls = []
  const deps = {
    // 第一次返回原始查询向量，第二次返回 HyDE 向量
    embedQueryFn: async () => [0.1],
    hydeVectorFn: async () => [0.2],
    request: async (type, payload) => {
      calls.push({ type, call: payload.calls[0] })
      const call = payload.calls[0]
      if (call.call_id.endsWith('-h')) {
        return [{ ok: true, data: { count: 1, items: [{ id: 7, via: 'hyde' }] } }]
      }
      return [{ ok: true, data: { count: 0, items: [] } }]
    },
    logger: { debug() {} },
  }
  const entry = await executeMemorySearch(deps, {
    args: { query: '我之前让你做什么' },
  })
  assert.equal(entry.ok, true)
  assert.equal(entry.data.count, 1)
  assert.equal(entry.data.items[0].via, 'hyde')
  // 两次 tools.calls：原始向量一次，HyDE 向量一次（call_id 带 -h）
  assert.equal(calls.length, 2)
  assert.ok(calls[1].call.call_id.endsWith('-h'))
  assert.deepEqual(calls[0].call.query_vector, [0.1])
  assert.deepEqual(calls[1].call.query_vector, [0.2])
})

test('HyDE：正常命中不重试（不多花模型调用）', async () => {
  let requested = 0
  let hydeCalled = 0
  const entry = await executeMemorySearch({
    embedQueryFn: async () => [0.5],
    hydeVectorFn: async () => { hydeCalled += 1; return [0.9] },
    request: async () => {
      requested += 1
      return [{ ok: true, data: { count: 1, items: [{ id: 1 }] } }]
    },
    logger: { debug() {} },
  }, { args: { query: 'drawio 边框' } })
  assert.equal(entry.data.count, 1)
  assert.equal(requested, 1)
  assert.equal(hydeCalled, 0, '命中时不该触发 HyDE 生成')
})

test('HyDE：重试仍零命中 → 保留原始零结果（不报错）', async () => {
  const entry = await executeMemorySearch({
    embedQueryFn: async () => [0.1],
    hydeVectorFn: async () => [0.2],
    request: async () => [{ ok: true, data: { count: 0, items: [] } }],
    logger: { debug() {} },
  }, { args: { query: '查无此物' } })
  assert.equal(entry.ok, true)
  assert.equal(entry.data.count, 0)
})

test('HyDE：无向量（embedding 未配）→ 不重试', async () => {
  let hydeCalled = 0
  const calls = []
  const entry = await executeMemorySearch({
    embedQueryFn: async () => null,
    hydeVectorFn: async () => { hydeCalled += 1; return [0.9] },
    request: async (type, payload) => {
      calls.push(payload.calls[0])
      return [{ ok: true, data: { count: 0, items: [] } }]
    },
    logger: { debug() {} },
  }, { args: { query: '纯词法查询' } })
  assert.equal(entry.data.count, 0)
  assert.equal(calls.length, 1)
  assert.equal(hydeCalled, 0, '没有向量通道就不进 HyDE 分支')
})

test('HyDE：hydeVector 复用提取槽自定义端点；宿主模式/端点失败返回 null', async () => {
  const original = globalThis.fetch
  const seen = []
  globalThis.fetch = async (url, options) => {
    seen.push(url)
    if (url.includes('/chat/completions')) {
      const body = JSON.parse(options.body)
      assert.match(body.messages[0].content, /查询扩展器/)
      assert.match(body.messages[1].content, /用户查询：我之前让你做什么/)
      return { ok: true, text: async () => JSON.stringify(
        { choices: [{ message: { content: '用户让我查 dsh-bilibili 插件并审源码' } }] }) }
    }
    return { ok: true, text: async () => JSON.stringify(
      { data: [{ embedding: [0.3] }] }) }
  }
  try {
    const settings = {
      embedding: { baseUrl: 'http://h/v1', model: 'bge-m3', dim: 1 },
      'model.extract': { mode: 'custom', baseUrl: 'http://h/v1', model: 'gen' },
    }
    const vector = await hydeVector(settings, '我之前让你做什么')
    assert.deepEqual(vector, [0.3])
    assert.equal(seen[0], 'http://h/v1/chat/completions')
    assert.equal(seen[1], 'http://h/v1/embeddings')
    // 生成空内容 → null
    globalThis.fetch = async url => ({ ok: true, text: async () => JSON.stringify(
      url.includes('/chat')
        ? { choices: [{ message: { content: '   ' } }] }
        : { data: [{ embedding: [0.3] }] }) })
    assert.equal(await hydeVector(settings, 'x'), null)
    // 提取槽是宿主模式（无自定义端点）→ 不生成，直接 null
    const hostSettings = { embedding: settings.embedding,
                           'model.extract': { mode: 'host' } }
    assert.equal(await hydeVector(hostSettings, 'x'), null)
    // embedding 未配置 → null
    assert.equal(await embedQuery({}, 'x'), null)
  } finally {
    globalThis.fetch = original
  }
})

/* ---------------- 会话隔离与 memory_note 向量链路（0.5.1） ---------------- */

test('lastUserText：取最后一条用户文本，跳过插件/工具来源', () => {
  assert.equal(lastUserText(null), '')
  assert.equal(lastUserText([
    { role: 'user', content: [{ type: 'text', text: '第一句' }] },
    { role: 'assistant', content: [{ type: 'text', text: '回答' }] },
    { role: 'user', content: [{ type: 'text', text: '  ' }] },
    { role: 'user', content: [{ type: 'text', text: '第二句' }] },
  ]), '第二句')
  assert.equal(lastUserText([
    { role: 'user', source: { kind: 'plugin' }, content: '注入的' },
    { role: 'user', content: '真正的' },
  ]), '真正的')
})

test('core.get 必须带当前会话与查询；召回按会话缓存', async () => {
  const seen = []
  const kernel = {
    request: async (type, data, options) => {
      seen.push({ type, data, options })
      return {
        enabled: true,
        version: 'v1',
        text: '<long_term_memory/>',
        notes_version: 'n1',
        notes_text: '<self_notes/>',
        recall_version: `r-${options?.sessionId || ''}`,
        recall_text: `<recall>${options?.sessionId || ''}:${data?.query_hint || ''}</recall>`,
        recall_items: [],
      }
    },
  }
  const prompt = new CorePrompt({}, kernel, { trace: () => {} })
  await prompt.refresh({ sessionId: 'sA', queryHint: '装插件',
                         recall: true, advanceTurn: true })
  assert.equal(seen[0].type, 'core.get')
  assert.equal(seen[0].options.sessionId, 'sA')
  assert.equal(seen[0].data.query_hint, '装插件')
  assert.equal(seen[0].data.recall, true)
  assert.equal(seen[0].data.advance_turn, true)
  const ctxA = { agent: { session: { id: 'sA' } } }
  const ctxB = { agent: { session: { id: 'sB' } } }
  assert.match(prompt.recallFor(ctxA), /sA:装插件/)

  await prompt.refresh({ sessionId: 'sB', queryHint: '查资料',
                         recall: true, advanceTurn: true })
  assert.match(prompt.recallFor(ctxA), /sA:装插件/, 'A 的文本不被 B 覆盖')
  assert.match(prompt.recallFor(ctxB), /sB:查资料/)
  assert.equal(prompt.currentRecall(), '',
    '没经过 pre-step 时没有「当前会话」，不猜')
})

test('后台刷新默认只编核心文本：不碰召回、不推进冷却', async () => {
  const seen = []
  const kernel = {
    request: async (type, data, options) => {
      seen.push({ data, options })
      return { enabled: true, version: 'v', text: '<core/>' ,
               notes_version: '', notes_text: '',
               recall_text: '<recall>不该出现</recall>', recall_items: [] }
    },
  }
  const prompt = new CorePrompt({}, kernel, { trace: () => {} })
  await prompt.refresh()
  assert.equal(seen[0].data.recall, false)
  assert.equal(seen[0].data.advance_turn, false)
  assert.equal(prompt.currentRecall(), '', '后台刷新不能写入召回缓存')
})

test('召回按组装 context 的会话取文本，两个会话互不串', async () => {
  const kernel = {
    request: async (type, data, options) => ({
      enabled: true, version: 'v', text: '', notes_version: '', notes_text: '',
      recall_text: `<recall>${options?.sessionId}</recall>`,
      recall_items: [], recall_version: 'r',
    }),
  }
  const prompt = new CorePrompt({}, kernel, { trace: () => {} })
  await prompt.refresh({ sessionId: 'A', recall: true, advanceTurn: true })
  await prompt.refresh({ sessionId: 'B', recall: true, advanceTurn: true })
  const ctxA = { agent: { session: { id: 'A' } } }
  const ctxB = { agent: { session: { id: 'B' } } }
  assert.match(prompt.recallFor(ctxA), /<recall>A<\/recall>/)
  assert.match(prompt.recallFor(ctxB), /<recall>B<\/recall>/)
  assert.equal(prompt.recallFor({}), '', '没有会话 context 时不借别的会话')
})

test('同一会话晚到的旧响应不覆盖新召回', async () => {
  const deferred = []
  const kernel = { request: () => new Promise(resolve => deferred.push(resolve)) }
  const prompt = new CorePrompt({}, kernel, { trace: () => {} })
  const oldCall = prompt.refresh({ sessionId: 'S', recall: true, advanceTurn: true })
  const newCall = prompt.refresh({ sessionId: 'S', recall: true, advanceTurn: true })
  const reply = text => ({ enabled: true, version: 'v', text: '',
                           notes_version: '', notes_text: '',
                           recall_text: text, recall_items: [], recall_version: text })
  deferred[1](reply('<recall>NEW</recall>'))
  await newCall
  deferred[0](reply('<recall>OLD</recall>'))
  await oldCall
  assert.match(prompt.recallFor({ agent: { session: { id: 'S' } } }), /NEW/,
    '旧请求晚到不能把新召回覆盖回 OLD')
})

test('agent/pre-step：在组装前用本步会话与用户输入刷新召回，并原样返回 next', async () => {
  const calls = []
  const kernel = {
    request: async (type, data, options) => {
      calls.push({ type, data, options })
      return { enabled: true, version: 'v', text: '', notes_version: '',
               notes_text: '', recall_text: '<recall>x</recall>', recall_items: [] }
    },
  }
  let handler
  const ctx = { on: (name, fn) => { assert.equal(name, 'agent/pre-step'); handler = fn; return () => {} } }
  const prompt = new CorePrompt(ctx, kernel, { trace: () => {} })
  prompt.bindStep()
  const decision = { kind: 'enter', messages: [] }
  const message = { role: 'user', content: [{ type: 'text', text: '上次那个插件怎么装的' }] }
  const result = await handler({ agent: { session: { id: 'sess-1' } }, messages: [message] },
    async () => decision)
  assert.equal(result, decision, 'waterfall 必须原样返回 next() 的结果')
  assert.equal(calls[0].options.sessionId, 'sess-1')
  assert.equal(calls[0].data.query_hint, '上次那个插件怎么装的')
  assert.equal(calls[0].data.recall, true)
  assert.equal(calls[0].data.advance_turn, true)
  assert.match(prompt.currentRecall(), /<recall>/)
})

test('pre-step 没有会话 ID 时清空兜底，不复用上一次的召回', async () => {
  let handler
  const ctx = { on: (name, fn) => { handler = fn; return () => {} } }
  const prompt = new CorePrompt(ctx, { request: async () => ({}) }, { trace: () => {} })
  prompt.bindStep()
  prompt.activeSession = 'old-session'
  prompt.recallBySession.set('old-session', { text: '<recall>OLD</recall>', items: [] })
  await handler({ messages: [{ role: 'user', content: '无会话' }] }, async () => ({ kind: 'enter' }))
  assert.equal(prompt.activeSession, '')
  assert.equal(prompt.currentRecall(), '')
})

test('pre-step 把当前输入的向量随 core.get 传给内核', async () => {
  const calls = []
  const embedded = []
  const kernel = {
    request: async (type, data, options) => {
      calls.push({ type, data, options })
      return { enabled: true, version: 'v', text: '', notes_version: '',
               notes_text: '', recall_text: '', recall_items: [] }
    },
  }
  let handler
  const ctx = { on: (name, fn) => { handler = fn; return () => {} } }
  const prompt = new CorePrompt(ctx, kernel, {
    trace: () => {},
    embedQueryFn: async text => { embedded.push(text); return [0.1, 0.2, 0.3] },
  })
  prompt.bindStep()
  await handler(
    { agent: { session: { id: 's-emb' } },
      messages: [{ role: 'user', content: '换个说法提旧事' }] },
    async () => ({ kind: 'enter' }))

  assert.deepEqual(embedded, ['换个说法提旧事'], '只给本轮输入算一次向量')
  assert.deepEqual(calls[0].data.query_vector, [0.1, 0.2, 0.3],
    '向量要随 core.get 下发，被动召回才能走向量通道')
})

test('后台刷新不算向量：只有用户步骤才付这份延迟', async () => {
  const embedded = []
  const calls = []
  const kernel = {
    request: async (type, data) => {
      calls.push(data)
      return { enabled: true, version: '', text: '', notes_version: '', notes_text: '' }
    },
  }
  const prompt = new CorePrompt({}, kernel, {
    trace: () => {},
    embedQueryFn: async text => { embedded.push(text); return [1] },
  })
  await prompt.refresh({ sessionId: 's1', queryHint: '后台', recall: false })
  assert.equal(embedded.length, 0, '后台刷新不该算向量')
  assert.equal(calls[0].query_vector, null)
})

test('召回等待超时：释放要等 core.get 落地，并带上同一张票', async () => {
  const order = []
  const kernel = {
    request: async (type, data) => {
      if (type === 'core.get') {
        order.push('get:start')
        await new Promise(resolve => setTimeout(resolve, 50))
        order.push('get:done')
        return { enabled: true, version: 'v', text: '', notes_version: '',
                 notes_text: '', recall_text: '', recall_items: [] }
      }
      if (type === 'core.release') {
        order.push(data.ticket ? 'release:ticket' : 'release:no-ticket')
        return {}
      }
      return {}
    },
  }
  let handler
  const ctx = { on: (name, fn) => { handler = fn; return () => {} } }
  const prompt = new CorePrompt(ctx, kernel, { trace: () => {}, recallWaitMs: 10 })
  prompt.bindStep()
  await handler(
    { agent: { session: { id: 's-slow' } },
      messages: [{ role: 'user', content: '很慢的召回' }] },
    async () => ({ kind: 'enter' }))

  await new Promise(resolve => setTimeout(resolve, 120))
  assert.deepEqual(order.filter(item => item !== 'get:start'),
    ['get:done', 'release:ticket'],
    '释放必须在 core.get 之后发出，否则会撤掉上一轮的标记')
})

test('memory_note：用 content 算向量并随调用传给内核', async () => {
  const requests = []
  const entry = await executeMemoryNote({
    embedQueryFn: async text => { assert.equal(text, '导出前统一线宽'); return [0.5, 0.25] },
    request: async (type, payload, options) => {
      requests.push({ type, payload, options })
      return [{ call_id: payload.calls[0].call_id, ok: true, data: { action: 'created' } }]
    },
  }, { args: { content: '导出前统一线宽', kind: 'experience' }, timeoutMs: 1234 })

  assert.equal(entry.ok, true)
  assert.equal(requests[0].type, 'tools.calls')
  const call = requests[0].payload.calls[0]
  assert.equal(call.name, 'memory_note')
  assert.deepEqual(call.query_vector, [0.5, 0.25], '没有向量就退化成字面查重')
  assert.equal(requests[0].options.timeoutMs, 1234)
})

test('memory_note：embedding 失败时不带向量，仍照常调用', async () => {
  let call
  await executeMemoryNote({
    embedQueryFn: async () => null,
    request: async (type, payload) => {
      call = payload.calls[0]
      return [{ ok: true, data: {} }]
    },
  }, { args: { content: '某条经验' } })
  assert.equal(call.name, 'memory_note')
  assert.equal('query_vector' in call, false)
})

// 新增边界：结构化凭据、队列小日志、启动恢复与退避重试。
test('脱敏 JSON 和认证头保留结构且遮盖值', () => {
  const value = '{"api_key":"customsecret123","nested":{"password":"mysecretvalue"}}'
  const masked = redact(value)
  assert.deepEqual(JSON.parse(masked), {
    api_key: '[REDACTED]', nested: { password: '[REDACTED]' },
  })
  assert.equal(redact(masked), masked)
  assert.equal(redact('Authorization: Basic dXNlcjpwYXNz'), 'Authorization: [REDACTED]')
  assert.equal(redact('password=topsecret123'), 'password=[REDACTED]')
})

test('突发消息在完整快照间也可由追加日志恢复', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-journal-'))
  const pendingPath = path.join(dir, 'pending.json')
  try {
    let handler
    const bridge = new IngestBridge({ on: (_name, fn) => { handler = fn; return () => {} } },
      { request: async () => ({ accepted: true }) },
      { pendingPath, saveIntervalMs: 10000 })
    bridge.attach()
    for (let seq = 1; seq <= 2; seq += 1) {
      handler({ id: 's' }, { type: 'user/message', seq,
        data: { id: `m${seq}`, content: `password=secret${seq}0000` } })
    }
    const restored = new IngestBridge({}, {}, { pendingPath })
    assert.deepEqual(restored.buffers.get('s').messages.map(item => item.msg_id), ['m1', 'm2'])
    assert.ok(!readFileSync(`${pendingPath}.journal`, 'utf8').includes('secret20000'))
    // 快照替换后日志已清空，重启不能把第一条追加两次。
    bridge._saveQueue()
    const again = new IngestBridge({}, {}, { pendingPath })
    assert.deepEqual(again.buffers.get('s').messages.map(item => item.msg_id), ['m1', 'm2'])
    if (bridge.buffers.get('s').timer) clearTimeout(bridge.buffers.get('s').timer)
    if (bridge._saveTimer) clearTimeout(bridge._saveTimer)
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('启动 flushAll 失败不取消退避重试；达到上限不忙循环', async () => {
  let attempts = 0
  const bridge = new IngestBridge({}, {
    request: async () => { attempts += 1; throw new Error('offline') },
  }, { retryBaseMs: 50, maxRetries: 1, logger: { warn() {} } })
  bridge.buffers.set('s', {
    messages: [{ msg_id: 'm1', seq: 1, content: 'x' }], lastSeq: 1, timer: null,
  })
  await bridge.flushAll()
  await new Promise(resolve => setTimeout(resolve, 190))
  assert.equal(attempts, 2, '首次失败后应重试一次，但上限后不应再次投递')
  assert.equal(bridge.buffers.get('s').timer, null)
  assert.equal(bridge.buffers.get('s').messages.length, 1)
})

/* ---------------- 被动召回：只排除确实在当前投影里的消息 ---------------- */
test('可见消息 ID 来自会话投影而不是本步收件箱；无投影时不猜测', async () => {
  const agent = { session: { deriveMessages: () => [
    { id: 'old-visible', role: 'user', content: '历史' },
    { id: 'summary', role: 'system', content: '压缩摘要' },
    { role: 'assistant', content: '没有可靠 ID' },
  ] } }
  assert.deepEqual(await visibleMessageIds(agent, [{ id: 'pending', role: 'user' }]),
    ['old-visible', 'summary', 'pending'])
  assert.equal(await visibleMessageIds({ session: { id: 's' } },
    [{ id: 'pending' }]), null, '没有可验证投影时不能仅凭收件箱过滤旧事件')
})

test('pre-step 等下游压缩完成，再给内核发送真实可见消息 ID', async () => {
  let handler
  const projection = [{ id: 'old-visible' }, { id: 'was-visible' }]
  const calls = []
  const ctx = { on: (_name, fn) => { handler = fn; return () => {} } }
  const kernel = { request: async (type, data) => {
    calls.push({ type, data })
    return { enabled: true, version: 'v', text: '', notes_version: '', notes_text: '',
             recall_text: '', recall_items: [] }
  } }
  const agent = { session: { id: 's', deriveMessages: () => projection } }
  const prompt = new CorePrompt(ctx, kernel)
  prompt.bindStep()
  const pending = [{ id: 'new-user', role: 'user', content: '换个话题' }]
  const decision = await handler({ agent, messages: pending }, async () => {
    // 下游将 was-visible 压缩出当前投影，并返回进入模型的消息。
    projection.splice(1, 1)
    projection.push({ id: 'new-summary' })
    return { kind: 'enter', messages: pending }
  })
  assert.equal(decision.kind, 'enter')
  assert.deepEqual(calls[0].data.visible_message_ids,
    ['old-visible', 'new-summary', 'new-user'])
  assert.equal(calls[0].data.query_hint, '换个话题')
})

test('投影不可用时保留保守行为；非模型步骤不计算召回', async () => {
  let handler
  const calls = []
  const prompt = new CorePrompt({ on: (_n, fn) => { handler = fn; return () => {} } },
    { request: async (type, data) => { calls.push(data); return {
      enabled: true, version: '', text: '', notes_version: '', notes_text: '',
      recall_text: '', recall_items: [],
    } } })
  prompt.bindStep()
  await handler({ agent: { session: { id: 's' } }, messages: [{ role: 'user', content: '你好' }] },
    async () => ({ kind: 'enter' }))
  assert.equal(calls[0].visible_message_ids, null)
  await handler({ agent: { session: { id: 's' } } }, async () => ({ kind: 'skip' }))
  assert.equal(calls.length, 1)
})

test('投影或 embedding 太慢时，本轮不注入上一轮快照', async () => {
  let handler
  const ctx = { on: (_n, fn) => { handler = fn; return () => {} } }
  const prompt = new CorePrompt(ctx, { request: async () => ({
    enabled: true, version: 'v', text: '', notes_version: '', notes_text: '',
    recall_text: '<recall>late</recall>', recall_items: [],
  }) }, { recallWaitMs: 10,
    embedQueryFn: async () => { await new Promise(resolve => setTimeout(resolve, 60)); return [1] } })
  prompt.recallBySession.set('s', { text: '<recall>OLD</recall>', items: [] })
  prompt.bindStep()
  const agent = { session: { id: 's', deriveMessages: () => [] } }
  await handler({ agent, messages: [{ role: 'user', content: '旧事' }] },
    async () => ({ kind: 'enter' }))
  assert.equal(prompt.recallFor({ agent }), '', '超时本轮不复用可能重复的旧快照')
  await new Promise(resolve => setTimeout(resolve, 75))
  assert.equal(prompt.recallFor({ agent }), '', '迟到结果也不应在本轮注入')
})

test('慢旧步骤不能在新步骤之后发送 core.get 并覆盖召回', async () => {
  let handler
  const calls = []
  let resolveSlow
  const slow = new Promise(resolve => { resolveSlow = resolve })
  const agent = { session: { id: 's', deriveMessages: () => [] } }
  const prompt = new CorePrompt({ on: (_n, fn) => { handler = fn; return () => {} } },
    { request: async (type, data) => {
      if (type === 'core.get') calls.push(data.query_hint)
      return { enabled: true, version: 'v', text: '', notes_version: '', notes_text: '',
               recall_text: `<recall>${data.query_hint}</recall>`, recall_items: [] }
    } }, { recallWaitMs: 20, embedQueryFn: async text => text === 'old'
      ? slow : [1] })
  prompt.bindStep()
  const old = handler({ agent, messages: [{ role: 'user', content: 'old' }] },
    async () => ({ kind: 'enter' }))
  await new Promise(resolve => setTimeout(resolve, 2))
  const current = handler({ agent, messages: [{ role: 'user', content: 'new' }] },
    async () => ({ kind: 'enter' }))
  await current
  resolveSlow([1])
  await old
  assert.deepEqual(calls, ['new'])
  assert.match(prompt.recallFor({ agent }), /new/)
})
