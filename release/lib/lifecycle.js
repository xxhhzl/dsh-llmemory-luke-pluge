/**
 * 内核进程的重启策略（纯逻辑，便于离线测试）。
 *
 * 内核是独立子进程，崩一次不该让三个记忆工具永久失效；但也不能无限重启
 * 掩盖“python 路径配错”这类必然失败。这里按退避给有限次机会。
 */

/** 默认退避：1s / 5s / 30s，三轮用尽就不再重启。 */
export const DEFAULT_RESTART_DELAYS = [1_000, 5_000, 30_000]

/** 第 attempt 次重启的等待毫秒数（0 基，超出长度取最后一个）。 */
export function restartDelay(attempt, delays = DEFAULT_RESTART_DELAYS) {
  if (!delays.length) return 0
  const index = Math.min(Math.max(0, Number(attempt) || 0), delays.length - 1)
  return delays[index]
}

/**
 * 重启调度器：schedule() 申请一次重启，close() 取消一切（插件卸载时）。
 *
 * @param {object} options
 * @param {number[]} [options.delays]  退避序列
 * @param {Function} options.restart   实际重启动作（通常 spawn + health）
 * @param {Function} [options.trace]   日志
 */
export function createRestartPolicy({
  delays = DEFAULT_RESTART_DELAYS, restart, trace = () => {},
} = {}) {
  let attempts = 0
  let timer = null
  let closed = false

  const schedule = info => {
    if (closed) return false
    if (attempts >= delays.length) {
      trace(`restart budget exhausted (${attempts})`)
      return false
    }
    const delay = restartDelay(attempts, delays)
    attempts += 1
    trace(`restart in ${delay}ms (attempt ${attempts}) code=${info?.code}`)
    timer = setTimeout(() => {
      timer = null
      Promise.resolve()
        .then(restart)
        .catch(error => {
          trace(`restart failed: ${error?.message ?? error}`)
          schedule({ code: 'retry' })
        })
    }, delay)
    timer.unref?.()
    return true
  }

  return {
    schedule,
    get attempts() { return attempts },
    get pending() { return timer !== null },
    reset() { attempts = 0 },
    close() {
      closed = true
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
    },
  }
}
