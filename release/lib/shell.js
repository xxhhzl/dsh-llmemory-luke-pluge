/**
 * 外壳装配：起内核、接三条桥、挂命令与本地路由。
 *
 * 从 index.js 拆出来是为了能在离线测试里直接 import（index.js 依赖
 * @deepseek-ai/schemastery 这个宿主包，测试环境没有）。
 */
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { mkdirSync } from 'node:fs'

import { Kernel } from './kernel.js'
import { IngestBridge } from './ingest-bridge.js'
import { CorePrompt } from './core-prompt.js'
import { defaultSettings, embedTexts, loadSettings } from './model-config.js'
import { registerMemoryCommand } from './commands.js'
import { registerLongmemoryRoutes } from './stats-route.js'
import { trace as defaultTrace } from './trace.js'
import { DEFAULT_RESTART_DELAYS, createRestartPolicy } from './lifecycle.js'

/**
 * 两条宿主桥要 import 宿主包（@deepseek-ai/dsh-llm、dsh-tools），
 * 离线测试里没有这些包，所以按需动态加载；测试通过 hostHandlers 注入替身。
 */
let hostModule = null

async function loadHostHandlers() {
  if (!hostModule) {
    const [llm, tools] = await Promise.all([
      import('./llm-bridge.js'),
      import('./tools-bridge.js'),
    ])
    hostModule = {
      handleLlmRequest: llm.handleLlmRequest,
      registerKernelTools: tools.registerKernelTools,
    }
  }
  return hostModule
}

/** 插件自带内核目录（release/kernel，与本文件同级的 ../kernel）。 */
function bundledKernelDir() {
  return fileURLToPath(new URL('../kernel', import.meta.url))
}

/** 等内核就绪：health 成功即认为可服务。 */
async function waitHealthy(kernel, { attempts = 60, intervalMs = 250 } = {}) {
  let lastError
  for (let index = 0; index < attempts; index += 1) {
    if (!kernel.process) throw new Error('内核进程未运行')
    try {
      await kernel.request('health', {}, { timeoutMs: 2000 })
      return true
    } catch (error) {
      lastError = error
      await new Promise(resolve => setTimeout(resolve, intervalMs))
    }
  }
  throw new Error(`内核未就绪: ${lastError?.message ?? 'unknown'}`)
}

/**
 * 装配外壳（供 apply 与测试复用）。
 * @returns {{kernel: Kernel, bridge: IngestBridge, start: Function, dispose: Function}}
 */
export function createShell(ctx, config = {}, {
  kernel: injected, restartDelays = DEFAULT_RESTART_DELAYS, trace = defaultTrace,
  hostHandlers = null,
} = {}) {
  const kernelDir = config.kernelDir || bundledKernelDir()
  const script = path.join(kernelDir, 'main.py')
  // 库路径由外壳解析成绝对路径：内核不依赖 cwd，也不写相对目录
  const packageDir = fileURLToPath(new URL('..', import.meta.url))
  const dbFile = config.dbPath
    ? path.resolve(config.dbPath)
    : path.join(packageDir, '.data', 'memory.db')
  const dataDir = path.dirname(dbFile)
  try {
    mkdirSync(dataDir, { recursive: true })
  } catch (error) {
    trace(`data dir unavailable: ${dataDir} (${error?.message ?? error})`)
  }
  trace(`config db=${dbFile} cwd=${dataDir} provider=${config.provider ?? ''} model=${config.model ?? ''}`)
  const kernel = injected ?? new Kernel({
    python: config.pythonPath || 'python',
    script,
    cwd: dataDir,
    env: { DSH_LONGMEMORY_DB: dbFile },
    logger: console,
  })
  const agentId = config.agentId || 'dsh-longmemory'
  const bridge = new IngestBridge(ctx, kernel, {
    agentId,
    debounceMs: config.ingestDebounceMs ?? 2500,
    timeoutMs: config.requestTimeoutMs ?? 30000,
    logger: console,
    trace,
    // 待确认队列落盘：内核在「已回执、未落库」之间退出时，重启后据此补投
    pendingPath: config.pendingPath || path.join(dataDir, 'ingest-pending.json'),
  })
  // 三槽模型配置（JEV / 提取 / Embedding）；默认继承宿主
  let modelSettings = defaultSettings(config)
  const refreshSettings = async () => {
    modelSettings = await loadSettings(kernel, config)
    return modelSettings
  }
  // 被动召回的查询向量：沿用宿主 embedding；没配就返回 null（退回字面召回）
  const embedForRecall = async text => {
    try {
      const profile = modelSettings?.embedding
      if (!profile?.model || !text) return null
      const [vector] = await embedTexts(profile, [text])
      return Array.isArray(vector) && vector.length ? vector : null
    } catch (error) {
      trace(`recall embed failed: ${error?.message ?? error}`)
      return null
    }
  }
  // 核心记忆注入：设置的自我/核心规则 → 系统提示词段；自述 → 动态上下文
  const corePrompt = new CorePrompt(ctx, kernel, {
    order: config.coreSectionOrder ?? -2000,
    contextOrder: config.coreContextOrder ?? 850,
    timeoutMs: config.requestTimeoutMs ?? 15000,
    trace,
    // 配上向量后，「换个说法提旧事」也能被动召回（0.5.3）
    embedQueryFn: config.recallEmbed === false ? null : embedForRecall,
  })

  const toolDisposers = []
  const extraDisposers = []
  let disposed = false
  let stopped = false
  let loadedHost = hostHandlers

  const getHost = async () => {
    if (!loadedHost) loadedHost = await loadHostHandlers()
    return loadedHost
  }

  /** 内核 llm.request → 外壳模型调用 → llm.result。 */
  const onLlmRequest = async frame => {
    try {
      const host = await getHost()
      const data = await host.handleLlmRequest(ctx, frame, {
        provider: config.provider,
        model: config.model,
        maxTokens: config.maxTokens,
        requestTimeoutMs: config.llmTimeoutMs ?? 120000,
      }, modelSettings)
      kernel.send('llm.result', data, {
        request_id: frame.request_id,
        session_id: frame.session_id,
        agent_id: frame.agent_id,
      })
    } catch (error) {
      kernel.send('llm.result', {
        request_id: frame?.data?.request_id || frame?.request_id || '',
        error: { code: 'llm_error', message: String(error?.message ?? error) },
      }, { request_id: frame?.request_id })
    }
  }

  /** 起进程 + 等就绪 + 下发库路径；首启与重启共用。 */
  const connect = async () => {
    kernel.start()
    kernel.off?.('llm.request', onLlmRequest)   // 重启时避免重复订阅
    kernel.on('llm.request', onLlmRequest)
    await waitHealthy(kernel)
    // 环境变量已给出库路径；再显式 set 一次，保证运行期配置一致
    await kernel.request('config.set', { patch: { store: { db_path: dbFile } } })
    await refreshSettings()
    // 核心记忆在每次（重）连后刷新；失败不阻塞启动
    try {
      await corePrompt.refresh()
    } catch (error) {
      trace(`core refresh failed: ${error?.message ?? error}`)
    }
  }

  /**
   * 内核退出看护：按退避重启。工具/命令/路由都持有同一个 kernel 对象，
   * 重启后不需要重新注册；用尽预算就停在“未运行”，日志里留痕。
   */
  const restarter = createRestartPolicy({
    delays: Array.isArray(restartDelays) && restartDelays.length
      ? restartDelays : DEFAULT_RESTART_DELAYS,
    trace,
    restart: connect,
  })

  const onKernelExit = info => {
    if (disposed || stopped) return
    restarter.schedule(info)
  }

  /** 内核 embed.request → 外壳 embedding 端点 → embed.result。 */
  const handleEmbedRequest = async frame => {
    const payload = frame?.data || {}
    const requestId = payload.request_id || frame?.request_id || ''
    try {
      const vectors = await embedTexts(modelSettings.embedding, payload.input || [])
      kernel.send('embed.result', {
        request_id: requestId,
        vectors,
        model: modelSettings.embedding.model,
        dim: vectors[0]?.length || 0,
      }, { request_id: frame?.request_id })
    } catch (error) {
      kernel.send('embed.result', {
        request_id: requestId,
        error: { message: String(error?.message ?? error) },
      }, { request_id: frame?.request_id })
    }
  }

  /** 记忆写入完成后刷新核心记忆（版本不变时内部不更新文本）。 */
  const onKernelFrame = frame => {
    if (frame?.type === 'embed.request') {
      void handleEmbedRequest(frame)
      return
    }
    if (frame?.type === 'notify' && frame?.data?.kind === 'settings') {
      void refreshSettings()
      return
    }
    if (frame?.type === 'notify' && frame?.data?.kind === 'core') {
      corePrompt.refresh().catch(error => {
        trace(`core refresh failed: ${error?.message ?? error}`)
      })
      return
    }
    if (frame?.type !== 'ingest.completed') return
    // 落库确认：这批消息可以从待确认队列删掉了
    bridge.onCommitted(frame?.job_id ?? frame?.data?.job_id)
    corePrompt.refresh().catch(error => {
      trace(`core refresh failed: ${error?.message ?? error}`)
    })
  }

  const start = async () => {
    if (disposed) return { tools: 0 }     // 已被卸载：别再起进程
    trace(`start (kernel=${script})`)
    stopped = false
    corePrompt.attach()                   // 只需注册一次；重启不重复
    // 召回按「本步的会话 + 本步的用户输入」在提示词组装前刷新（0.5.1）
    try {
      corePrompt.bindStep()
    } catch (error) {
      trace(`recall bindStep failed: ${error?.message ?? error}`)
    }
    await connect()

    if (disposed) return { tools: 0 }
    const host = await getHost()
    if (disposed) return { tools: 0 }
    toolDisposers.push(...host.registerKernelTools(ctx, kernel, await kernel.request(
      'tools.schema', {}), {
      agentId,
      timeoutMs: config.requestTimeoutMs ?? 30000,
      getSettings: () => modelSettings,
    }))
    if (disposed) return { tools: toolDisposers.length }
    bridge.attach()
    // **先订阅**再补投：补投的 ingest.completed 可能马上就回来，
    // 订阅晚一步这条确认就丢了，待确认队列会一直留着这批消息。
    kernel.on('frame', onKernelFrame)
    // 上次没等到 completed 的批次补投：内核按 msg_id 判重，重发安全
    try {
      await bridge.resumePending()
    } catch (error) {
      trace(`resume pending failed: ${error?.message ?? error}`)
    }
    // 上次来不及发出去的去抖缓冲（被强杀时保存在磁盘）继续投递
    try {
      await bridge.flushAll()
    } catch (error) {
      trace(`flush restored buffers failed: ${error?.message ?? error}`)
    }
    // 附加能力（命令 / 统计路由）各自隔离：任一失败都不影响工具与会话投递
    for (const [label, register] of [
      ['command:/memory', () => registerMemoryCommand(ctx, {
        bridge, timeoutMs: config.requestTimeoutMs ?? 30000, trace,
      })],
      ['routes:/longmemory/*', () => registerLongmemoryRoutes(ctx, {
        kernel, agentId, timeoutMs: config.requestTimeoutMs ?? 15000, trace,
        getSettings: () => modelSettings,
        // 宿主模型只能在外壳里测：借 llm-bridge 走同一条 ctx.llm 通道
        testLlm: async profile => {
          const host = await getHost()
          return host.handleLlmRequest(ctx, {
            data: {
              request_id: 'settings-test',
              purpose: 'longmemory',
              messages: [{ role: 'user', content: '只回复 OK' }],
            },
          }, {
            provider: config.provider,
            model: config.model,
            maxTokens: config.maxTokens,
            requestTimeoutMs: config.llmTimeoutMs ?? 120000,
          }, { 'model.extract': profile })
        },
      })],
    ]) {
      try {
        const disposer = register()
        if (disposer) extraDisposers.push(disposer)
      } catch (error) {
        trace(`${label} failed: ${error?.message ?? error}`)
      }
    }
    if (!disposed) kernel.on('exit', onKernelExit)
    trace(`started: tools=${toolDisposers.length} extras=${extraDisposers.length}`)
    return { tools: toolDisposers.length }
  }

  const dispose = async () => {
    if (disposed) return
    disposed = true
    stopped = true
    restarter.close()
    kernel.off?.('exit', onKernelExit)
    // 先排空再摘模型回调：反过来的话，最后一批消息触发的摘要没人应答，
    // flush 会白等到超时，那批记忆就丢了。
    try {
      await bridge.flushAll()
    } catch {
      /* 尽力而为：退出路径不抛 */
    }
    kernel.off?.('llm.request', onLlmRequest)
    kernel.off?.('frame', onKernelFrame)
    bridge.dispose?.()
    corePrompt.dispose()
    for (const disposer of [...toolDisposers, ...extraDisposers]) {
      try {
        disposer?.()
      } catch {
        /* ignore */
      }
    }
    toolDisposers.length = 0
    extraDisposers.length = 0
    kernel.stop()
  }

  return { kernel, bridge, corePrompt, start, dispose }
}
