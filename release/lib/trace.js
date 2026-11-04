/**
 * 诊断痕迹：插件运行期的轻量日志（~/.dsh/dsh-longmemory/shell.log）。
 *
 * 只用于回答“插件到底加载到哪一步”，失败一律吞掉——诊断不能影响插件本身。
 */
import { appendFileSync, mkdirSync, renameSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

const MAX_BYTES = 5 * 1024 * 1024

/** 日志目录（内核 stderr 也落这里）。 */
export function logDir() {
  return path.join(homedir(), '.dsh', 'dsh-longmemory')
}

/** 超过上限就滚动一份 .1，避免长期运行把磁盘写满。 */
export function rotateIfNeeded(file, maxBytes = MAX_BYTES) {
  try {
    if (statSync(file).size < maxBytes) return
    renameSync(file, `${file}.1`)
  } catch {
    /* 文件不存在或不可写：忽略 */
  }
}

export function trace(message) {
  try {
    const dir = logDir()
    mkdirSync(dir, { recursive: true })
    const file = path.join(dir, 'shell.log')
    rotateIfNeeded(file)
    appendFileSync(file, `${new Date().toISOString()} ${message}\n`, 'utf8')
  } catch {
    /* 诊断失败不影响插件 */
  }
}
