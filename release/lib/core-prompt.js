/**
 * 核心记忆注入：两块内容、两条通道、各自标签。
 *
 *   设置的自我 + 核心规则 → 系统提示词段（ctx.systemPrompt.section）
 *   自己写的自我        → 动态上下文（ctx.systemPrompt.context，user 快照）
 *
 * 文本由内核 `core.get` 编译（带 `<long_term_memory>` / `<self_notes>` 标签），
 * 这里只做缓存与注册；版本不变就不更新，避免无谓击穿系统提示词前缀缓存。
 */

export const CORE_SECTION = 'dsh-longmemory:core'
export const SELF_NOTES_CONTEXT = 'dsh-longmemory:self-notes'

export class CorePrompt {
  /**
   * @param {object} ctx     宿主插件上下文（需 ctx.systemPrompt）
   * @param {object} kernel  内核帧客户端
   */
  constructor(ctx, kernel, {
    order = 700, contextOrder = 850, timeoutMs = 15000, trace = () => {},
  } = {}) {
    this.ctx = ctx
    this.kernel = kernel
    this.order = order
    this.contextOrder = contextOrder
    this.timeoutMs = timeoutMs
    this.trace = trace
    this.text = ''
    this.version = ''
    this.notesText = ''
    this.notesVersion = ''
    this.disposers = []
    this.attached = false
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
    } else {
      this.trace('systemPrompt.context unavailable: 自述不注入')
    }
    this.attached = true
    return true
  }

  /** 从内核取编译结果；只有版本变化才替换缓存文本。 */
  async refresh() {
    if (!this.kernel) return null
    const payload = await this.kernel.request('core.get', {}, { timeoutMs: this.timeoutMs })
    if (!payload || payload.enabled === false) {
      this.version = ''
      this.text = ''
      this.notesVersion = ''
      this.notesText = ''
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
    return payload
  }

  dispose() {
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
