/**
 * 本地管理路由：`/longmemory/*`（只监听回环，供设置页使用）。
 *
 *   GET  /longmemory/stats                              统计（设置页概况）
 *   POST /longmemory/api                                管理 API：{op: tables|list|get|insert|update|delete, ...}
 *   GET  /longmemory/logs?source=shell|kernel&lines=200 插件日志尾部
 *
 * 安全边界：这些接口能改记忆库，只接受本机回环请求；不提供跨机访问。
 */
import { closeSync, openSync, readSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

import { optionalService } from './services.js'

const MAX_BODY_BYTES = 1_000_000
const MAX_LOG_LINES = 1000
const LOOPBACK = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1'])

/** 本机回环判断（AF_INET / AF_INET6 两种写法）。 */
export function isLoopback(req) {
  const address = req?.socket?.remoteAddress ?? ''
  return LOOPBACK.has(address)
}

/** 取 Host 头的裸主机名（去端口、去 IPv6 方括号、小写）。 */
export function hostnameOf(hostHeader) {
  const text = String(hostHeader ?? '').trim().toLowerCase()
  if (!text) return ''
  if (text.startsWith('[')) {
    const end = text.indexOf(']')
    return end < 0 ? text : text.slice(1, end)
  }
  const colon = text.lastIndexOf(':')
  return colon === -1 ? text : text.slice(0, colon)
}

/** Host 必须是本机名：挡住 DNS rebinding（域名解析到 127.0.0.1 也不算本机）。 */
export function isAllowedHost(hostHeader) {
  const host = hostnameOf(hostHeader)
  return host === '' || LOOPBACK_HOSTS.has(host)
}

/** Origin 必须是本机页面：浏览器跨站写请求一定会带 Origin。 */
export function isAllowedOrigin(origin) {
  const text = String(origin ?? '').trim()
  if (!text) return true          // 非浏览器客户端（curl/测试）没有 Origin
  try {
    return LOOPBACK_HOSTS.has(hostnameOf(new URL(text).host))
  } catch {
    return false                  // 解析不了就按可疑处理
  }
}

/**
 * 本地管理接口的准入判定（返回 null 表示放行）。
 *
 * 光判 `remoteAddress` 不够：浏览器发出的请求天然来自回环。真正的门是
 * ① Host 必须本机（防 rebinding）② Origin 必须本机页面 ③ 写请求必须是
 * `application/json`（`text/plain` 是免预检的简单请求，必须堵死）。
 */
export function authorizeRequest(req, { write = false } = {}) {
  if (!isLoopback(req)) {
    return { status: 403, error: '仅允许本机访问' }
  }
  if (!isAllowedHost(req?.headers?.host)) {
    return { status: 403, error: 'host_not_allowed' }
  }
  if (String(req?.headers?.['sec-fetch-site'] ?? '').toLowerCase() === 'cross-site') {
    return { status: 403, error: 'cross_site_blocked' }
  }
  if (!isAllowedOrigin(req?.headers?.origin)) {
    return { status: 403, error: 'origin_not_allowed' }
  }
  if (write) {
    const type = String(req?.headers?.['content-type'] ?? '').toLowerCase()
    if (!type.startsWith('application/json')) {
      return { status: 415, error: 'content_type_must_be_json' }
    }
  }
  return null
}

/** 读取 JSON 请求体（带大小上限）。 */
export function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', chunk => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error('请求体过大'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8').trim()
      if (!text) return resolve({})
      try {
        resolve(JSON.parse(text))
      } catch (error) {
        reject(new Error(`JSON 解析失败: ${error.message}`))
      }
    })
    req.on('error', reject)
  })
}

/** 日志尾部最多读这么多字节：日志再大也不能每次轮询整文件读。 */
export const MAX_TAIL_BYTES = 256 * 1024

/**
 * 读日志尾部；文件不存在返回空数组。
 *
 * :param baseDir: 日志目录，测试可指定临时目录
 */
export function tailLog(source, {
  baseDir = path.join(homedir(), '.dsh', 'dsh-longmemory'),
  maxBytes = MAX_TAIL_BYTES,
} = {}) {
  const file = path.join(baseDir, source === 'kernel' ? 'kernel.stderr.log' : 'shell.log')
  try {
    const size = statSync(file).size
    const start = Math.max(0, size - maxBytes)
    const length = size - start
    if (length <= 0) return []
    const buffer = Buffer.alloc(length)
    const fd = openSync(file, 'r')
    try {
      readSync(fd, buffer, 0, length, start)
    } finally {
      closeSync(fd)
    }
    let lines = buffer.toString('utf8').split('\n')
    // 从文件中间开始读会把第一行切成半截，丢掉它
    if (start > 0) lines = lines.slice(1)
    return lines
  } catch {
    return []
  }
}

function sendJson(res, status, payload) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(JSON.stringify(payload))
}

export function registerLongmemoryRoutes(ctx, {
  kernel, agentId = '', timeoutMs = 15_000, trace = () => {},
} = {}) {
  // 服务名是 `webServer`（cordis Service 名，大小写敏感）；`webserver` 仅为容错
  const server = optionalService(ctx, 'webServer') ?? optionalService(ctx, 'webserver')
  if (!server || typeof server.register !== 'function') {
    trace('webServer service unavailable: /longmemory/* 未注册')
    return null
  }

  const dispose = server.register({
    kind: 'prefix',
    path: '/longmemory',
    async handler(req, res) {
      const denied = authorizeRequest(req, { write: req.method === 'POST' })
      if (denied) {
        sendJson(res, denied.status, { ok: false, error: denied.error })
        return
      }
      const url = new URL(req.url ?? '/', 'http://localhost')
      const route = url.pathname.replace(/\/+$/, '')

      try {
        if (route === '/longmemory/stats'
            && (req.method === 'GET' || req.method === 'HEAD')) {
          const sessionId = url.searchParams.get('session_id') ?? ''
          const data = await kernel.request('stats', {}, { sessionId, agentId, timeoutMs })
          sendJson(res, 200, data)
          return
        }

        if (route === '/longmemory/logs' && req.method === 'GET') {
          const source = url.searchParams.get('source') === 'kernel' ? 'kernel' : 'shell'
          const lines = Number(url.searchParams.get('lines') ?? 200)
          const data = tailLog(source)
          sendJson(res, 200, {
            source,
            lines: data.slice(-Math.max(1, Math.min(lines || 200, MAX_LOG_LINES))),
          })
          return
        }

        if (route === '/longmemory/api') {
          if (req.method !== 'POST') {
            sendJson(res, 405, { ok: false, error: 'method_not_allowed' })
            return
          }
          const payload = await readJsonBody(req).catch(error => ({ __error: error }))
          if (payload && payload.__error) {
            sendJson(res, 400, { ok: false, error: 'bad_request',
                                 message: String(payload.__error?.message ?? payload.__error) })
            return
          }
          const data = await kernel.request('admin', payload, {
            sessionId: payload.session_id ?? '', agentId, timeoutMs,
          })
          sendJson(res, 200, data)
          return
        }

        sendJson(res, 404, { ok: false, error: 'not_found' })
      } catch (error) {
        trace(`route ${route} failed: ${error?.message ?? error}`)
        sendJson(res, 503, { ok: false, error: 'kernel_unavailable',
                             message: String(error?.message ?? error) })
      }
    },
  })
  trace('routes registered: /longmemory/{stats,api,logs}')
  return dispose
}

/** 兼容旧名字。 */
export const registerStatsRoute = registerLongmemoryRoutes
