const K_AT = 'ng.console.at'
const K_RT = 'ng.console.rt'
const K_DEV = 'ng.console.device'

export const tokens = {
  get access() { return localStorage.getItem(K_AT) || '' },
  get refresh() { return localStorage.getItem(K_RT) || '' },
  save(body) {
    if (body.access_token) localStorage.setItem(K_AT, body.access_token)
    if (body.refresh_token) localStorage.setItem(K_RT, body.refresh_token)
  },
  clear() {
    localStorage.removeItem(K_AT)
    localStorage.removeItem(K_RT)
  },
}

export function deviceFp() {
  let v = localStorage.getItem(K_DEV)
  if (!v) {
    v = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now())
    localStorage.setItem(K_DEV, v)
  }
  return v
}

export async function api(path, options = {}, retried = false) {
  const headers = { ...(options.headers || {}) }
  if (tokens.access && !headers.Authorization) headers.Authorization = `Bearer ${tokens.access}`
  const response = await fetch(path, { ...options, headers })
  if (response.status !== 401 || retried || !tokens.refresh || path.startsWith('/auth/')) {
    return response
  }
  const refreshed = await fetch('/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: tokens.refresh }),
  })
  if (!refreshed.ok) {
    tokens.clear()
    return response
  }
  tokens.save(await refreshed.json())
  return api(path, options, true)
}

export async function readJson(response) {
  try {
    return await response.json()
  } catch {
    return null
  }
}

export function errorMessage(body, fallback) {
  const detail = body && body.detail
  if (detail && typeof detail === 'object') return detail.message || fallback
  if (typeof detail === 'string') return detail
  return fallback
}
