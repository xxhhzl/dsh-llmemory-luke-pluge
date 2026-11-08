/**
 * HyDE（Hypothetical Document Embeddings）查询扩展。
 *
 * Gao et al., ACL 2023《Precise Zero-Shot Dense Retrieval without Relevance
 * Labels》：零样本检索里，用「模型假想的回答」的向量去搜，比用问题本身的
 * 向量更接近真实文档。零命中的查询（「我之前让你做什么」）往往没有可检索
 * 的实词；假想回答里有实词，向量距离因此更近。
 *
 * 独立成模块（而不是塞在 tools-bridge 里）有两个原因：
 * - tools-bridge 顶层 import 宿主包 @deepseek-ai/dsh-tools，离线测试导不进；
 * - 这段逻辑是纯函数（settings + query → vector|null），值得单独测。
 */
import { chatCompletion, embedTexts, profileFor } from './model-config.js'

const HYDE_SYSTEM_PROMPT = [
  '你是长期记忆系统的查询扩展器。',
  '用户的问题没有检索到任何记忆，很可能因为问题里没有实质关键词。',
  '请写一段 2-3 句的「假想回答」：假设记忆库里存着相关内容，它最可能是什么。',
  '要求：写具体名词（工具名、项目名、命令、文件），不要写「之前做的事情」这类空话；',
  '只输出假想回答本身，不要解释、不要引号。',
].join('\n')

export { HYDE_SYSTEM_PROMPT }

/** 查询文本 → 向量；embedding 未配置或失败时返回 null（调用方降级）。 */
export async function embedQuery(settings, text) {
  const embedding = settings?.embedding
  if (!embedding?.baseUrl || !embedding?.model) return null
  try {
    const [vector] = await embedTexts(embedding, [text])
    return Array.isArray(vector) ? vector : null
  } catch {
    return null
  }
}

/** HyDE 主流程：假想回答 → 向量；任何一步失败返回 null。 */
export async function hydeVector(settings, query) {
  const profile = profileFor(settings, 'summary')     // 复用提取槽
  if (profile.mode !== 'custom' || !profile.baseUrl || !profile.model) return null
  try {
    const { content } = await chatCompletion(profile, {
      system: HYDE_SYSTEM_PROMPT,
      user: `用户查询：${query}`,
    })
    const text = String(content || '').trim()
    if (!text) return null
    return await embedQuery(settings, text)
  } catch {
    return null
  }
}
