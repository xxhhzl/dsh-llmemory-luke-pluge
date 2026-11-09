/**
 * 核心记忆注入：两块内容、两条通道、各自标签。
 *
 *   设置的自我 + 核心规则 → 系统提示词段（ctx.systemPrompt.section）
 *   自己写的自我        → 动态上下文（ctx.systemPrompt.context，user 快照）
 *   被动回想            → 动态上下文（<recall>，按当前会话/当前输入编译）
 *
 * 文本由内核 `core.get` 编译（带 `<long_term_memory>` / `<self_notes>` / `<recall>` 标签），
 * 这里只做缓存与注册；版本不变就不更新，避免无谓击穿系统提示词前缀缓存。
 *
 * 会话隔离（0.5.1）：
 * - 每次编译都带上当前 `session_id`，内核不再用「最近有消息的会话」兜底；
 * - 召回缓存按会话存；`agent/pre-step` 在提示词组装前把当前会话与当前用户输入
 *   交给内核，因此注入的是**本轮**的回忆，而不是上一轮 ingest 之后的结果。
 */

export const CORE_SECTION = 'dsh-longmemory:core'
export const SELF_NOTES_CONTEXT = 'dsh-longmemory:self-notes'
export const RECALL_CONTEXT = 'dsh-longmemory:recall'

/** pre-step 里最多等召回这么久；超时就先用上一轮快照，不拖慢本轮请求。 */
export const RECALL_WAIT_MS = 1500

/** 从 user 消息数组里取最后一条非空文本（内容块只取 text）。 */
export function lastUserText(messages) {
  if (!Array.isArray(messages)) return ''
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.role && message.role !== 'user') continue
    const kind = message?.source?.kind
    if (kind === 'plugin' || kind === 'tool' || kind === 'model') continue
    const content = message?.content
    let text = ''
    if (typeof content === 'string') text = content
    else if (Array.isArray(content)) {
      text = content
        .filter(block => block?.type === 'text' && typeof block.text === 'string')
        .map(block => block.text)
        .join('\n')
    }
    if (text.trim()) return text.trim()
  }
  return ''
}

export class CorePrompt {
  /**
   * @param {object} ctx     宿主插件上下文（需 ctx.systemPrompt）
   * @param {object} kernel  内核帧客户端
   */
  constructor(ctx, kernel, {
    order = -2000, contextOrder = 850, timeoutMs = 15000,
    recallWaitMs = RECALL_WAIT_MS, trace = () => {},
  } = {}) {
    this.ctx = ctx
    this.kernel = kernel
    this.order = order
    this.contextOrder = contextOrder
    this.timeoutMs = timeoutMs
    this.recallWaitMs = recallWaitMs
    this.trace = trace
    this.text = ''
    this.version = ''
    this.notesText = ''
    this.notesVersion = ''
    this.recallBySession = new Map()   // session_id -> { text, items, version }
    this.activeSession = ''
    this.stepDisposer = null
    this.disposers = []
    this.attached = false
  }

  /** 当前会话的召回文本；没有会话时为空（绝不回退到别的会话）。 */
  currentRecall() {
    if (!this.activeSession) return ''
    return this.recallBySession.get(this.activeSession)?.text || ''
  }

  /** 注册 section 与 context；宿主没有 systemPrompt 时安全跳过。 */
  attach() {
    if (this.attached) return true
    const prompt = this.ctx?.systemPrompt
    if (!prompt || typeof prompt.section !== 'function') {
      this.trace('systemPrompt unavailable: 核心记忆不注入')
      return false
    }
    const effect = typeof this.ctx.effect === 'function'
      ? (fn, label) => this.ctx.effect(fn, label)
      : fn => fn()

    this.disposers.push(effect(() => prompt.section({
      name: CORE_SECTION,
      order: this.order,
      text: () => this.text,
      interpolate: false,          // 记忆正文可能含 {{…}}，原样保留
    }), 'longmemory.core.section'))

    if (typeof prompt.context === 'function') {
      this.disposers.push(effect(() => prompt.context({
        name: SELF_NOTES_CONTEXT,
        order: this.contextOrder,
        text: () => this.notesText,
        interpolate: false,
      }), 'longmemory.selfNotes.context'))
      // 被动回想：每轮按当前对话捞几条相关记忆，排在自述之后（更靠近对话）
      this.disposers.push(effect(() => prompt.context({
        name: RECALL_CONTEXT,
        order: this.contextOrder + 10,
        text: () => this.currentRecall(),
        interpolate: false,
      }), 'longmemory.recall.context'))
    } else {
      this.trace('systemPrompt.context unavailable: 自述不注入')
    }
    this.attached = true
    return true
  }

  /**
   * 挂 `agent/pre-step`：在提示词组装之前，用**本步的会话与用户输入**刷新召回。
   * waterfall 契约：无论成败都要把 `next()` 的结果原样传上去。
   */
  bindStep() {
    if (this.stepDisposer) return this.stepDisposer
    if (typeof this.ctx?.on !== 'function') return null
    const handler = async (payload, next) => {
      const sessionId = payload?.agent?.session?.id
        || payload?.agent?.session?.header?.id || ''
      if (sessionId) {
        const queryHint = lastUserText(payload?.messages)
        this.activeSession = sessionId
        try {
          await Promise.race([
            this.refresh({ sessionId, queryHint }),
            new Promise(resolve => {
              const timer = setTimeout(resolve, this.recallWaitMs)
              timer.unref?.()
            }),
          ])
        } catch (error) {
          this.trace(`recall pre-step failed: ${error?.message ?? error}`)
        }
      }
      return next()
    }
    this.stepDisposer = this.ctx.on('agent/pre-step', handler)
    return this.stepDisposer
  }

  /**
   * 从内核取编译结果；核心块/自述按版本号更新，召回按会话覆盖（文本不变就不动）。
   *
   * @param {object} options
   * @param {string} [options.sessionId] 会话 id；缺省用 pre-step 记下的当前会话
   * @param {string} [options.queryHint] 当前用户输入；缺省让内核用最近几条消息
   */
  async refresh({ sessionId = '', queryHint = '' } = {}) {
    if (!this.kernel) return null
    const target = sessionId || this.activeSession || ''
    const payload = await this.kernel.request(
      'core.get',
      { query_hint: queryHint || null },
      { timeoutMs: this.timeoutMs, sessionId: target },
    )
    if (!payload || payload.enabled === false) {
      this.version = ''
      this.text = ''
      this.notesVersion = ''
      this.notesText = ''
      this.recallBySession.clear()
      return payload
    }
    if ((payload.version || '') !== this.version) {
      this.version = payload.version || ''
      this.text = payload.text || ''
      this.trace(`core updated version=${this.version} chars=${this.text.length}`)
    }
    if ((payload.notes_version || '') !== this.notesVersion) {
      this.notesVersion = payload.notes_version || ''
      this.notesText = payload.notes_text || ''
      this.trace(`self notes updated version=${this.notesVersion} chars=${this.notesText.length}`)
    }
    // 召回按会话存：A 会话的刷新不能覆盖 B 会话的文本
    const key = target
    if (key) {
      const text = payload.recall_text || ''
      const existing = this.recallBySession.get(key)
      if (!existing || existing.text !== text) {
        this.recallBySession.set(key, {
          text,
          items: payload.recall_items || [],
          version: payload.recall_version || '',
        })
        this.trace(`recall updated session=${key} items=${(payload.recall_items || []).length}`)
      }
      if (key === this.activeSession || !this.activeSession) this.activeSession = key
    }
    return payload
  }

  dispose() {
    try {
      this.stepDisposer?.()
    } catch {
      /* 卸载路径尽力而为 */
    }
    this.stepDisposer = null
    for (const disposer of this.disposers) {
      try {
        disposer?.()
      } catch {
        /* 卸载路径尽力而为 */
      }
    }
    this.disposers = []
    this.attached = false
  }
}
