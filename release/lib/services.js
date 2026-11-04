/**
 * 可选服务访问。
 *
 * cordis 的规则：没写进 `inject` 的服务，直接 `ctx.commands` 会抛
 * `cannot get property "commands" without inject`。设置页/命令这类能力
 * 只在 Web 组合里存在，写死 inject 又会让插件在无该服务的组合里起不来，
 * 所以走 `ctx.reflect.get(name, false)` —— 缺失时返回 undefined。
 */
export function optionalService(ctx, name) {
  try {
    const direct = ctx?.[name]
    if (direct) return direct
  } catch {
    /* 未注入：继续走 reflect */
  }
  try {
    return ctx?.reflect?.get?.(name, false)
  } catch {
    return undefined
  }
}
