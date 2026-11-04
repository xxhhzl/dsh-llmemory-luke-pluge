/**
 * 模型调用桥：内核的 `llm.request` → Harness 的 `ctx.llm` → `llm.result`。
 *
 * 内核不持有 provider / model / key，只发 messages 与 purpose；
 * 这里把「system + 单轮 user」翻成 Harness 的 stream 调用并回文本。
 */
import { BlockAssembler, createUserMessage } from '@deepseek-ai/dsh-llm'

const DEFAULT_PURPOSE = 'longmemory'

/** 把内核的 messages 拆成 system 文本与 user 文本（内核只发单轮）。 */
export function splitMessages(messages = []) {
  const system = []
  const user = []
  for (const message of messages) {
    const text = typeof message?.content === 'string'
      ? message.content
      : JSON.stringify(message?.content ?? '')
    if (message?.role === 'system') system.push(text)
    else if (message?.role === 'user') user.push(text)
    else if (message?.role === 'assistant') user.push(text)
  }
  return { system: system.join('\n\n'), user: user.join('\n\n') }
}

/** 从组装好的内容块里取出纯文本。 */
export function blocksToText(blocks = []) {
  return blocks
    .filter(block => block?.type === 'text' && typeof block.text === 'string')
    .map(block => block.text)
    .join('')
}

/** 从 finish 块里取失败信息；成功返回 null。 */
export function finishFailure(finish) {
  if (!finish || finish.kind === undefined) return null
  if (finish.kind === 'error' || finish.kind === 'aborted') {
    const failure = finish.failure ?? {}
    return {
      code: failure.code ?? (finish.kind === 'aborted' ? 'aborted' : 'llm_error'),
      message: failure.message ?? '模型调用失败',
    }
  }
  return null
}

/**
 * 处理一帧 llm.request，返回要回给内核的 llm.result data。
 * 失败也返回结构化 error，让内核按自己的降级策略处理。
 */
export async function handleLlmRequest(ctx, frame, config = {}) {
  const requestId = frame?.data?.request_id || frame?.request_id || ''
  const payload = frame?.data ?? {}
  const { system, user } = splitMessages(payload.messages)

  if (!config.provider || !config.model) {
    return {
      request_id: requestId,
      error: { code: 1004, message: '外壳未配置 provider / model（插件 config）' },
    }
  }

  const controller = new AbortController()
  const timeoutMs = Number(payload.timeout_ms) > 0
    ? Number(payload.timeout_ms)
    : Number(config.requestTimeoutMs ?? 120_000)
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const call = await ctx.llm.prepareCall(
      {
        provider: config.provider,
        model: config.model,
        ...(Number(config.maxTokens) > 0 ? { maxTokens: Number(config.maxTokens) } : {}),
      },
      controller.signal,
    )
    const assembler = new BlockAssembler()
    const messages = [createUserMessage({
      content: [{ type: 'text', text: user }],
      source: { kind: 'dsh-longmemory' },
    })]
    const options = {
      ...call.config,
      messages,
      ...(system ? { system } : {}),
      purpose: payload.purpose || DEFAULT_PURPOSE,
      signal: controller.signal,
    }
    for await (const chunk of call.stream(options)) assembler.push(chunk)

    const failure = finishFailure(assembler.finish)
    if (failure) {
      return {
        request_id: requestId,
        error: { code: failure.code === 'aborted' ? 'timeout' : failure.code,
                 message: failure.message },
      }
    }
    return {
      request_id: requestId,
      content: blocksToText(assembler.blocks()),
      model: call.config.model,
      usage: assembler.usage ?? {},
    }
  } catch (error) {
    return {
      request_id: requestId,
      error: {
        code: error?.code ?? 'llm_error',
        message: String(error?.message ?? error),
      },
    }
  } finally {
    clearTimeout(timer)
  }
}
