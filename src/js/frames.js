/**
 * 协议帧：与 Python 内核（src/main.py）之间的 JSON-Lines 信封。
 *
 * 字段与 docs/Transmission.md 一致：protocol_version / plugin_version /
 * type / request_id / job_id / session_id / agent_id / ts / data。
 */
export const PROTOCOL_VERSION = '1.0'
export const PLUGIN_VERSION = '0.1.0'

let counter = 0

/** 生成进程内唯一的 request_id。 */
export function nextRequestId(prefix = 'req') {
  counter += 1
  return `${prefix}_${Date.now().toString(36)}_${counter}`
}

/** 当前时间的 ISO 8601（带本地时区偏移）。 */
export function isoNow(date = new Date()) {
  const pad = (value, width = 2) => String(value).padStart(width, '0')
  const offset = -date.getTimezoneOffset()
  const sign = offset >= 0 ? '+' : '-'
  const abs = Math.abs(offset)
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    + `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

/** 构造一帧（不序列化）。 */
export function buildFrame(type, data, envelope = {}) {
  return {
    protocol_version: PROTOCOL_VERSION,
    plugin_version: PLUGIN_VERSION,
    type,
    request_id: envelope.request_id ?? '',
    job_id: envelope.job_id ?? null,
    session_id: envelope.session_id ?? '',
    agent_id: envelope.agent_id ?? '',
    ts: isoNow(),
    data: data ?? {},
  }
}

/** 序列化成一行（内核按行读取）。 */
export function encodeFrame(type, data, envelope = {}) {
  return `${JSON.stringify(buildFrame(type, data, envelope))}\n`
}

/** 解析内核下行的一行；坏行返回 null，不抛。 */
export function decodeFrame(line) {
  const text = String(line ?? '').trim()
  if (!text) return null
  try {
    const value = JSON.parse(text)
    return value && typeof value === 'object' && typeof value.type === 'string'
      ? value
      : null
  } catch {
    return null
  }
}

/** 内核错误帧 → Error（带 code）。 */
export function frameError(frame) {
  const data = frame?.data ?? {}
  const error = new Error(data.message || '内核错误')
  error.code = data.code
  error.detail = data.detail
  return error
}
