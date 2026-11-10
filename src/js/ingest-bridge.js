/**
 * 会话桥：Harness 的 session/event → 内核 `ingest.request`。
 *
 * 只把「模型可见的表面事件」转成消息（user / assistant / tool），
 * 与内核 docs/e2e.md 的口径一致；system/developer 事件丢弃。
 * 每个会话按事件序号（seq）增量投递，避免重复入库。
 *
 * 可靠性口径：收到 `ingest.receipt` 只说明内核「接下了」，不等于已落库。
 * 因此这批消息先进**待确认队列**（可选持久化到磁盘），只有收到
 * `ingest.completed` 才删除；内核中途退出时，重启后由 `resumePending()`
 * 对账补投（内核按 msg_id 判重，重发是安全的）。
 */

import { appendFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import { redactMessages } from './redact.js'

const SURFACE = new Map([
  ['user/message', 'user'],
  ['assistant/message', 'assistant'],
  ['tool/result', 'tool'],
])

/** 把内容块数组拼成纯文本；工具结果保留 JSON 形态。 */
export function contentToText(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts = []
  for (const block of content) {
    if (block?.type === 'text' && typeof block.text === 'string') {
      parts.push(block.text)
    } else if (block?.type === 'json' && block.value !== undefined) {
      parts.push(JSON.stringify(block.value))
    } else if (block?.type === 'tool-call') {
      parts.push(`[tool-call] ${block.name ?? ''} ${JSON.stringify(block.arguments ?? {})}`)
    }
  }
  return parts.join('\n')
}

/**
 * 增量投递器：按 session 收集、去抖后发一次 ingest.request。
 */
export class IngestBridge {
  constructor(ctx, kernel, {
    agentId = '', debounceMs = 2500, timeoutMs = 60_000, logger = console,
    trace = () => {}, pendingPath = '', maxRetries = 5, retryBaseMs = 1000,
    saveIntervalMs = 200,
  } = {}) {
    this.trace = trace;
    this.ctx = ctx
    this.kernel = kernel
    this.agentId = agentId
    this.debounceMs = Math.max(200, Number(debounceMs) || 2500)
    this.timeoutMs = timeoutMs
    this.maxRetries = Math.max(0, Number(maxRetries) || 0)
    this.retryBaseMs = Math.max(50, Number(retryBaseMs) || 1000)
    // 完整队列快照做节流；每条新消息另行追加到小日志，避免快照间隔内崩溃漏记。
    this.saveIntervalMs = Math.max(0, Number(saveIntervalMs) || 0)
    this._lastSaveAt = 0
    this._saveTimer = null
    this.logger = logger
    this.buffers = new Map()   // sessionId -> { messages: [], lastSeq: null, timer }
    // job_id -> { sessionId, messages }：已回执、尚未收到 completed
    this.pending = new Map()
    this.pendingPath = pendingPath || ''
    this.journalPath = this.pendingPath ? `${this.pendingPath}.journal` : ''
    this._journalSeq = 0
    this._loadQueue()
    this.dispose = null
  }

  /** 订阅会话事件；返回解绑函数。 */
  attach() {
    const handler = (session, event) => {
      try {
        this.#onEvent(session, event)
      } catch (error) {
        this.logger.warn(`[longmemory] 会话事件处理失败: ${error.message}`)
      }
    }
    // cordis 的 ctx.on 返回 disposer；兼容只有 ctx.off 的实现
    const disposer = this.ctx.on('session/event', handler)
    this.trace?.('bridge attached: session/event')
    this.dispose = typeof disposer === 'function'
      ? disposer
      : () => this.ctx.off?.('session/event', handler)
    return this.dispose
  }

  #onEvent(session, event) {
    const role = SURFACE.get(event?.type)
    if (!role) return
    const sessionId = session?.id
    if (!sessionId) return

    const message = event.type === 'assistant/message'
      ? event.data?.message
      : event.data
    const content = contentToText(message)
    if (!content) return

    const seq = Number(event.seq)
    if (!Number.isFinite(seq)) return

    let buffer = this.buffers.get(sessionId)
    if (!buffer) {
      // lastSeq=null 表示「尚未见过任何序号」：宿主 seq 从 0 开始编号
      // （session/permission 占 0，surface 事件通常从 8 起），初始化为 0
      // 会把真正的 seq=0 事件当成重复丢掉。
      buffer = { messages: [], lastSeq: null, timer: null }
      this.buffers.set(sessionId, buffer)
    }
    if (buffer.lastSeq !== null && seq <= buffer.lastSeq) return
    this.trace?.(`event ${event.type} seq=${seq} role=${role} chars=${content.length}`)

    buffer.messages.push({
      msg_id: String(message?.id ?? `${sessionId}:${seq}`),
      seq,
      role,
      content,
      ts: new Date().toISOString(),
    })
    buffer.lastSeq = seq
    // 先追加小日志确保新消息可恢复，再节流合并完整队列快照。
    this._appendUnsent(sessionId, buffer.messages.at(-1), buffer.lastSeq)
    this._saveQueueDeferred()

    if (buffer.timer) clearTimeout(buffer.timer)
    buffer.timer = setTimeout(() => {
      buffer.timer = null
      this.flush(sessionId).catch(error => {
        this.logger.warn(`[longmemory] 投递失败(${sessionId}): ${error.message}`)
      })
    }, this.debounceMs)
    buffer.timer.unref?.()
  }

  /** 立即投递某会话的缓冲。 */
  async flush(sessionId) {
    const buffer = this.buffers.get(sessionId)
    if (!buffer || buffer.messages.length === 0) return null
    // 同一会话不并发冲刷：两次 flush 会把同一批消息发两遍
    if (buffer.flushing) return null
    buffer.flushing = true
    const messages = buffer.messages.splice(0, buffer.messages.length)
    this.trace?.(`flush session=${sessionId} n=${messages.length}`)
    let failed = false
    try {
      const result = await this.#deliver(sessionId, messages)
      buffer.retry = 0
      this._saveQueue()
      this.trace?.(`flush ok session=${sessionId} accepted=${result?.accepted}`)
      return result
    } catch (error) {
      failed = true
      // 已经答应过回执的这批消息还没落库：放回队首等下次投递，
      // 否则一次超时就永久丢消息（内核按 msg_id 去重，重发是安全的）。
      buffer.messages.unshift(...messages)
      this._saveQueue()
      this.#scheduleRetry(sessionId, buffer)
      this.trace?.(`flush retry session=${sessionId} n=${messages.length}: ${error?.message ?? error}`)
      throw error
    } finally {
      buffer.flushing = false
      // 投递期间又来了新消息：它的去抖定时器到期时会因为 flushing 直接返回，
      // 这里补一次投递，否则这批要等下一个事件或重启才会发。
      if (!failed && buffer.messages.length && !buffer.timer) {
        this.#scheduleFlush(sessionId, buffer, 0)
      }
    }
  }

  /** 安排一次投递；已有定时器时以新的为准。 */
  #scheduleFlush(sessionId, buffer, delayMs) {
    if (buffer.timer) clearTimeout(buffer.timer)
    buffer.timer = setTimeout(() => {
      buffer.timer = null
      this.flush(sessionId).catch(error => {
        this.logger.warn(`[longmemory] 投递失败(${sessionId}): ${error.message}`)
      })
    }, delayMs)
    buffer.timer.unref?.()
  }

  /**
   * 投递失败后的退避重试：不重设定时器的话，这批消息要等新事件、
   * 显式冲刷或重启才会再发，等于「把消息放着不管」。
   */
  #scheduleRetry(sessionId, buffer) {
    buffer.retry = (buffer.retry || 0) + 1
    if (buffer.retry > this.maxRetries) {
      this.logger.warn(`[longmemory] 投递重试已达上限(${sessionId})，等下次事件或重启再试`)
      return
    }
    const delay = Math.min(30_000, this.retryBaseMs * 2 ** (buffer.retry - 1))
    this.#scheduleFlush(sessionId, buffer, delay)
    this.trace?.(`retry scheduled session=${sessionId} attempt=${buffer.retry} delay=${delay}`)
  }

  /**
   * 发一次 ingest.request。
   *
   * 任务 id 由**外壳**生成，并在发送**之前**登记为待确认：回执与完成帧的
   * 到达顺序没有保证（内核可能先推 completed 再让回执 resolve），等回执
   * 回来才登记会让早到的完成帧扑空，任务永久留在队列里。
   */
  async #deliver(sessionId, messages) {
    const jobId = this.#nextJobId()
    this.pending.set(jobId, { sessionId, messages })
    this._saveQueue()
    try {
      const result = await this.kernel.request('ingest.request', {
        job_id: jobId,
        messages,
        cursor: { last_seq: messages[0].seq - 1 },
      }, { sessionId, agentId: this.agentId, timeoutMs: this.timeoutMs })
      if (!result?.accepted) {
        // 没有新消息：内核不会推 completed，自己撤掉登记
        this.pending.delete(jobId)
        this._saveQueue()
      }
      return result
    } catch (error) {
      // 请求失败：撤掉登记，消息由 flush 放回缓冲后再重投
      this.pending.delete(jobId)
      this._saveQueue()
      throw error
    }
  }

  /** 外壳侧的任务 id：进程内单调递增，便于重投时换新号。 */
  #nextJobId() {
    this._jobSeq = (this._jobSeq || 0) + 1
    return `job_${Date.now().toString(36)}_${this._jobSeq}`
  }

  /** 内核确认已落库（ingest.completed）：从待确认队列移除。 */
  onCommitted(jobId) {
    if (!jobId) return false
    const removed = this.pending.delete(jobId)
    if (removed) this._saveQueue()
    return removed
  }

  /**
   * 重启对账：把上次没等到 completed 的批次重投一遍。
   * 内核按 msg_id 判重，重复投递不会产生重复正文。
   */
  async resumePending() {
    if (this.pending.size === 0) return 0
    let resumed = 0
    for (const [jobId, item] of [...this.pending]) {
      try {
        this.pending.delete(jobId)
        const result = await this.#deliver(item.sessionId, item.messages)
        resumed += 1
        this.trace?.(`resume ok job=${jobId} accepted=${result?.accepted}`)
      } catch (error) {
        // 重投失败：留在待确认队列里等下次，不能丢
        this.pending.set(jobId, item)
        this.trace?.(`resume retry job=${jobId}: ${error?.message ?? error}`)
      }
    }
    this._saveQueue()
    return resumed
  }

  /** 待确认条数（诊断用）。 */
  get pendingCount() {
    return this.pending.size
  }

  _loadQueue() {
    if (!this.pendingPath) return
    try {
      const parsed = JSON.parse(readFileSync(this.pendingPath, 'utf8'))
      this._journalSeq = Number(parsed?.journal_seq) || 0
      for (const item of parsed?.pending ?? []) {
        if (item?.job_id && Array.isArray(item.messages) && item.messages.length) {
          this.pending.set(item.job_id, {
            sessionId: item.sessionId ?? '', messages: item.messages,
          })
        }
      }
      for (const [sessionId, item] of Object.entries(parsed?.unsent ?? {})) {
        if (!sessionId || !Array.isArray(item?.messages) || !item.messages.length) continue
        this.buffers.set(sessionId, {
          messages: item.messages,
          lastSeq: Number.isFinite(item.lastSeq) ? item.lastSeq : null,
          timer: null,
        })
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        this.logger.warn(`[longmemory] 队列快照读取失败: ${error.message}`)
      }
    }
    // 快照后追加的消息从小日志恢复；崩在快照替换与日志清空之间时，
    // journal_seq 会跳过已包含在快照中的条目。
    try {
      for (const line of readFileSync(this.journalPath, 'utf8').split('\n')) {
        if (!line.trim()) continue
        let entry
        try { entry = JSON.parse(line) } catch { continue } // 忽略崩溃时未写完的尾行
        if (!Number.isSafeInteger(entry.seq) || entry.seq <= this._journalSeq
            || !entry.sessionId || !entry.message) continue
        const buffer = this.buffers.get(entry.sessionId)
          ?? { messages: [], lastSeq: null, timer: null }
        buffer.messages.push(entry.message)
        buffer.lastSeq = entry.lastSeq
        this.buffers.set(entry.sessionId, buffer)
        this._journalSeq = entry.seq
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        this.logger.warn(`[longmemory] 队列日志读取失败: ${error.message}`)
      }
    }
    if (this.pending.size || this.buffers.size) {
      this.trace?.(`queue restored: pending=${this.pending.size} unsent=${this.buffers.size}`)
    }
  }

  /** 每条新消息只追加一条脱敏记录；全量快照稍后批量合并。 */
  _appendUnsent(sessionId, message, lastSeq) {
    if (!this.journalPath) return
    const seq = ++this._journalSeq
    try {
      mkdirSync(path.dirname(this.journalPath), { recursive: true })
      appendFileSync(this.journalPath, `${JSON.stringify({
        seq, sessionId, message: redactMessages([message])[0], lastSeq,
      })}\n`, 'utf8')
    } catch (error) {
      this.logger.warn(`[longmemory] 队列日志写入失败: ${error.message}`)
      this._saveQueue() // 写日志失败则退回完整快照
    }
  }

  /**
   * 节流写盘：距上次写盘足够久就立即写，否则合并成一次。
   *
   * 单条消息已由追加日志同步持久化；这里仅合并快照，
   * 减少「每条都全量重序列化整个队列」的事件循环阻塞。
   */
  _saveQueueDeferred() {
    if (!this.pendingPath) return
    const now = Date.now()
    const elapsed = now - (this._lastSaveAt || 0)
    if (elapsed >= this.saveIntervalMs) {
      this._saveQueue()
      return
    }
    if (this._saveTimer) return
    this._saveTimer = setTimeout(() => {
      this._saveTimer = null
      this._saveQueue()
    }, Math.max(0, this.saveIntervalMs - elapsed))
    this._saveTimer.unref?.()
  }

  _saveQueue() {
    if (!this.pendingPath) return
    const unsent = {}
    for (const [sessionId, buffer] of this.buffers) {
      if (!buffer.messages.length) continue
      // 落盘前脱敏：队列是磁盘明文副本，内核的脱敏要到入库才发生。
      // 投递时用的是脱敏内容，内核再脱敏一次是幂等的，落库结果不变。
      unsent[sessionId] = {
        messages: redactMessages(buffer.messages), lastSeq: buffer.lastSeq,
      }
    }
    const payload = {
      version: 2,
      journal_seq: this._journalSeq,
      pending: [...this.pending.entries()].map(([jobId, item]) => ({
        job_id: jobId, sessionId: item.sessionId,
        messages: redactMessages(item.messages),
      })),
      unsent,
    }
    try {
      mkdirSync(path.dirname(this.pendingPath), { recursive: true })
      const tmp = `${this.pendingPath}.tmp`
      // 先写临时文件再改名：崩在写一半时不会留下坏 JSON
      writeFileSync(tmp, JSON.stringify(payload), 'utf8')
      renameSync(tmp, this.pendingPath)
      // 快照先替换，再清日志；中途崩溃可凭 journal_seq 幂等恢复。
      writeFileSync(this.journalPath, '', 'utf8')
      this._lastSaveAt = Date.now()
    } catch (error) {
      this.logger.warn(`[longmemory] 待确认队列写入失败: ${error.message}`)
    }
  }

  /** 全部尝试投递；仅真正退出时取消重试定时器。 */
  async flushAll({ cancelTimers = false } = {}) {
    const ids = [...this.buffers.keys()]
    await Promise.allSettled(ids.map(id => this.flush(id)))
    if (cancelTimers) {
      for (const buffer of this.buffers.values()) {
        if (buffer.timer) clearTimeout(buffer.timer)
        buffer.timer = null
      }
      if (this._saveTimer) clearTimeout(this._saveTimer)
      this._saveTimer = null
      this._saveQueue()
    }
  }
}
