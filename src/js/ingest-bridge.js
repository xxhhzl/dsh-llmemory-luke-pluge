/**
 * 会话桥：Harness 的 session/event → 内核 `ingest.request`。
 *
 * 只把「模型可见的表面事件」转成消息（user / assistant / tool），
 * 与内核 docs/e2e.md 的口径一致；system/developer 事件丢弃。
 * 每个会话按事件序号（seq）增量投递，避免重复入库。
 */

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
    trace = () => {},
  } = {}) {
    this.trace = trace;
    this.ctx = ctx
    this.kernel = kernel
    this.agentId = agentId
    this.debounceMs = Math.max(200, Number(debounceMs) || 2500)
    this.timeoutMs = timeoutMs
    this.logger = logger
    this.buffers = new Map()   // sessionId -> { messages: [], lastSeq: 0, timer }
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
      buffer = { messages: [], lastSeq: 0, timer: null }
      this.buffers.set(sessionId, buffer)
    }
    if (seq <= buffer.lastSeq) return
    this.trace?.(`event ${event.type} seq=${seq} role=${role} chars=${content.length}`)

    buffer.messages.push({
      msg_id: String(message?.id ?? `${sessionId}:${seq}`),
      seq,
      role,
      content,
      ts: new Date().toISOString(),
    })
    buffer.lastSeq = seq

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
      const result = await this.kernel.request('ingest.request', {
        messages,
        cursor: { last_seq: messages[0].seq - 1 },
      }, { sessionId, agentId: this.agentId, timeoutMs: this.timeoutMs })
      this.trace?.(`flush ok session=${sessionId} accepted=${result?.accepted}`)
      return result
    } catch (error) {
      // 已经答应过回执的这批消息还没落库：放回队首等下次投递，
      // 否则一次超时就永久丢消息（内核按 msg_id 去重，重发是安全的）。
      buffer.messages.unshift(...messages)
      this.trace?.(`flush retry session=${sessionId} n=${messages.length}: ${error?.message ?? error}`)
      throw error
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
