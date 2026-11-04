/**
 * 斜杠命令：`/memory` —— 立即把当前会话缓冲投递给内核切分。
 *
 * 命令不走模型消息（`dsh-commands` 的语义）：用户在输入框敲 `/memory`，
 * 外壳立即 flush 去抖缓冲，内核照常做切分/摘要/落库，结果以命令结果文本回显。
 */
import { optionalService } from './services.js'

export function registerMemoryCommand(ctx, {
  bridge, timeoutMs = 30000, trace = () => {},
} = {}) {
  const commands = optionalService(ctx, 'commands')
  if (!commands || typeof commands.register !== 'function') {
    trace('commands service unavailable: /memory 未注册')
    return null
  }

  const dispose = commands.register({
    name: 'memory',
    description: '立即切分当前会话并写入长期记忆（跳过投递去抖）',
    async handler({ agent } = {}) {
      const sessionId = agent?.session?.id ?? ''
      if (!sessionId) return { kind: 'error', text: '没有可用的会话' }
      try {
        const result = await bridge.flush(sessionId)
        if (!result) return { kind: 'success', text: '没有待写入的新消息' }
        const accepted = result?.accepted === true
        const count = result?.new_messages ?? 0
        trace(`command /memory session=${sessionId} accepted=${accepted} n=${count}`)
        return accepted
          ? { kind: 'success', text: `已投递 ${count} 条消息，正在切分…` }
          : { kind: 'success', text: '没有待写入的新消息' }
      } catch (error) {
        trace(`command /memory failed: ${error?.message ?? error}`)
        return { kind: 'error', text: `投递失败: ${error?.message ?? error}` }
      }
    },
  })
  trace('command registered: /memory')
  return dispose
}
