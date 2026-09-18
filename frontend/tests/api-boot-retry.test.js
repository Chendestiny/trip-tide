// 接口层 · 后端冷启动重试的单元测试（node --test，零依赖）
//
// 测的是 `src/trip-api.js` 里「首屏报错、刷新才好」那个修复：
//   dev 下 Vite ~0.6s ready，后端要 2~4s，首屏的 /api/health 与 /api/trip/cities
//   会打到还没监听的端口，Vite 代理回 500 + text/plain + 空 body。
//
// 这里用 stub 的 globalThis.fetch 精确复现各种响应签名，不起任何服务。

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { getCities, health, previewPlan, __test__ } from '../src/trip-api.js'

const { BOOT, inBootWindow, looksLikeNotUp } = __test__

// ---------------------------------------------------------------- 测试替身
/** 造一个 Response 形状的最小对象（只实现 trip-api.js 用到的那几个成员） */
function res({ status = 200, body = '', contentType = 'application/json' } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? contentType : null) },
    text: async () => body,
    json: async () => JSON.parse(body),
  }
}

/** Vite 代理在「后端还没监听」时的真实签名：500 + text/plain + 空 body */
const proxyNotUp = () => res({ status: 500, body: '', contentType: 'text/plain' })

/**
 * 装上假的 fetch。
 * @param decide 收到第 n 次调用（从 1 起）时返回 Response 或抛出的 Error
 * @returns 调用记录数组
 */
function withFetch(decide) {
  const real = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET' })
    const out = decide(calls.length, url, opts)
    if (out instanceof Error) throw out
    return out
  }
  const restore = () => { globalThis.fetch = real }
  return { calls, restore }
}

/** 临时把重试节奏改快 + 保证恢复，免得 12×600ms 拖慢测试 */
async function fast(fn) {
  const { attempts, delayMs, windowMs, startedAt } = BOOT
  BOOT.delayMs = 1
  try {
    return await fn()
  } finally {
    Object.assign(BOOT, { attempts, delayMs, windowMs, startedAt })
  }
}

// ---------------------------------------------------------------- 签名判定
test('looksLikeNotUp：只认「5xx 且不是 JSON 错误体」，业务错误一律不算', () => {
  assert.equal(looksLikeNotUp(500, 'text/plain', ''), true) // vite 代理：后端没起来
  assert.equal(looksLikeNotUp(502, 'text/plain', 'Bad gateway'), true)
  assert.equal(looksLikeNotUp(500, 'application/json', '{"detail":"boom"}'), false) // 后端真崩了
  assert.equal(looksLikeNotUp(422, 'application/json', '[]'), false) // 参数校验错
  assert.equal(looksLikeNotUp(404, 'text/plain', ''), false) // 4xx 永远不重
})

test('inBootWindow：窗口内为真，窗口外为假', () => {
  assert.equal(inBootWindow(), true) // 刚 import，必然在窗口内
  assert.equal(inBootWindow(BOOT.startedAt + BOOT.windowMs + 1), false)
})

// ---------------------------------------------------------------- 核心行为
test('GET 在冷启动窗口内自动重试，后端起来后拿到数据（不用手动刷新）', async () => {
  const { calls, restore } = withFetch((n) => (n <= 2 ? proxyNotUp() : res({ body: JSON.stringify([{ name: '成都' }]) })))
  try {
    const data = await fast(() => getCities())
    assert.deepEqual(data, [{ name: '成都' }])
    assert.equal(calls.length, 3, '应当重试到第 3 次才成功')
    assert.equal(calls[0].url, '/api/trip/cities')
  } finally {
    restore()
  }
})

test('POST 绝不重放（/plan 要跑 15~30s LLM，重放就是双倍花钱）', async () => {
  const { calls, restore } = withFetch(() => proxyNotUp())
  try {
    await assert.rejects(
      () => fast(() => previewPlan({ city: '成都', days: 2 })),
      (e) => e.bootRetryable === true, // 签名认出来了，但方法不可重放
    )
    assert.equal(calls.length, 1, `POST 被重放了 ${calls.length} 次`)
    assert.equal(calls[0].method, 'POST')
  } finally {
    restore()
  }
})

test('后端真的崩了（500 + JSON detail）不重试，错误文案照原样抛出', async () => {
  const { calls, restore } = withFetch(() => res({
    status: 500, body: JSON.stringify({ detail: '内部错误' }), contentType: 'application/json',
  }))
  try {
    await assert.rejects(() => fast(() => getCities()), /内部错误/)
    assert.equal(calls.length, 1, '业务 500 不该走冷启动重试')
  } finally {
    restore()
  }
})

test('业务 422 校验错误不重试，且把 detail 数组归一成一句中文', async () => {
  const { calls, restore } = withFetch(() => res({
    status: 422,
    body: JSON.stringify({ detail: [{ msg: 'field required' }] }),
    contentType: 'application/json',
  }))
  try {
    await assert.rejects(() => fast(() => getCities()), /field required/)
    assert.equal(calls.length, 1)
  } finally {
    restore()
  }
})

test('fetch 直接连不上（TypeError）按冷启动处理，会重试', async () => {
  const { calls, restore } = withFetch((n) =>
    (n === 1 ? new TypeError('fetch failed') : res({ body: JSON.stringify([]) })))
  try {
    const data = await fast(() => getCities())
    assert.deepEqual(data, [])
    assert.equal(calls.length, 2)
  } finally {
    restore()
  }
})

test('冷启动窗口过后不再重试：真实错误立刻抛出，不拖慢排查', async () => {
  const { calls, restore } = withFetch(() => proxyNotUp())
  const saved = BOOT.startedAt
  try {
    BOOT.startedAt = Date.now() - BOOT.windowMs - 10 // 假装页面早就加载完了
    await assert.rejects(() => fast(() => getCities()))
    assert.equal(calls.length, 1, `窗口外还在重试（${calls.length} 次）`)
  } finally {
    BOOT.startedAt = saved
    restore()
  }
})

test('重试耗尽后给的是「后端没起来 + 怎么起」，不是干巴巴的 HTTP 500', async () => {
  const { restore } = withFetch(() => proxyNotUp())
  const { attempts } = BOOT
  try {
    BOOT.attempts = 2
    await assert.rejects(
      () => fast(() => getCities()),
      /连不上后端服务[\s\S]*cd backend/,
    )
  } finally {
    BOOT.attempts = attempts
    restore()
  }
})

test('health() 也吃冷启动重试 —— 首屏报错的正是它和 getCities()', async () => {
  const { calls, restore } = withFetch((n) =>
    (n === 1 ? proxyNotUp() : res({ body: JSON.stringify({ ok: true, llm_ready: true }) })))
  try {
    const h = await fast(() => health())
    assert.equal(h.ok, true)
    assert.equal(calls.length, 2)
    assert.equal(calls[0].url, '/api/health', '健康检查不在 /api/trip 前缀下')
  } finally {
    restore()
  }
})

test('health() 连不上时抛的是裸 TypeError，也必须被归一成可重试（端到端跑出来的真 bug）', async () => {
  // 单测喂「500 Response」测不出这条路径 —— Node/浏览器 fetch 在端口没监听时
  // 根本不返回 Response，直接抛 TypeError('fetch failed')。
  const { calls, restore } = withFetch((n) =>
    (n === 1 ? new TypeError('fetch failed') : res({ body: JSON.stringify({ ok: true }) })))
  try {
    const h = await fast(() => health())
    assert.equal(h.ok, true)
    assert.equal(calls.length, 2, 'health() 的网络层失败没走冷启动重试')
  } finally {
    restore()
  }
})

test('health() 在窗口外不重试（避免把后端的真故障藏起来）', async () => {
  const { calls, restore } = withFetch(() => proxyNotUp())
  const saved = BOOT.startedAt
  try {
    BOOT.startedAt = Date.now() - BOOT.windowMs - 10
    await assert.rejects(() => fast(() => health()))
    assert.equal(calls.length, 1)
  } finally {
    BOOT.startedAt = saved
    restore()
  }
})

test('重试期间不产生未处理的 rejection，且每次都打同一个 URL', async () => {
  const { calls, restore } = withFetch((n) => (n <= 3 ? proxyNotUp() : res({ body: '[]' })))
  try {
    await fast(() => getCities())
    assert.equal(calls.length, 4)
    assert.ok(calls.every((c) => c.url === '/api/trip/cities'))
    assert.ok(calls.every((c) => c.method === 'GET'))
  } finally {
    restore()
  }
})