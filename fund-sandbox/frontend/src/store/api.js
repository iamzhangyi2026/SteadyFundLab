// 本地 FastAPI 后端的极薄 fetch 封装。
// 开发态：Vite 代理把 /api 转发到 http://127.0.0.1:8765
// 生产态（Electron）：FastAPI 伺服 frontend/dist，/api 同源，无需改任何东西。
// 设计目标：消灭旧前端里散落的 23 个手写 loadXxx，所有取数走这一层。

async function request(method, path, { params, body } = {}) {
  let url = path
  if (params) {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') q.set(k, String(v))
    }
    const s = q.toString()
    if (s) url += (url.includes('?') ? '&' : '?') + s
  }
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(url, opts)
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json()).detail || ''
    } catch {
      detail = ''
    }
    throw new Error(detail || `HTTP ${res.status}`)
  }
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return null
}

export const apiGet = (path, params) => request('GET', path, { params })
export const apiPost = (path, body) => request('POST', path, { body })
export const apiPut = (path, body) => request('PUT', path, { body })
export const apiDelete = (path) => request('DELETE', path)
