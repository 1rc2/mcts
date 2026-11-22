export const TYPES = {
  chest: { label: '宝箱', color: '#e3b25c', shape: 'square' },
  anchor: { label: '锚点', color: '#63c7ff', shape: 'diamond' },
  material: { label: '材料', color: '#8fdc7a', shape: 'circle' },
  echo: { label: '声骸', color: '#c98dff', shape: 'ring' },
  enemy: { label: '敌人', color: '#ff6b6b', shape: 'diamond' },
  puzzle: { label: '解谜', color: '#ff9a6b', shape: 'hex' },
  quest: { label: '任务', color: '#ff6f91', shape: 'star' },
  npc: { label: 'NPC', color: '#ffd166', shape: 'circle' },
  custom: { label: '自定义', color: '#e6ebf2', shape: 'pin' }
}

export function typeMeta (type) {
  return TYPES[type] || TYPES.custom
}

export function clamp (v, min, max) {
  return Math.min(max, Math.max(min, v))
}

export function formatDistance (meters) {
  if (!Number.isFinite(meters)) return ''
  if (meters < 1000) return `${Math.round(meters)} m`
  return `${(meters / 1000).toFixed(2)} km`
}

export function formatTime (ts) {
  if (!ts) return ''
  const d = new Date(ts)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function debounce (fn, wait) {
  let timer = null
  const wrapped = (...args) => {
    clearTimeout(timer)
    timer = setTimeout(() => fn(...args), wait)
  }
  wrapped.flush = (...args) => {
    clearTimeout(timer)
    fn(...args)
  }
  return wrapped
}
