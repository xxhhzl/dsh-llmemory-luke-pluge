/**
 * 外壳生命周期测试（node --test）：退出看护、重启去重、dispose 顺序。
 *
 * 用假内核（EventEmitter + 记录调用）跑，不需要 Python 子进程——
 * 沙箱里 Node 抓不到子进程 stdio（spawn EPERM），进程级往返放在
 * tests/test_shell_contract.py 里用 Python 侧验证。
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { DEFAULT_RESTART_DELAYS, createRestartPolicy, restartDelay }
  from '../../src/js/lifecycle.js'
import { createShell } from '../../src/js/shell.js'

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

function fakeKernel() {
  const bus = new EventEmitter()
  const calls = []
  const kernel = {
    process: null,
    calls,
    start() {
      calls.push('start')
      kernel.process = {}
      return kernel.process
    },
    stop() {
      calls.push('stop')
      kernel.process = null
    },
    send(type) { calls.push(`send:${type}`) },
    request(type, data) {
      calls.push(`req:${type}`)
      if (type === 'health') return Promise.resolve({ ok: true })
      if (type === 'tools.schema') return Promise.resolve([])
      if (type === 'ingest.request') {
        // 回显外壳带来的 job_id：与真实内核一致（外壳登记先于发送）
        return Promise.resolve({ accepted: true, job_id: data?.job_id })
      }
      return Promise.resolve({})
    },
    on(event, handler) { bus.on(event, handler); return kernel },
    off(event, handler) { bus.off(event, handler); return kernel },
    emit(event, payload) { return bus.emit(event, payload) },
    listeners(event) { return bus.listenerCount(event) },
  }
  return kernel
}

function makeShell(options = {}, config = {}) {
  const kernel = fakeKernel()
  const ctx = { on: () => () => {}, off: () => {} }
  const hostHandlers = {
    registerKernelTools: () => [],
    handleLlmRequest: async () => ({ ok: true }),
  }
  const shell = createShell(ctx, { dbPath: 'unused.db', ...config }, {
    kernel, restartDelays: [5], trace: () => {}, hostHandlers, ...options,
  })
  return { shell, kernel }
}

test('restartDelay：越界取最后一档，空数组退化为 0', () => {
  assert.deepEqual(DEFAULT_RESTART_DELAYS, [1000, 5000, 30000])
  assert.equal(restartDelay(0), 1000)
  assert.equal(restartDelay(1), 5000)
  assert.equal(restartDelay(99), 30000)
  assert.equal(restartDelay(-3), 1000)
  assert.equal(restartDelay(2, []), 0)
})

test('重启策略：按退避重试，预算用尽后不再调度', async () => {
  let count = 0
  const policy = createRestartPolicy({ delays: [1, 1], restart: async () => { count += 1 } })
  assert.equal(policy.schedule({ code: 1 }), true)
  await sleep(20)
  assert.equal(count, 1)
  assert.equal(policy.schedule({ code: 1 }), true)
  await sleep(20)
  assert.equal(count, 2)
  assert.equal(policy.schedule({ code: 1 }), false, '第三次应被预算拒绝')
  assert.equal(policy.attempts, 2)
})

test('重启策略：close 之后不再重启', async () => {
  let count = 0
  const policy = createRestartPolicy({ delays: [30], restart: async () => { count += 1 } })
  policy.schedule({ code: 1 })
  policy.close()
  await sleep(60)
  assert.equal(count, 0)
  assert.equal(policy.schedule({ code: 1 }), false)
})

test('内核退出会重启，且 llm.request 订阅不重复', async () => {
  const { shell, kernel } = makeShell()
  await shell.start()
  assert.equal(kernel.calls.filter(item => item === 'start').length, 1)
  assert.equal(kernel.listeners('llm.request'), 1)

  kernel.emit('exit', { code: 1, signal: null })
  await sleep(40)

  assert.equal(kernel.calls.filter(item => item === 'start').length, 2, '应重新拉起内核')
  assert.equal(kernel.listeners('llm.request'), 1, '重启不能重复订阅')
  assert.equal(kernel.listeners('exit'), 1, 'exit 订阅也只应有一份')
  await shell.dispose()
})

test('dispose 先排空、再摘模型回调，且幂等', async () => {
  const { shell, kernel } = makeShell()
  await shell.start()

  const order = []
  shell.bridge.flushAll = async () => { order.push('flushAll') }
  const realOff = kernel.off
  kernel.off = (event, handler) => {
    order.push(`off:${event}`)
    return realOff(event, handler)
  }

  await shell.dispose()
  assert.ok(order.includes('flushAll'), '退出前必须排空缓冲')
  assert.ok(order.indexOf('flushAll') < order.indexOf('off:llm.request'),
    '先 flush 再摘 llm 回调，否则最后一批摘要没人应答')
  assert.equal(kernel.listeners('llm.request'), 0)
  assert.equal(kernel.listeners('exit'), 0)
  assert.equal(kernel.calls.filter(item => item === 'stop').length, 1)

  await shell.dispose()      // 再来一次不应重复拆
  assert.equal(kernel.calls.filter(item => item === 'stop').length, 1)
})

test('已 dispose 的 shell 不会再起进程', async () => {
  const { shell, kernel } = makeShell()
  await shell.dispose()
  const result = await shell.start()
  assert.deepEqual(result, { tools: 0 })
  assert.equal(kernel.calls.includes('start'), false)
})

test('dispose 之后不再响应内核退出（不重启）', async () => {
  const { shell, kernel } = makeShell()
  await shell.start()
  await shell.dispose()
  kernel.emit('exit', { code: 1, signal: null })
  await sleep(40)
  assert.equal(kernel.calls.filter(item => item === 'start').length, 1)
})

test('内核自动重启后要补投待确认任务', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-restart-'))
  const pendingPath = path.join(dir, 'pending.json')
  await writeFile(pendingPath, JSON.stringify({
    version: 2,
    pending: [{
      job_id: 'job_old', sessionId: 's1',
      messages: [{ msg_id: 'm1', seq: 3, role: 'user', content: 'a' }],
    }],
    unsent: {},
  }))

  const { shell, kernel } = makeShell({}, {
    dbPath: path.join(dir, 'memory.db'), pendingPath,
  })
  try {
    await shell.start()
    const delivered = () => kernel.calls.filter(item => item === 'req:ingest.request').length
    const before = delivered()

    kernel.emit('exit', { code: 1, signal: null })   // 内核退出，看护策略自动拉起
    await sleep(120)

    assert.ok(delivered() > before, '重启后必须补投，只重连会把消息留在队列里')
    assert.equal(kernel.listeners('frame'), 1, '重启不能重复注册 frame 监听')
  } finally {
    await shell.dispose()
    await rm(dir, { recursive: true, force: true })
  }
})

test('先订阅完成帧再补投：补投期间的 completed 不能丢', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'lm-order-'))
  const pendingPath = path.join(dir, 'pending.json')
  await writeFile(pendingPath, JSON.stringify({
    version: 2,
    pending: [{
      job_id: 'job_old', sessionId: 's1',
      messages: [{ msg_id: 'm1', seq: 3, role: 'user', content: 'a' }],
    }],
    unsent: {},
  }))

  const { shell, kernel } = makeShell({}, {
    dbPath: path.join(dir, 'memory.db'), pendingPath,
  })
  const order = []
  const realOn = kernel.on
  const realRequest = kernel.request
  kernel.on = (event, handler) => {
    if (event === 'frame') order.push('subscribe')
    return realOn(event, handler)
  }
  kernel.request = async (type, ...rest) => {
    if (type === 'ingest.request') order.push('deliver')
    return realRequest(type, ...rest)
  }

  try {
    await shell.start()
    assert.deepEqual(order.slice(0, 2), ['subscribe', 'deliver'],
      '补投前必须先注册完成帧监听，否则这批 completed 收不到')
  } finally {
    await shell.dispose()
    await rm(dir, { recursive: true, force: true })
  }
})
