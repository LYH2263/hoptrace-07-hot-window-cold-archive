async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id, includeCold = false) =>
    req(`/api/waybills/${encodeURIComponent(id)}/timeline${includeCold ? '?include_cold=true' : ''}`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
  hotcoldProof: (id) => req(`/api/waybills/${encodeURIComponent(id)}/hotcold-proof`),
  hotWindows: () => req('/api/hot-windows'),
  setHotWindow: (id, maxPoints) =>
    req(`/api/waybills/${encodeURIComponent(id)}/hot-window`, {
      method: 'PUT',
      body: JSON.stringify({ max_points: maxPoints }),
    }),
}
