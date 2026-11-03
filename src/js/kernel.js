/**
 * 内核进程管理：spawn Python、按行收发帧、请求-应答配对。
 *
 * 线程/进程边界只有一条：stdout 是协议通道（一帧一行），stderr 是日志。
 * 内核的 `llm.request` 是**入站请求**（需要外壳回 `llm.result`），
 * 其余请求由外壳发起、按 request_id 配对等待应答。
 */
import { spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { appendFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import readline from 'node:readline'

import { decodeFrame, encodeFrame, frameError, nextRequestId } from './frames.js'
import { logDir, rotateIfNeeded } from './trace.js'

const DEFAULT_TIMEOUT_MS = 30_000

export class Kernel extends EventEmitter {
  /**
   * @param {object} options
   * @param {string} options.python        Python 可执行文件
   * @param {string} options.script        main.py 绝对路径
   * @param {string} [options.cwd]         工作目录（相对路径的库落在它下面）
   * @param {object} [options.logger]      形如 console 的 logger
   */
  constructor({ python, script, cwd, env, logger = console }) {
    super()
    this.python = python
    this.script = script
    this.cwd = cwd
    this.env = env ?? {}
    this.logger = logger
    this.process = null
    this.pending = new Map()
    this.stderr = []
    this.stopped = false
  }

  /** 启动进程并开始读帧；重复调用无副作用。 */
  start() {
    if (this.process) return this.process
    this.process = spawn(this.python, [this.script], {
      cwd: this.cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
        ...this.env,
      },
    })

    const reader = readline.createInterface({ input: this.process.stdout })
    reader.on('line', line => this.#onLine(line))

    const errReader = readline.createInterface({ input: this.process.stderr })
    errReader.on('line', line => {
      // 内核日志只留在本地环形缓冲里，便于失败时给出诊断；同时落盘一份
      this.stderr.push(line)
      try {
        const dir = logDir()
        mkdirSync(dir, { recursive: true })
        const file = path.join(dir, 'kernel.stderr.log')
        rotateIfNeeded(file)
        appendFileSync(file, `${new Date().toISOString()} ${line}\n`, 'utf8')
      } catch {
        /* 诊断失败忽略 */
      }
      if (this.stderr.length > 200) this.stderr.shift()
      if (line.includes('ERROR')) this.logger.warn(`[longmemory:kernel] ${line}`)
    })

    this.process.on('exit', (code, signal) => {
      const wasStopped = this.stopped
      this.process = null
      if (!wasStopped) {
        this.logger.warn(`[longmemory] 内核退出 code=${code} signal=${signal}`)
      }
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer)
        reject(new Error(`内核已退出（code=${code}）`))
      }
      this.pending.clear()
      this.emit('exit', { code, signal })
    })

    this.process.on('error', error => {
      this.logger.warn(`[longmemory] 内核启动失败: ${error.message}`)
      // spawn 失败（python 路径不存在 / 权限）时不会有 exit 事件，
      // 进程对象必须清掉，否则 waitHealthy 会一直等一个死管道。
      if (!this.process?.pid) this.process = null
      // 注意：EventEmitter 的 'error' 没有监听者会直接抛，这里按需发
      if (this.listenerCount('error') > 0) this.emit('error', error)
    })
    return this.process
  }

  /** 把一帧写入内核 stdin。 */
  send(type, data, envelope = {}) {
    if (!this.process?.stdin?.writable) {
      throw new Error('内核未运行：无法发送帧')
    }
    this.process.stdin.write(encodeFrame(type, data, envelope))
  }

  /**
   * 发起一次请求并等待同 request_id 的应答。
   * @returns {Promise<any>} 应答帧的 data
   */
  request(type, data, { sessionId = '', agentId = '', timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    const requestId = nextRequestId(type.replace(/\W+/g, '_'))
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId)
        reject(new Error(`内核应答超时: ${type}`))
      }, timeoutMs)
      this.pending.set(requestId, { resolve, reject, timer, expect: type })
      try {
        this.send(type, data, { request_id: requestId, session_id: sessionId, agent_id: agentId })
      } catch (error) {
        clearTimeout(timer)
        this.pending.delete(requestId)
        reject(error)
      }
    })
  }

  /** 停止进程（幂等）。 */
  stop() {
    this.stopped = true
    const proc = this.process
    if (!proc) return
    try {
      this.send('shutdown', { drain_ms: 1000 })
    } catch {
      /* 进程已不可写：直接 kill */
    }
    const killer = setTimeout(() => {
      try {
        proc.kill()
      } catch {
        /* ignore */
      }
    }, 1500)
    proc.once('exit', () => clearTimeout(killer))
  }

  #onLine(line) {
    const frame = decodeFrame(line)
    if (!frame) {
      if (line.trim()) this.logger.warn(`[longmemory] 非法帧: ${line.slice(0, 200)}`)
      return
    }

    if (frame.type === 'error') {
      const pending = this.pending.get(frame.request_id)
      if (pending) {
        clearTimeout(pending.timer)
        this.pending.delete(frame.request_id)
        pending.reject(frameError(frame))
        return
      }
      this.emit('kernel-error', frameError(frame))
      return
    }

    // 内核发起的请求：由外层处理并回帧
    if (frame.type === 'llm.request') {
      this.emit('llm.request', frame)
      return
    }

    const pending = this.pending.get(frame.request_id)
    if (pending) {
      clearTimeout(pending.timer)
      this.pending.delete(frame.request_id)
      pending.resolve(frame.data)
      return
    }

    this.emit('frame', frame)
  }
}
