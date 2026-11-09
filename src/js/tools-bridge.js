/**
 * 工具桥：内核 `tools.schema` → Harness 工具注册 → 调用回内核 `tools.calls`。
 *
 * 内核的 schema 是 JSON Schema；Harness 用自家的参数 DSL，这里做一次窄转换
 * （只覆盖内核实际用到的 string / integer / number / boolean / enum / anyOf-null）。
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import { embedQuery, hydeVector } from './hyde.js'
import { executeMemoryNote } from './note-call.js'

const PRIMITIVES = new Set(['string', 'number', 'integer', 'boolean'])

/** 取 anyOf 里第一个非 null 分支。 */
function pickBranch(spec) {
  if (Array.isArray(spec?.anyOf)) {
    return spec.anyOf.find(branch => branch?.type !== 'null') ?? { type: 'string' }
  }
  return spec ?? {}
}

/**
 * 单个属性：JSON Schema → 工具 DSL。
 * @returns {object|null} null 表示无法转换，调用方应跳过该属性
 */
export function convertProperty(raw) {
  const spec = pickBranch(raw)
  const description = raw?.description ?? spec.description
  if (spec.const !== undefined) {
    return { type: typeof spec.const === 'number' ? 'number' : 'string',
             const: spec.const, ...(description ? { description } : {}) }
  }
  if (Array.isArray(spec.enum)) {
    const kind = spec.type && PRIMITIVES.has(spec.type) ? spec.type : 'string'
    return { type: kind, enum: [...spec.enum], ...(description ? { description } : {}) }
  }
  if (spec.type === 'array') {
    const item = convertProperty(spec.items ?? { type: 'string' })
    return item ? { type: 'array', items: item, ...(description ? { description } : {}) } : null
  }
  if (!PRIMITIVES.has(spec.type)) return null
  const out = { type: spec.type }
  if (description) out.description = description
  return out
}

/** 内核 schema 的 parameters → Harness 参数 DSL。 */
export function convertParameters(parameters = {}) {
  const required = new Set(parameters.required ?? [])
  const result = {}
  for (const [name, raw] of Object.entries(parameters.properties ?? {})) {
    const converted = convertProperty(raw)
    if (!converted) continue
    result[name] = required.has(name) ? { ...converted, required: true } : converted
  }
  return result
}

/** 工具的返回值：内核给的是结构化 JSON，这里原样返回并渲染成文本。 */
function outputSpec() {
  return {
    schema: { type: 'json' },
    render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
  }
}

/**
 * memory_search 的执行体（含 HyDE 重试），抽出来便于离线测试：
 * 依赖（embedQuery / hydeVector / kernel.request）全部走参数，不碰宿主包。
 */
export async function executeMemorySearch(
  { embedQueryFn = embedQuery, hydeVectorFn = hydeVector, request, logger },
  { name = 'memory_search', args, sessionId = '', agentId = '', timeoutMs = 30_000 },
) {
  const callId = `${name}-${Date.now().toString(36)}`
  const call = { call_id: callId, name, arguments: args ?? {} }
  const vector = await embedQueryFn(args?.query)
  if (vector) call.query_vector = vector
  const result = await request('tools.calls', { calls: [call] })
  let entry = Array.isArray(result) ? result[0] : null
  // HyDE：词法+向量都零命中时，用假想回答的向量再试一次。
  // 触发条件收窄到 count===0 且向量通道可用，正常命中不多花一次模型调用。
  const empty = entry?.ok && entry.data?.count === 0
  if (empty && call.query_vector) {
    const hyde = await hydeVectorFn(args.query)
    if (hyde) {
      const retry = await request('tools.calls', {
        calls: [{ ...call, query_vector: hyde, call_id: `${callId}-h` }],
      })
      const retried = Array.isArray(retry) ? retry[0] : null
      if (retried?.ok && retried.data?.count > 0) {
        entry = retried
      }
    }
  }
  return entry
}

/**
 * 把内核声明的工具注册进 Harness。
 * @returns {Array<Function>} 每项的 disposer
 */
export function registerKernelTools(ctx, kernel, schemas, {
  agentId = '', timeoutMs = 30_000, logger = console, getSettings = null,
} = {}) {
  const disposers = []
  for (const schema of schemas ?? []) {
    const name = schema?.name
    if (!name || typeof schema.parameters !== 'object') continue
    const definition = defineTool({
      name,
      description: schema.description || name,
      parameters: convertParameters(schema.parameters),
      output: outputSpec(),
      async execute(args, exec) {
        const sessionId = exec?.agent?.session?.id ?? ''
        if (name === 'memory_search' && args?.query) {
          const settings = getSettings?.()
          const entry = await executeMemorySearch({
            embedQueryFn: query => embedQuery(settings, query),
            hydeVectorFn: query => hydeVector(settings, query),
            request: (type, payload) => kernel.request(
              type, payload, { sessionId, agentId, timeoutMs }),
            logger,
          }, { name, args, sessionId, agentId, timeoutMs })
          if (!entry) throw new Error(`内核没有返回 ${name} 的结果`)
          if (!entry.ok) {
            const error = new Error(entry.error?.message || `${name} 执行失败`)
            error.code = entry.error?.code
            throw error
          }
          return entry.data
        }
        if (name === 'memory_note' && args?.content) {
          const settings = getSettings?.()
          const entry = await executeMemoryNote({
            embedQueryFn: text => embedQuery(settings, text),
            request: (type, payload, opts) => kernel.request(
              type, payload,
              { sessionId, agentId, timeoutMs: opts?.timeoutMs ?? timeoutMs }),
          }, { name, args, timeoutMs })
          if (!entry) throw new Error(`内核没有返回 ${name} 的结果`)
          if (!entry.ok) {
            const error = new Error(entry.error?.message || `${name} 执行失败`)
            error.code = entry.error?.code
            throw error
          }
          return entry.data
        }
        const callId = `${name}-${Date.now().toString(36)}`
        const call = { call_id: callId, name, arguments: args ?? {} }
        const result = await kernel.request('tools.calls', {
          calls: [call],
        }, { sessionId, agentId, timeoutMs })
        const entry = Array.isArray(result) ? result[0] : null
        if (!entry) throw new Error(`内核没有返回 ${name} 的结果`)
        if (!entry.ok) {
          const error = new Error(entry.error?.message || `${name} 执行失败`)
          error.code = entry.error?.code
          throw error
        }
        return entry.data
      },
    })
    disposers.push(ctx.tools.register(definition))
    logger.debug?.(`[longmemory] 注册工具 ${name}`)
  }
  return disposers
}
