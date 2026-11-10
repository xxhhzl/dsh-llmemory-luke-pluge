/**
 * 外壳侧脱敏：与内核 `src/sanitize.py` 保持同一套规则。
 *
 * 队列文件是磁盘上的明文副本，内核的脱敏发生在入库时——如果不在落盘前
 * 处理一遍，`ingest-pending.json` 里会留着完整凭据。这里按同一规则遮盖，
 * 投递时用过脱敏内容，内核再按同一规则脱敏是幂等的，最终落库结果不变。
 */

export const REDACT_MASK = '[REDACTED]'

// 值：单/双引号串，或 4 个以上非空白字符
const VALUE = "(?:'[^']*'|\"[^\"]*\"|\\S{4,})"
const KEY = ('(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|'
  + 'private[_-]?key|credential|authorization|auth|bearer)')

const PATTERNS = [
  new RegExp(`(\\$env:[A-Za-z_][A-Za-z0-9_]*\\s*=\\s*)${VALUE}`, 'g'),
  new RegExp(`(Bearer\\s+)([A-Za-z0-9._\\-]{8,})`, 'gi'),
  new RegExp(`\\b(${KEY})\\b\\s*[:=]\\s*${VALUE}`, 'gi'),
  new RegExp(`(--(?:password|passwd|token|secret|api-key)\\s+)(\\S{4,})`, 'gi'),
]

const BARE = new RegExp(
  '\\b(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|'
  + 'eyJ[A-Za-z0-9_\\-]{10,}\\.[A-Za-z0-9_\\-]{10,}\\.[A-Za-z0-9_\\-]{6,})', 'g')

/**
 * 命中即替换成 [REDACTED]；无命中原样返回。
 * @param {string} text
 * @returns {string}
 */
export function redact(text) {
  if (!text) return text
  let out = String(text)
  for (const pattern of PATTERNS) {
    out = out.replace(pattern, (_match, prefix) => `${prefix}${REDACT_MASK}`)
  }
  return out.replace(BARE, REDACT_MASK)
}

/** 消息数组脱敏：只处理 content，其余字段原样保留。 */
export function redactMessages(messages) {
  if (!Array.isArray(messages)) return messages
  return messages.map(item => (
    item && typeof item.content === 'string'
      ? { ...item, content: redact(item.content) }
      : item
  ))
}
