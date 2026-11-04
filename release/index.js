/**
 * 插件入口（打包产物）。
 *
 * Harness 按 package.json 的 exports["."] 加载本文件；实现全部在 lib/ 下，
 * 这样 src/js 与 release/lib 可以逐字节比对（scripts/build_release.py --check）。
 */
export { apply, Config, createShell, inject, name } from './lib/index.js'
