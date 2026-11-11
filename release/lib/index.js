/**
 * DSH 长期记忆插件的入口（Host plugin）。
 *
 * 结构：
 *   本文件         插件清单：name / inject / Config + cordis effect
 *   shell.js       装配：起内核、接三条桥、挂命令与本地路由
 *   kernel.js      子进程 + 帧收发
 *   lifecycle.js   退出看护（退避重启，纯逻辑）
 *   llm-bridge.js  内核 llm.request → ctx.llm
 *   tools-bridge.js 内核 tools.schema → ctx.tools，调用回灌
 *   ingest-bridge.js session/event → ingest.request
 *
 * 约定：内核只持有库与算法，provider/model/key 全部留在外壳；
 * 界面可见文案（meta.title / meta.description）在 locale/*.json。
 */
import z from '@deepseek-ai/schemastery'

import { trace } from './trace.js'
import { createShell } from './shell.js'

export { createShell } from './shell.js'
export { restartDelay, DEFAULT_RESTART_DELAYS } from './lifecycle.js'

export const name = 'dsh-llmemory-luke'

/** 依赖的工具与模型服务；缺一不可。 */
export const inject = ['tools', 'llm', 'sessions', 'systemPrompt']

trace(`module loaded (pid=${process.pid})`)

export const Config = z.object({
  pythonPath: z.string().default('python').description('Python 可执行文件'),
  kernelDir: z.string().description('内存核目录（含 main.py）；缺省用插件自带 kernel'),
  dbPath: z.string().description('记忆库路径；缺省为包内 .data/memory.db'),
  provider: z.string().description('模型 provider（如 deepseek-official）'),
  model: z.string().description('模型 id（如 deepseek-v4-flash）'),
  maxTokens: z.number().step(1).min(1).description('单次调用的输出上限'),
  agentId: z.string().default('dsh-llmemory-luke').description('写入信封的 agent 标识'),
  ingestDebounceMs: z.number().step(1).min(200).default(2500)
    .description('会话事件去抖窗口（毫秒）'),
  requestTimeoutMs: z.number().step(1).min(1000).default(30000)
    .description('外壳等内核应答的超时（毫秒）'),
  llmTimeoutMs: z.number().step(1).min(1000).default(120000)
    .description('单次模型调用超时（毫秒）'),
  coreSectionOrder: z.number().step(1).default(-2000)
    .description('核心记忆在系统提示词中的段顺序（persona 之后、文件引用之前）'),
  coreContextOrder: z.number().step(1).default(850)
    .description('自述自我在动态上下文中的顺序'),
})

/** Cordis 插件入口。 */
export function apply(ctx, config = {}) {
  const shell = createShell(ctx, config)
  ctx.effect(() => {
    shell.start().catch(error => {
      trace(`start failed: ${error?.message ?? error}`)
      console.warn(`[longmemory] 外壳启动失败: ${error.message}`)
    })
    // 返回 disposer：先排空最后一批消息，再拆工具与路由
    return () => shell.dispose()
  })
  return shell
}
