/**
 * 模型配置：三槽（JEV / 提取 / Embedding）的默认值、合并、直连调用。
 *
 * 宿主模型走 ctx.llm；自定义端点走本文件的 fetch（JS 侧），
 * 内核不持有 baseURL / key。
 */

export const SETTING_KEYS = ['model.jev', 'model.extract', 'embedding']

export function defaultSettings(config = {}) {
  const host = {
    mode: 'host',
    provider: config.provider || '',
    model: config.model || '',
    baseUrl: '',
    apiKey: '',
    timeoutMs: Number(config.llmTimeoutMs ?? 120000),
  }
  return {
    'model.jev': { ...host },
    'model.extract': { ...host },
    embedding: {
      flavor: 'openai',
      baseUrl: 'http://127.0.0.1:11434/v1',
      model: 'bge-m3',
      apiKey: '',
      dim: 1024,
      batchSize: 16,
      timeoutMs: 60000,
    },
  }
}

/** 存储值覆盖默认值（只做浅合并，字段缺失沿用默认）。 */
export function mergeSettings(stored, defaults) {
  const merged = {}
  for (const key of SETTING_KEYS) {
    merged[key] = { ...(defaults[key] || {}), ...(stored?.[key] || {}) }
  }
  return merged
}

/** 从内核读设置并与默认值合并；失败时只用默认值。 */
export async function loadSettings(kernel, config) {
  const defaults = defaultSettings(config)
  try {
    const result = await kernel.request('settings.raw', {}, { timeoutMs: 15000 })
    return mergeSettings(result?.items || {}, defaults)
  } catch {
    return defaults
  }
}

/** 按 purpose 选槽：jev 用 JEV 槽，其余文本调用用提取槽。 */
export function profileFor(settings, purpose) {
  const key = purpose === 'jev' ? 'model.jev' : 'model.extract'
  return settings?.[key] || {}
}

function withTimeout(timeoutMs) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs) || 60000))
  return { controller, done: () => clearTimeout(timer) }
}

function baseUrl(value) {
  return String(value || '').trim().replace(/\/+$/, '')
}

/** OpenAI 兼容 chat/completions（自定义端点）。 */
export async function chatCompletion(profile, { system = '', user = '' } = {}) {
  const url = `${baseUrl(profile.baseUrl)}/chat/completions`
  if (!profile.baseUrl || !profile.model) throw new Error('自定义端点缺少 baseUrl / model')
  const { controller, done } = withTimeout(profile.timeoutMs)
  try {
    const response = await fetch(url, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'content-type': 'application/json',
        ...(profile.apiKey ? { authorization: `Bearer ${profile.apiKey}` } : {}),
      },
      body: JSON.stringify({
        model: profile.model,
        temperature: 0,
        stream: false,
        messages: [
          ...(system ? [{ role: 'system', content: system }] : []),
          { role: 'user', content: user },
        ],
      }),
    })
    const text = await response.text()
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`)
    const data = JSON.parse(text)
    const content = data?.choices?.[0]?.message?.content
    if (typeof content !== 'string') throw new Error('响应缺少 choices[0].message.content')
    return { content, model: data?.model || profile.model, usage: data?.usage || {} }
  } finally {
    done()
  }
}

/** 批量 embedding：openai → /embeddings；ollama → /api/embed。 */
export async function embedTexts(profile, texts) {
  const input = (texts || []).map(text => String(text ?? ''))
  if (input.length === 0) return []
  if (!profile.baseUrl || !profile.model) throw new Error('embedding 缺少 baseUrl / model')
  const flavor = String(profile.flavor || 'openai').toLowerCase()
  const root = baseUrl(profile.baseUrl)
  const url = flavor === 'ollama'
    ? `${root.replace(/\/v1$/, '')}/api/embed`
    : `${root}/embeddings`
  const { controller, done } = withTimeout(profile.timeoutMs)
  try {
    const response = await fetch(url, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'content-type': 'application/json',
        ...(profile.apiKey ? { authorization: `Bearer ${profile.apiKey}` } : {}),
      },
      body: JSON.stringify({ model: profile.model, input }),
    })
    const text = await response.text()
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`)
    const data = JSON.parse(text)
    const vectors = flavor === 'ollama'
      ? data?.embeddings
      : (data?.data || []).map(item => item?.embedding)
    if (!Array.isArray(vectors) || vectors.length !== input.length) {
      throw new Error('embedding 响应条数不匹配')
    }
    if (profile.dim && vectors[0] && vectors[0].length !== Number(profile.dim)) {
      throw new Error(`embedding 维度 ${vectors[0].length} ≠ 配置 ${profile.dim}`)
    }
    return vectors
  } finally {
    done()
  }
}
