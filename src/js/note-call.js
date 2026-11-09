/**
 * 工具调用的纯函数层（不 import 宿主包，离线可测）。
 *
 * 这里只负责「调用前要做的事」：memory_note 先算 content 的向量再随调用传给内核。
 * tools-bridge 负责把结果注册成宿主工具。
 */

/**
 * memory_note：给 content 算向量，随 `tools.calls` 传给内核。
 *
 * 内核的语义查重与「新条目立刻进向量索引」都依赖这个向量；没有它的通用分支
 * 只能退化成字面比较，新写条目也搜不到（0.5.1 修）。
 *
 * @param {object} deps
 * @param {(text: string) => Promise<number[]|null>} deps.embedQueryFn
 * @param {(type: string, payload: object) => Promise<any>} deps.request
 * @returns {Promise<object|null>} 内核返回的单条 tools.result 条目
 */
export async function executeMemoryNote(
  { embedQueryFn, request },
  { name = 'memory_note', args, timeoutMs = 30_000 } = {},
) {
  const callId = `${name}-${Date.now().toString(36)}`
  const call = { call_id: callId, name, arguments: args ?? {} }
  if (args?.content) {
    const vector = await embedQueryFn(args.content)
    if (Array.isArray(vector) && vector.length) call.query_vector = vector
  }
  const result = await request('tools.calls', { calls: [call] }, { timeoutMs })
  return Array.isArray(result) ? result[0] : null
}
