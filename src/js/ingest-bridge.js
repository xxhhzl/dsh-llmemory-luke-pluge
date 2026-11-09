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

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import path from 'node:path'

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
    trace = () => {}, pendingPath = '',
  } = {}) {
    this.trace = trace;
    this.ctx = ctx
    this.kernel = kernel
    this.agentId = agentId
    this.debounceMs = Math.max(200, Number(debounceMs) || 2500)
    this.timeoutMs = timeoutMs
    this.logger = logger
    this.buffers = new Map()   // sessionId -> { messages: [], lastSeq: null, timer }
    // job_id -> { sessionId, messages }：已回执、尚未收到 completed
    this.pending = new Map()
    this.pendingPath = pendingPath || ''
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
    // 去抖缓冲也落盘：外壳被强杀时，这里是这批消息唯一的副本
    this._saveQueue()

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
    const messages = buffer.messages.splice(0, buffer.messages.length)
    this.trace?.(`flush session=${sessionId} n=${messages.length}`)
    try {
      const result = await this.#deliver(sessionId, messages)
      this._saveQueue()
      this.trace?.(`flush ok session=${sessionId} accepted=${result?.accepted}`)
      return result
    } catch (error) {
      // 已经答应过回执的这批消息还没落库：放回队首等下次投递，
      // 否则一次超时就永久丢消息（内核按 msg_id 去重，重发是安全的）。
      buffer.messages.unshift(...messages)
      this._saveQueue()
      this.trace?.(`flush retry session=${sessionId} n=${messages.length}: ${error?.message ?? error}`)
      throw error
    }
  }

  /**
   * 发一次 ingest.request。
   * 回执 accepted 时把这批消息记为待确认——回执不等于落库。
   */
  async #deliver(sessionId, messages) {
    const result = await this.kernel.request('ingest.request', {
      messages,
      cursor: { last_seq: messages[0].seq - 1 },
    }, { sessionId, agentId: this.agentId, timeoutMs: this.timeoutMs })
    if (result?.accepted && result?.job_id) {
      this.pending.set(result.job_id, { sessionId, messages })
      this._saveQueue()
    }
    return result
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
      const raw = readFileSync(this.pendingPath, 'utf8')
      const parsed = JSON.parse(raw)
      for (const item of parsed?.pending ?? []) {
        if (item?.job_id && Array.isArray(item.messages) && item.messages.length) {
          this.pending.set(item.job_id, {
            sessionId: item.sessionId ?? '', messages: item.messages,
          })
        }
      }
      // 还没发出去的去抖缓冲：外壳被强杀时它们是唯一的副本
      for (const [sessionId, item] of Object.entries(parsed?.unsent ?? {})) {
        if (!sessionId || !Array.isArray(item?.messages) || !item.messages.length) continue
        this.buffers.set(sessionId, {
          messages: item.messages,
          lastSeq: Number.isFinite(item.lastSeq) ? item.lastSeq : null,
          timer: null,
        })
      }
      if (this.pending.size || this.buffers.size) {
        this.trace?.(`queue restored: pending=${this.pending.size} unsent=${this.buffers.size}`)
      }
    } catch (error) {
      // 文件不存在 / 内容坏了：当作空队列，绝不因此阻止启动
      if (error?.code !== 'ENOENT') {
        this.logger.warn(`[longmemory] 待确认队列读取失败: ${error.message}`)
      }
    }
  }

  _saveQueue() {
    if (!this.pendingPath) return
    const unsent = {}
    for (const [sessionId, buffer] of this.buffers) {
      if (!buffer.messages.length) continue
      unsent[sessionId] = { messages: buffer.messages, lastSeq: buffer.lastSeq }
    }
    const payload = {
      version: 2,
      pending: [...this.pending.entries()].map(([jobId, item]) => ({
        job_id: jobId, sessionId: item.sessionId, messages: item.messages,
      })),
      unsent,
    }
    try {
      mkdirSync(path.dirname(this.pendingPath), { recursive: true })
      const tmp = `${this.pendingPath}.tmp`
      // 先写临时文件再改名：崩在写一半时不会留下坏 JSON
      writeFileSync(tmp, JSON.stringify(payload), 'utf8')
      renameSync(tmp, this.pendingPath)
    } catch (error) {
      this.logger.warn(`[longmemory] 待确认队列写入失败: ${error.message}`)
    }
  }

  /** 投递所有会话并清理定时器（进程退出前调用）。 */
  async flushAll() {
    const ids = [...this.buffers.keys()]
    await Promise.allSettled(ids.map(id => this.flush(id)))
    for (const buffer of this.buffers.values()) {
      if (buffer.timer) clearTimeout(buffer.timer)
      buffer.timer = null
    }
  }
}
