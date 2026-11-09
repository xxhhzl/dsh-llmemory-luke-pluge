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
 * 会话隔离（0.5.2）：
 * - 宿主 `systemPrompt` 的 `text` 回调会收到**本次组装**的 context
 *   （`{ agent, scope, signal }`，`agent.session.id` 即会话 id），召回按它取文本，
 *   不再依赖一个全局「当前会话」指针——两个步骤交错时也不会串；
 * - 每轮召回由 `agent/pre-step` 触发（提示词组装之前），带当前用户输入；
 * - `refresh()` 默认只重编核心文本（`recall:false`），后台刷新不推进冷却轮次；
 * - 同一会话的刷新带递增序号，晚到的旧响应不会覆盖新结果。
 */

export const CORE_SECTION = 'dsh-longmemory:core'
export const SELF_NOTES_CONTEXT = 'dsh-longmemory:self-notes'
export const RECALL_CONTEXT = 'dsh-longmemory:recall'

/** pre-step 里最多等召回这么久；超时就先用上一轮快照，不拖慢本轮请求。 */
export const RECALL_WAIT_MS = 1500

/** 从 prompt 组装 context 里取会话 id（宿主 shape：context.agent.session.id）。 */
export function assembleSessionId(context) {
  return context?.agent?.session?.id
    || context?.agent?.id
    || context?.session?.id
    || context?.sessionId
    || ''
}

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
    embedQueryFn = null,
  } = {}) {
    this.ctx = ctx
    this.kernel = kernel
    this.order = order
    this.contextOrder = contextOrder
    this.timeoutMs = timeoutMs
    this.recallWaitMs = recallWaitMs
    this.trace = trace
    // 可选：给本轮用户输入算向量，让被动召回也能走向量通道
    // （只靠字面命中时，换个说法提旧事就召不回）。
    this.embedQueryFn = embedQueryFn
    this.text = ''
    this.version = ''
    this.notesText = ''
    this.notesVersion = ''
    this.recallBySession = new Map()   // session_id -> { text, items, version }
    this.recallSeq = new Map()         // session_id -> 最近一次刷新序号
    this.activeSession = ''            // 只用于诊断/无 context 的兜底读取
    this.stepDisposer = null
    this.disposers = []
    this.attached = false
  }

  /** 取某个会话的召回文本；没有会话就返回空（绝不借别的会话）。 */
  recallFor(context) {
    const sessionId = assembleSessionId(context) || this.activeSession
    if (!sessionId) return ''
    return this.recallBySession.get(sessionId)?.text || ''
  }

  /** 最近一轮步骤的召回文本（对应会话）；没有就为空。 */
  currentRecall() {
    return this.activeSession
      ? (this.recallBySession.get(this.activeSession)?.text || '')
      : ''
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
      // 被动回想：text 收到本次组装的 context，按它的会话取文本（请求级隔离）
      this.disposers.push(effect(() => prompt.context({
        name: RECALL_CONTEXT,
        order: this.contextOrder + 10,
        text: context => this.recallFor(context),
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
        || payload?.agent?.id || ''
      if (!sessionId) {
        this.activeSession = ''      // 没有会话：清掉兜底，绝不复用上一次
        return next()
      }
      this.activeSession = sessionId
      const queryHint = lastUserText(payload?.messages)
      try {
        // 算向量与请求一起受 recallWaitMs 限时：慢就给上一轮快照，不拖慢本轮。
        const work = (async () => {
          let queryVector = null
          if (this.embedQueryFn && queryHint) {
            try {
              queryVector = await this.embedQueryFn(queryHint)
            } catch (error) {
              this.trace(`recall embed failed: ${error?.message ?? error}`)
            }
          }
          await this.refresh({
            sessionId, queryHint, recall: true, advanceTurn: true,
            queryVector,
          })
        })()
        await Promise.race([
          work,
          new Promise(resolve => {
            const timer = setTimeout(resolve, this.recallWaitMs)
            timer.unref?.()
          }),
        ])
      } catch (error) {
        this.trace(`recall pre-step failed: ${error?.message ?? error}`)
      }
      return next()
    }
    this.stepDisposer = this.ctx.on('agent/pre-step', handler)
    return this.stepDisposer
  }

  /**
   * 从内核取编译结果。
   *
   * @param {object} options
   * @param {string} [options.sessionId] 会话 id；缺省用最近一次步骤的会话
   * @param {string} [options.queryHint] 当前用户输入；缺省让内核用最近几条消息
   * @param {boolean} [options.recall]  是否重编召回（默认否：后台刷新只编核心文本）
   * @param {boolean} [options.advanceTurn] 是否推进召回冷却（只有步骤为 true）
   * @param {number[]} [options.queryVector] 当前输入的向量；有它召回才走向量通道
   */
  async refresh({
    sessionId = '', queryHint = '', recall = false, advanceTurn = false,
    queryVector = null,
  } = {}) {
    if (!this.kernel) return null
    const target = sessionId || this.activeSession || ''
    const seq = (this.recallSeq.get(target) || 0) + 1
    this.recallSeq.set(target, seq)

    const payload = await this.kernel.request(
      'core.get',
      {
        query_hint: queryHint || null,
        recall: Boolean(recall),
        advance_turn: Boolean(advanceTurn),
        query_vector: Array.isArray(queryVector) && queryVector.length
          ? queryVector : null,
      },
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
    // 召回：按会话存；晚到的旧响应（序号已过期）不落缓存
    if (recall && target && !payload.recall_skipped
        && this.recallSeq.get(target) === seq) {
      const text = payload.recall_text || ''
      const existing = this.recallBySession.get(target)
      if (!existing || existing.text !== text) {
        this.recallBySession.set(target, {
          text,
          items: payload.recall_items || [],
          version: payload.recall_version || '',
        })
        // 会话 Map 有上限：长期运行时不无限增长，淘汰最久未更新的会话
        while (this.recallBySession.size > 50) {
          const oldest = this.recallBySession.keys().next().value
          if (oldest === target) break
          this.recallBySession.delete(oldest)
        }
        this.trace(`recall updated session=${target} items=${(payload.recall_items || []).length}`)
      }
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
