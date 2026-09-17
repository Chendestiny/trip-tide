// 排程引擎 · Python 数值语义（round / 定点格式化）
//
// 这不是洁癖，是**一致性要求**：离线版要和联机版给出同一份行程文案，
// 而 Python 与 JS 在「恰好 .5」上的舍入方向相反：
//
//     Python  round(742.5)  → 742      （银行家舍入，取偶数侧）
//     Python  f"{6.25:.1f}" → "6.2"
//     JS      Math.round(742.5) → 743
//     JS      (6.25).toFixed(1) → "6.3"   （受二进制表示影响，还会再飘一次）
//
// 实测这会让「日均游览约 742 分钟」变成 743、「6.2 小时」变成 6.3 ——
// 用户看不出对错，但两侧文案对不上就没法做逐字回归。

/** Python 的 round()：四舍六入五取偶 */
export function pyRound(x) {
  const f = Math.floor(x)
  const d = x - f
  if (d > 0.5) return f + 1
  if (d < 0.5) return f
  return f % 2 === 0 ? f : f + 1 // 恰好 .5 → 取偶数侧
}

/** Python 的定点格式化 `f"{x:.{digits}f}"` 的等价物（返回值是字符串） */
export function pyFixed(x, digits = 0) {
  const p = 10 ** digits
  const r = pyRound(x * p) / p
  return r.toFixed(digits) // 此时不会再发生进位，toFixed 只负责补齐小数位
}
