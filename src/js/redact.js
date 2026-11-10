/** 外壳侧脱敏：与内核 sanitize.py 的规则按测试用例保持一致。 */
export const REDACT_MASK = '[REDACTED]'

const VALUE = "(?:'[^']*'|\"[^\"]*\"|\\S{4,})"
const KEY = ('(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|'
  + 'private[_-]?key|credential|authorization|auth|bearer)')

const PATTERNS = [
  [new RegExp(`(\\$env:[A-Za-z_][A-Za-z0-9_]*\\s*=\\s*)${VALUE}`, 'g'), false],
  [new RegExp('(Bearer\\s+)([A-Za-z0-9._\\-]{8,})', 'gi'), false],
  [new RegExp('(Authorization\\s*:\\s*)(?:Basic\\s+\\S+|Bearer\\s+\\S+|\\S{4,})', 'gi'), false],
  // JSON 字段要保留冒号和值的引号；工具调用里的嵌套 JSON 也能遮盖。
  [new RegExp(`("${KEY}"\\s*:\\s*)("(?:\\\\.|[^"\\\\])*"|[^,}\\]\\s]+)`, 'gi'), true],
  [new RegExp(`(\\b(?!authorization\\b)${KEY}\\b\\s*[:=]\\s*)${VALUE}`, 'gi'), false],
  [new RegExp('(--(?:password|passwd|token|secret|api-key)\\s+)(\\S{4,})', 'gi'), false],
]
const BARE = new RegExp(
  '\\b(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|'
  + 'eyJ[A-Za-z0-9_\\-]{10,}\\.[A-Za-z0-9_\\-]{10,}\\.[A-Za-z0-9_\\-]{6,})', 'g')

/** 遮盖常见凭据；仅替换值，不破坏键名和 JSON 结构。 */
export function redact(text) {
  if (!text) return text
  let out = String(text)
  for (const [pattern, quoted] of PATTERNS) {
    out = out.replace(pattern, (_match, prefix) =>
      `${prefix}${quoted ? JSON.stringify(REDACT_MASK) : REDACT_MASK}`)
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
