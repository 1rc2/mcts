import { MapView } from './mapview.js'
import { RecognitionController } from './recognition.js'
import { TYPES, typeMeta, clamp, formatDistance, debounce } from './meta.js'

const $ = (sel, root = document) => root.querySelector(sel)
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)]
const api = (channel, payload) => window.wuwa.invoke(channel, payload)

const TYPE_ORDER = ['chest', 'anchor', 'material', 'echo', 'enemy', 'puzzle', 'quest', 'npc', 'custom']

const state = {
  settings: null,
  progress: { completed: {}, custom: [], favorites: [], localRoutes: [] },
  account: null,
  map: null,
  markers: { bundled: [], remote: [] },
  routes: { bundled: [], local: [] },
  query: '',
  listQuery: '',
  activeTab: 'points',
  selectedId: null,
  activeRouteId: null,
  targetIndex: 0,
  player: null,
  mode: 'idle',
  appInfo: null
}

let view = null
let recognition = null
let allMarkers = []
let markerIndex = new Map()
let lastHover = null

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */
function toast (text, ms = 2200) {
  const el = $('#toast')
  el.textContent = text
  el.hidden = false
  clearTimeout(toast._t)
  toast._t = setTimeout(() => { el.hidden = true }, ms)
}

function banner (text, { primary, onPrimary, onClose } = {}) {
  const el = $('#banner')
  $('#banner-text').textContent = text
  const p = $('#banner-primary')
  p.hidden = !primary
  p.textContent = primary || '确定'
  p.onclick = () => { el.hidden = true; onPrimary && onPrimary() }
  $('#banner-close').onclick = () => { el.hidden = true; onClose && onClose() }
  el.hidden = false
}

function closeBanner () {
  $('#banner').hidden = true
}

function icon (name) {
  return `<svg><use href="#i-${name}"/></svg>`
}

function esc (s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]))
}

/* ------------------------------------------------------------------ */
/* 模态框                                                              */
/* ------------------------------------------------------------------ */
let modalResolve = null

function openModal ({ title, bodyHTML, okText = '确定', cancelText = '取消', showCancel = true, onMount }) {
  $('#modal-title').textContent = title
  const body = $('#modal-body')
  body.innerHTML = bodyHTML
  $('#modal-ok').textContent = okText
  $('#modal-cancel').textContent = cancelText
  $('#modal-cancel').hidden = !showCancel
  $('#modal-root').hidden = false
  onMount && onMount(body)

  return new Promise(resolve => {
    modalResolve = resolve
  })
}

function closeModal (value) {
  $('#modal-root').hidden = true
  const r = modalResolve
  modalResolve = null
  if (r) r(value)
}

function confirmDialog (title, text, okText = '确定') {
  return openModal({
    title,
    bodyHTML: `<p>${esc(text)}</p>`,
    okText
  }).then(v => v === true)
}

function promptDialog ({ title, fields, okText = '保存' }) {
  const html = fields.map(f => {
    if (f.type === 'textarea') {
      return `<label>${esc(f.label)}<textarea data-field="${f.name}" placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea></label>`
    }
    if (f.type === 'select') {
      const opts = f.options.map(o => `<option value="${esc(o.value)}"${o.value === f.value ? ' selected' : ''}>${esc(o.label)}</option>`).join('')
      return `<label>${esc(f.label)}<select data-field="${f.name}">${opts}</select></label>`
    }
    return `<label>${esc(f.label)}<input type="text" data-field="${f.name}" value="${esc(f.value || '')}" placeholder="${esc(f.placeholder || '')}" spellcheck="false" /></label>`
  }).join('')
  return openModal({
    title,
    bodyHTML: html,
    okText,
    onMount: (body) => {
      const first = body.querySelector('input, textarea')
      if (first) setTimeout(() => first.focus(), 30)
    }
  }).then(v => (v === true ? readModalFields(fields.map(f => f.name)) : null))
}

function readModalFields (names) {
  const out = {}
  for (const name of names) {
    const el = $(`[data-field="${name}"]`, $('#modal-body'))
    out[name] = el ? el.value.trim() : ''
  }
  return out
}

/* ------------------------------------------------------------------ */
/* 点位数据                                                            */
/* ------------------------------------------------------------------ */
function rebuildMarkers () {
  const remote = state.markers.remote || []
  const custom = state.progress.custom || []
  allMarkers = []
  markerIndex = new Map()
  for (const m of [...remote, ...(state.markers.bundled || []), ...custom]) {
    if (!m || !m.id || markerIndex.has(m.id)) continue
    m.x = m.u * (state.map?.width || 1000)
    m.y = m.v * (state.map?.height || 1000)
    allMarkers.push(m)
    markerIndex.set(m.id, m)
  }
  refreshMapData()
  renderPointsList()
  renderRoutes()
  renderRouteBar()
  renderMeta()
}

function refreshMapData () {
  if (!view) return
  view.setData({
    markers: allMarkers,
    completed: state.progress.completed,
    filters: state.settings.filters,
    query: state.query,
    hideCompleted: state.settings.filters.hideCompleted
  })
}

function currentRoute () {
  if (!state.activeRouteId) return null
  const all = [...state.routes.local, ...state.routes.bundled]
  return all.find(r => r.id === state.activeRouteId) || null
}

function completionCount () {
  const ids = new Set(allMarkers.map(m => m.id))
  let done = 0
  for (const id of Object.keys(state.progress.completed || {})) {
    if (ids.has(id)) done++
  }
  return { done, total: allMarkers.length }
}

function renderMeta () {
  const { done, total } = completionCount()
  $('#points-meta').textContent = `共 ${total} 个点位 · 已完成 ${done}`
}

function isCompleted (id) {
  return Boolean(state.progress.completed[id])
}

async function persistProgress () {
  try {
    await api('progress:save', state.progress)
  } catch (err) {
    toast(`保存失败：${err.message}`)
  }
}

async function setCompleted (id, value) {
  if (value) {
    if (state.progress.completed[id]) return
    state.progress.completed[id] = Date.now()
  } else {
    if (!state.progress.completed[id]) return
    delete state.progress.completed[id]
  }
  refreshMapData()
  renderPointsList()
  renderMeta()
  renderRouteBar()
  await persistProgress()

  const route = currentRoute()
  const target = route && route.points ? route.points[state.targetIndex] : null
  if (value && target && target.markerId === id) {
    setTimeout(() => moveRouteTarget(1), 60)
  }
}

async function toggleCompleted (id) {
  return setCompleted(id, !isCompleted(id))
}

function markerDistance (m) {
  if (!state.player) return null
  const dx = m.x - state.player.x
  const dy = m.y - state.player.y
  return Math.hypot(dx, dy) * (view?.metersPerPixel() || 1)
}

/* ------------------------------------------------------------------ */
/* 地图事件                                                            */
/* ------------------------------------------------------------------ */
function setupMap () {
  const canvas = $('#map')
  view = new MapView(canvas, {
    onMarkerClick: (m) => {
      if (state.mode === 'locate') {
        applyManualPosition({ u: m.u, v: m.v })
        return
      }
      state.selectedId = m.id
      renderPointsList()
      showMarkerDetail(m)
    },
    onMapClick: (pt) => {
      if (state.mode === 'add') {
        finishAddMarker(pt)
        return
      }
      if (state.mode === 'locate') {
        applyManualPosition(pt)
        return
      }
      view.setSelected(null)
      state.selectedId = null
      renderPointsList()
    },
    onHover: (m, sx, sy) => {
      renderTooltip(m, sx, sy)
    },
    onHoverMove: (m, sx, sy) => {
      renderTooltip(m, sx, sy)
    }
  })

  new ResizeObserver(() => view.resize()).observe($('#map-wrap'))

  $('#map').addEventListener('dblclick', (e) => {
    const rect = canvas.getBoundingClientRect()
    view.zoomBy(1.7, e.clientX - rect.left, e.clientY - rect.top)
  })
}

function renderTooltip (m, sx, sy) {
  const tip = $('#tooltip')
  if (!m) {
    tip.hidden = true
    lastHover = null
    return
  }
  if (!lastHover || lastHover.id !== m.id) {
    const meta = typeMeta(m.type)
    const dist = markerDistance(m)
    tip.innerHTML = `
      <div class="tt-title" style="color:${meta.color}">${esc(m.name)}</div>
      <div class="tt-meta">${esc(meta.label)} · ${esc(m.region || '未知区域')}${isCompleted(m.id) ? ' · 已完成' : ''}</div>
      ${m.drops ? `<div class="tt-drops">${esc(m.drops)}</div>` : ''}
      ${m.note ? `<div class="tt-note">${esc(m.note)}</div>` : ''}
      ${dist !== null ? `<div class="tt-meta">距当前位置约 ${formatDistance(dist)}</div>` : ''}
    `
  }
  const rect = $('#map-wrap').getBoundingClientRect()
  const x = clamp(sx + 14, 6, rect.width - 250)
  const y = clamp(sy + 14, 6, rect.height - 110)
  tip.style.left = `${x}px`
  tip.style.top = `${y}px`
  tip.hidden = false
  lastHover = m
}

async function showMarkerDetail (m) {
  $('#tooltip').hidden = true
  const meta = typeMeta(m.type)
  const dist = markerDistance(m)
  state.selectedId = m.id
  view.setSelected(m.id)
  const html = `
    <div class="row space" style="margin-bottom:6px">
      <div>
        <div style="font-weight:600;color:${meta.color}">${esc(m.name)}</div>
        <div class="muted small">${esc(meta.label)} · ${esc(m.region || '未知区域')}</div>
      </div>
      <span class="badge ${isCompleted(m.id) ? 'on' : ''}">${isCompleted(m.id) ? '已完成' : '未完成'}</span>
    </div>
    ${m.drops ? `<p>掉落 / 获取：${esc(m.drops)}</p>` : ''}
    ${dist !== null ? `<p>距当前位置约 ${formatDistance(dist)}</p>` : ''}
    <p>坐标：${(m.u * 100).toFixed(2)}% , ${(m.v * 100).toFixed(2)}%</p>
    ${m.note ? `<p>备注：${esc(m.note)}</p>` : ''}
    <p class="hint">${m.custom ? '这是你自己添加的私人点位，仅保存在本机。' : '点位数据来自库街区公开地图，标记仅影响本地/已绑定账号的进度。'}</p>
  `
  const result = await openModal({
    title: '点位详情',
    bodyHTML: html,
    okText: isCompleted(m.id) ? '标记未完成' : '标记完成',
    cancelText: '关闭'
  })
  if (result === true) {
    await toggleCompleted(m.id)
    toast(isCompleted(m.id) ? '已标记完成' : '已取消完成')
  }
  view.setSelected(null)
  state.selectedId = null
  renderPointsList()
}

/* ------------------------------------------------------------------ */
/* 手动添加 / 手动定位                                                  */
/* ------------------------------------------------------------------ */
function startAddMarker () {
  closeModal(false)
  state.mode = 'add'
  view.setMode('add')
  banner('在地图上点击要添加私人点位的位置', {
    primary: '取消',
    onPrimary: () => cancelMode()
  })
}

function cancelMode () {
  state.mode = 'idle'
  view?.setMode('idle')
  closeBanner()
}

async function finishAddMarker (pt) {
  state.mode = 'idle'
  view.setMode('idle')
  closeBanner()
  const fields = await promptDialog({
    title: '新增私人点位',
    fields: [
      { name: 'name', label: '点位名称', placeholder: '例如：第三只声骸', value: '' },
      { name: 'type', label: '类型', type: 'select', value: 'custom', options: TYPE_ORDER.map(t => ({ value: t, label: TYPES[t].label })) },
      { name: 'note', label: '备注', type: 'textarea', placeholder: '可选，例如：需要从北侧洞口进入' }
    ],
    okText: '添加'
  })
  if (!fields || !fields.name) {
    if (fields) toast('点位名称不能为空')
    return
  }
  try {
    const marker = {
      id: `custom-${Date.now().toString(36)}`,
      type: fields.type || 'custom',
      name: fields.name.slice(0, 60),
      note: fields.note || '',
      u: +pt.u.toFixed(5),
      v: +pt.v.toFixed(5),
      region: '自定义',
      custom: true
    }
    state.progress.custom = [...state.progress.custom, marker]
    await persistProgress()
    rebuildMarkers()
    toast('已添加私人点位')
  } catch (err) {
    toast(`添加失败：${err.message}`)
  }
}

function startManualLocate () {
  state.mode = 'locate'
  view.setMode('locate')
  banner('点击地图上你当前所在的位置（识图失败时的手动备用方案）', {
    primary: '取消',
    onPrimary: () => cancelMode()
  })
}

function applyManualPosition (pt) {
  state.mode = 'idle'
  view.setMode('idle')
  closeBanner()
  const x = pt.u * state.map.width
  const y = pt.v * state.map.height
  state.player = { x, y, score: 1, manual: true, ts: Date.now() }
  view.setPlayer(state.player)
  recognition?.setPosition(x, y)
  updateRecogBar({ state: 'manual', message: '已手动定位（手动校准）' })
  toast('位置已手动更新')
}

/* ------------------------------------------------------------------ */
/* 识图                                                                */
/* ------------------------------------------------------------------ */
let recogInitDone = false

function updateRecogBar ({ state: st, message, score, ms }) {
  const label = $('#recog-state')
  label.textContent = message || ''
  label.className = `recog-state ${st === 'ok' ? 'on' : (st === 'fail' ? 'fail' : '')}`
  $('#recog-score').textContent = Number.isFinite(score)
    ? `匹配度 ${(score * 100).toFixed(0)}%${Number.isFinite(ms) ? ` · ${ms}ms` : ''}`
    : ''
  $('#btn-manual-locate').hidden = st !== 'fail'
}

function ensureRecognition () {
  if (recognition) return recognition
  recognition = new RecognitionController({
    onStatus: ({ state: st, message, score, ms }) => updateRecogBar({ state: st, message, score, ms }),
    onPosition: (pos) => {
      state.player = { x: pos.x, y: pos.y, score: pos.score, angle: pos.angle, ts: pos.ts }
      view.setPlayer(state.player)
      if (state.settings.recognition.autoCenter && !view.dragging) {
        /* 仅在识别置信度充足时跟随，避免抖动 */
        if (pos.score >= 0.68) view.centerOnPlayer(false)
      }
      renderPointsList()
    },
    onFail: () => {
      updateRecogBar({ state: 'fail', message: '识图连续失败，已切换手动定位' })
      $('#recog-toggle').checked = false
      state.settings.recognition.enabled = false
      saveSettings({ recognition: { enabled: false } })
      toast('识图连续失败，已自动切换为手动点击定位', 3200)
      startManualLocate()
    }
  })
  return recognition
}

async function startRecognition (silent = false) {
  const rec = state.settings.recognition
  if (!rec.region) {
    if (!silent) toast('请先在设置中框选游戏小地图区域')
    $('#recog-toggle').checked = false
    return false
  }
  const ctrl = ensureRecognition()
  try {
    if (!recogInitDone) {
      updateRecogBar({ state: 'init', message: '正在建立地图索引…' })
      await ctrl.buildMapPyramid(view.image, {
        imageUrl: state.map.imageUrl,
        width: state.map.width,
        height: state.map.height
      })
      recogInitDone = true
    }
    await ctrl.start(rec)
    if (state.player) ctrl.setPosition(state.player.x, state.player.y)
    return true
  } catch (err) {
    updateRecogBar({ state: 'fail', message: err.message || '识图启动失败' })
    $('#recog-toggle').checked = false
    state.settings.recognition.enabled = false
    saveSettings({ recognition: { enabled: false } })
    if (!silent) toast(err.message || '识图启动失败')
    return false
  }
}

function stopRecognition () {
  recognition?.stop('识图定位已关闭')
  state.settings.recognition.enabled = false
  $('#recog-toggle').checked = false
  saveSettings({ recognition: { enabled: false } })
}

async function toggleRecognition () {
  if (state.settings.recognition.enabled) {
    stopRecognition()
    toast('识图定位已关闭')
  } else {
    state.settings.recognition.enabled = true
    $('#recog-toggle').checked = true
    saveSettings({ recognition: { enabled: true } })
    const ok = await startRecognition()
    toast(ok ? '识图定位已开启' : '识图未启动，可使用手动定位')
  }
}

/* ------------------------------------------------------------------ */
/* 列表渲染                                                            */
/* ------------------------------------------------------------------ */
function filteredForList () {
  const q = state.listQuery.trim().toLowerCase()
  const hideDone = state.settings.filters.hideCompleted
  return allMarkers.filter(m => {
    if (hideDone && isCompleted(m.id)) return false
    if (state.settings.filters[m.type] === false) return false
    if (!q) return true
    return (
      String(m.name).toLowerCase().includes(q) ||
      String(m.region || '').toLowerCase().includes(q) ||
      String(m.drops || '').toLowerCase().includes(q) ||
      String(m.note || '').toLowerCase().includes(q)
    )
  })
}

function renderPointsList () {
  const host = $('#points-list')
  if (!host) return
  const list = filteredForList()
  if (!list.length) {
    host.innerHTML = '<div class="empty">没有匹配的点位</div>'
    renderMeta()
    return
  }
  const sorted = [...list].sort((a, b) => {
    const da = markerDistance(a)
    const db = markerDistance(b)
    if (da !== null && db !== null) return da - db
    return String(a.name).localeCompare(String(b.name), 'zh-CN')
  })
  const shown = sorted.slice(0, 200)
  host.innerHTML = shown.map(m => {
    const meta = typeMeta(m.type)
    const done = isCompleted(m.id)
    const dist = markerDistance(m)
    const sub = [meta.label, m.region || '未知', m.drops || '', dist !== null ? formatDistance(dist) : ''].filter(Boolean).join(' · ')
    return `
      <div class="row-item ${done ? 'done' : ''} ${m.id === state.selectedId ? 'sel' : ''}" data-id="${esc(m.id)}">
        <span class="ri-dot" style="background:${meta.color};color:${meta.color}"></span>
        <div class="ri-main">
          <div class="ri-name">${esc(m.name)}</div>
          <div class="ri-sub">${esc(sub)}</div>
        </div>
        <div class="ri-actions">
          <button class="icon-btn mini" data-act="locate" title="在地图上定位">${icon('crosshair')}</button>
          <button class="icon-btn mini" data-act="toggle" title="${done ? '标记未完成' : '标记完成'}">${icon(done ? 'refresh' : 'check')}</button>
          ${m.custom ? `<button class="icon-btn mini" data-act="remove" title="删除私人点位">${icon('trash')}</button>` : ''}
        </div>
      </div>
    `
  }).join('')
  renderMeta()
}

function bindPointsList () {
  $('#points-list').addEventListener('click', async (e) => {
    const row = e.target.closest('.row-item')
    if (!row) return
    const id = row.dataset.id
    const marker = markerIndex.get(id)
    if (!marker) return
    const act = e.target.closest('[data-act]')?.dataset.act
    if (act === 'toggle') {
      await toggleCompleted(id)
      toast(isCompleted(id) ? '已标记完成' : '已取消完成')
      return
    }
    if (act === 'locate') {
      view.centerOn(marker.u, marker.v)
      state.selectedId = id
      view.setSelected(id)
      renderPointsList()
      return
    }
    if (act === 'remove') {
      const ok = await confirmDialog('删除私人点位', `确定删除「${marker.name}」吗？`, '删除')
      if (!ok) return
      state.progress.custom = state.progress.custom.filter(m => m.id !== id)
      delete state.progress.completed[id]
      await persistProgress()
      rebuildMarkers()
      toast('已删除')
      return
    }
    state.selectedId = id
    view.centerOn(marker.u, marker.v)
    view.setSelected(id)
    renderPointsList()
    showMarkerDetail(marker)
  })
}

/* ------------------------------------------------------------------ */
/* 路线                                                                */
/* ------------------------------------------------------------------ */
function renderRoutes () {
  const host = $('#routes-list')
  const select = $('#route-select')
  const all = [...state.routes.local, ...state.routes.bundled]
  const favs = new Set(state.progress.favorites || [])
  if (!all.length) {
    host.innerHTML = '<div class="empty">还没有可用路线</div>'
    select.innerHTML = '<option value="">暂无路线</option>'
    return
  }
  select.innerHTML = `<option value="">（不加载路线）</option>` + all.map(r =>
    `<option value="${esc(r.id)}"${r.id === state.activeRouteId ? ' selected' : ''}>${r.local ? '★ ' : ''}${esc(r.name)}</option>`
  ).join('')

  host.innerHTML = all.map(r => {
    const doneCount = (r.points || []).filter(p => p.markerId && isCompleted(p.markerId)).length
    return `
      <div class="row-item ${r.id === state.activeRouteId ? 'sel' : ''}" data-route="${esc(r.id)}">
        <span class="ri-dot" style="background:${r.local ? '#5fd3c4' : '#8b9ca7'};color:#5fd3c4"></span>
        <div class="ri-main">
          <div class="ri-name">${esc(r.name)}</div>
          <div class="ri-sub">${esc(r.author || '未知作者')} · ${(r.points || []).length} 个点位 · 已完成 ${doneCount}</div>
        </div>
        <div class="ri-actions">
          <button class="icon-btn mini ${favs.has(r.id) ? 'active' : ''}" data-act="fav" title="收藏路线">${icon('star')}</button>
          <button class="icon-btn mini" data-act="load" title="加载路线">${icon('right')}</button>
        </div>
      </div>
    `
  }).join('')
}

function bindRoutes () {
  $('#routes-list').addEventListener('click', async (e) => {
    const row = e.target.closest('[data-route]')
    if (!row) return
    const id = row.dataset.route
    const act = e.target.closest('[data-act]')?.dataset.act
    if (act === 'fav') {
      const favs = new Set(state.progress.favorites || [])
      if (favs.has(id)) favs.delete(id)
      else favs.add(id)
      state.progress.favorites = [...favs]
      await persistProgress()
      renderRoutes()
      toast(favs.has(id) ? '已收藏路线' : '已取消收藏')
      return
    }
    loadRoute(id)
  })

  $('#route-select').addEventListener('change', (e) => {
    loadRoute(e.target.value || null)
  })
}

function loadRoute (id) {
  state.activeRouteId = id || null
  const route = currentRoute()
  state.targetIndex = 0
  if (route) {
    const firstUndone = (route.points || []).findIndex(p => !p.markerId || !isCompleted(p.markerId))
    state.targetIndex = firstUndone >= 0 ? firstUndone : 0
  }
  view.setRoute(route, state.targetIndex)
  renderRoutes()
  renderRouteBar()
  saveSettings({ activeRouteId: state.activeRouteId })
  if (route && route.points && route.points.length) {
    const t = route.points[state.targetIndex]
    view.centerOn(t.u, t.v)
    const marker = t.markerId ? markerIndex.get(t.markerId) : null
    toast(marker ? `当前目标：${marker.name}` : `当前目标：路线第 ${state.targetIndex + 1} 点`)
  } else if (!route) {
    toast('已取消路线')
  }
}

function renderRouteBar () {
  const route = currentRoute()
  const bar = $('#route-bar')
  if (!route) {
    bar.hidden = true
    return
  }
  bar.hidden = false
  const pts = route.points || []
  const t = pts[state.targetIndex]
  $('#route-bar-title').textContent = route.name
  if (!t) {
    $('#route-bar-sub').textContent = '这条路线上没有点位'
    return
  }
  const marker = t.markerId ? markerIndex.get(t.markerId) : null
  const done = marker ? isCompleted(marker.id) : false
  const dist = marker ? markerDistance(marker) : null
  const parts = [
    `${state.targetIndex + 1}/${pts.length}`,
    marker ? marker.name : `第 ${state.targetIndex + 1} 点`,
    done ? '已完成' : '待前往',
    dist !== null ? formatDistance(dist) : ''
  ].filter(Boolean)
  $('#route-bar-sub').textContent = parts.join(' · ')
}

function bindRouteBar () {
  $('#btn-route-prev').addEventListener('click', () => moveRouteTarget(-1))
  $('#btn-route-next').addEventListener('click', () => moveRouteTarget(1))
  $('#btn-route-done').addEventListener('click', async () => {
    const route = currentRoute()
    const t = route?.points?.[state.targetIndex]
    if (!t) return
    if (t.markerId) {
      await setCompleted(t.markerId, true)
      toast('已标记当前目标完成')
      moveRouteTarget(1)
    } else {
      toast('该路线点没有关联点位，无法标记完成')
    }
  })
}

function moveRouteTarget (delta) {
  const route = currentRoute()
  if (!route || !route.points?.length) return
  const next = clamp(state.targetIndex + delta, 0, route.points.length - 1)
  state.targetIndex = next
  view.setRoute(route, next)
  renderRouteBar()
  const t = route.points[next]
  view.centerOn(t.u, t.v)
  const marker = t.markerId ? markerIndex.get(t.markerId) : null
  toast(marker ? `目标：${marker.name}` : `目标：第 ${next + 1} 点`)
}

/* ------------------------------------------------------------------ */
/* 筛选 / 搜索                                                          */
/* ------------------------------------------------------------------ */
function renderChips () {
  const host = $('#type-chips')
  host.innerHTML = TYPE_ORDER.map(t => {
    const meta = TYPES[t]
    const on = state.settings.filters[t] !== false
    return `<button class="chip ${on ? 'on' : 'off'}" data-type="${t}" style="--chip-color:${meta.color}">
      <span class="dot" style="background:${meta.color}"></span>${meta.label}
    </button>`
  }).join('')
}

function bindFilters () {
  $('#type-chips').addEventListener('click', async (e) => {
    const chip = e.target.closest('[data-type]')
    if (!chip) return
    const t = chip.dataset.type
    const next = !(state.settings.filters[t] !== false)
    state.settings.filters = { ...state.settings.filters, [t]: next }
    renderChips()
    refreshMapData()
    renderPointsList()
    await saveSettings({ filters: state.settings.filters })
  })

  $('#btn-show-all').addEventListener('click', async () => {
    const filters = {}
    for (const t of TYPE_ORDER) filters[t] = true
    state.settings.filters = { ...state.settings.filters, ...filters }
    renderChips()
    refreshMapData()
    renderPointsList()
    await saveSettings({ filters: state.settings.filters })
    toast('已显示全部类型')
  })

  $('#btn-hide-all').addEventListener('click', async () => {
    const filters = {}
    for (const t of TYPE_ORDER) filters[t] = false
    state.settings.filters = { ...state.settings.filters, ...filters, hideCompleted: state.settings.filters.hideCompleted }
    renderChips()
    refreshMapData()
    renderPointsList()
    await saveSettings({ filters: state.settings.filters })
    toast('已隐藏全部类型')
  })

  const toggleHide = async (value) => {
    state.settings.filters = { ...state.settings.filters, hideCompleted: value }
    $('#hide-completed').checked = value
    $('#btn-toggle-completed').classList.toggle('on', value)
    refreshMapData()
    renderPointsList()
    await saveSettings({ filters: state.settings.filters })
  }
  $('#btn-toggle-completed').addEventListener('click', () => toggleHide(!state.settings.filters.hideCompleted))
  $('#hide-completed').addEventListener('change', (e) => toggleHide(e.target.checked))
}

function bindSearch () {
  const search = $('#search')
  const apply = debounce((value) => {
    state.query = value
    refreshMapData()
  }, 140)
  search.addEventListener('input', () => apply(search.value))
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      state.query = search.value
      refreshMapData()
      const first = allMarkers.find(m => view._matchesQuery(m) && state.settings.filters[m.type] !== false)
      if (first) view.centerOn(first.u, first.v)
    }
    if (e.key === 'Escape') {
      search.value = ''
      state.query = ''
      refreshMapData()
    }
  })
  $('#btn-search-clear').addEventListener('click', () => {
    search.value = ''
    state.query = ''
    refreshMapData()
  })

  const listSearch = $('#list-search')
  const applyList = debounce((v) => {
    state.listQuery = v
    renderPointsList()
  }, 120)
  listSearch.addEventListener('input', () => applyList(listSearch.value))
}

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */
const SHORTCUT_FIELDS = ['togglePanel', 'markComplete', 'nextTarget', 'toggleRecognition']

function applyTheme (theme) {
  document.body.classList.toggle('theme-dark', theme !== 'light')
  document.body.classList.toggle('theme-light', theme === 'light')
  $('#btn-theme').innerHTML = icon(theme === 'light' ? 'moon' : 'sun')
}

function syncSettingsUI () {
  const s = state.settings
  $('#set-ontop').checked = Boolean(s.window.alwaysOnTop)
  $('#btn-pin').classList.toggle('active', Boolean(s.window.alwaysOnTop))
  $('#set-lock').checked = Boolean(s.window.locked)
  $('#set-theme').checked = s.window.theme === 'light'
  $('#set-opacity').value = String(Math.round(s.window.opacity * 100))
  $('#opacity-value').textContent = `${Math.round(s.window.opacity * 100)}%`

  $('#recog-toggle').checked = Boolean(s.recognition.enabled)
  $('#set-recog').checked = Boolean(s.recognition.enabled)
  $('#set-interval').value = String(s.recognition.intervalMs)
  $('#interval-value').textContent = `${s.recognition.intervalMs}ms`
  $('#set-diameter').value = String(s.recognition.mapDiameter)
  $('#diameter-value').textContent = String(s.recognition.mapDiameter)
  $('#set-score').value = String(Math.round(s.recognition.minScore * 100))
  $('#score-value').textContent = `${Math.round(s.recognition.minScore * 100)}%`

  const region = s.recognition.region
  $('#region-info').textContent = region
    ? `小地图区域：${Math.round(region.x)}, ${Math.round(region.y)} · ${Math.round(region.width)}×${Math.round(region.height)}`
    : '尚未框选小地图区域'

  for (const key of SHORTCUT_FIELDS) {
    const el = $(`[data-shortcut="${key}"]`)
    if (el) el.value = s.shortcuts[key] || ''
  }

  for (const el of $$('[data-account]')) {
    el.value = s.account[el.dataset.account] || ''
  }

  $('#hide-completed').checked = Boolean(s.filters.hideCompleted)
  $('#btn-toggle-completed').classList.toggle('on', Boolean(s.filters.hideCompleted))

  $('#map-info').textContent = state.map.custom
    ? `自定义地图：${state.map.fileName}（${state.map.width}×${state.map.height}）`
    : `示例地图（${state.map.width}×${state.map.height}）`

  applyTheme(s.window.theme)
}

async function saveSettings (patch) {
  try {
    const res = await api('settings:update', patch)
    state.settings = res.settings
    if (res.shortcuts) reportShortcuts(res.shortcuts)
    return res.settings
  } catch (err) {
    toast(`设置保存失败：${err.message}`)
    return state.settings
  }
}

function reportShortcuts ({ registered, failed }) {
  if (failed && failed.length) {
    const names = { togglePanel: '显示/隐藏', markComplete: '标记完成', nextTarget: '下一个点位', toggleRecognition: '识图开关' }
    const detail = failed.map(f => `${names[f.key] || f.key}（${f.accelerator}）：${f.reason}`).join('\n')
    banner(`部分快捷键注册失败：\n${detail}`, { primary: '知道了' })
  }
}

function bindSettings () {
  $('#set-ontop').addEventListener('change', async (e) => {
    await saveSettings({ window: { alwaysOnTop: e.target.checked } })
    $('#btn-pin').classList.toggle('active', e.target.checked)
  })

  $('#set-lock').addEventListener('change', async (e) => {
    await api('window:action', { action: 'setLocked', value: e.target.checked })
    await saveSettings({ window: { locked: e.target.checked } })
    toast(e.target.checked ? '窗口尺寸已锁定' : '窗口尺寸已解锁')
  })

  $('#set-theme').addEventListener('change', async (e) => {
    await saveSettings({ window: { theme: e.target.checked ? 'light' : 'dark' } })
    applyTheme(e.target.checked ? 'light' : 'dark')
  })

  $('#set-opacity').addEventListener('input', (e) => {
    const v = Number(e.target.value) / 100
    $('#opacity-value').textContent = `${e.target.value}%`
    api('window:action', { action: 'setOpacity', value: v })
    state.settings.window.opacity = v
  })
  $('#set-opacity').addEventListener('change', () => {
    saveSettings({ window: { opacity: state.settings.window.opacity } })
  })

  $('#set-recog').addEventListener('change', async (e) => {
    if (e.target.checked) {
      state.settings.recognition.enabled = true
      await saveSettings({ recognition: { enabled: true } })
      $('#recog-toggle').checked = true
      const ok = await startRecognition()
      if (!ok) e.target.checked = false
    } else {
      stopRecognition()
    }
  })

  $('#set-interval').addEventListener('input', (e) => {
    const v = Number(e.target.value)
    $('#interval-value').textContent = `${v}ms`
    state.settings.recognition.intervalMs = v
    if (recognition) recognition.settings = state.settings.recognition
  })
  $('#set-interval').addEventListener('change', () => {
    saveSettings({ recognition: { intervalMs: state.settings.recognition.intervalMs } })
  })

  $('#set-diameter').addEventListener('input', (e) => {
    const v = Number(e.target.value)
    $('#diameter-value').textContent = String(v)
    state.settings.recognition.mapDiameter = v
    if (recognition) recognition.settings = state.settings.recognition
  })
  $('#set-diameter').addEventListener('change', () => {
    saveSettings({ recognition: { mapDiameter: state.settings.recognition.mapDiameter } })
  })

  $('#set-score').addEventListener('input', (e) => {
    const v = Number(e.target.value) / 100
    $('#score-value').textContent = `${e.target.value}%`
    state.settings.recognition.minScore = v
    if (recognition) recognition.settings = state.settings.recognition
  })
  $('#set-score').addEventListener('change', () => {
    saveSettings({ recognition: { minScore: state.settings.recognition.minScore } })
  })

  $('#set-display').addEventListener('change', (e) => {
    const id = e.target.value === '' ? null : Number(e.target.value)
    state.settings.recognition.displayId = id
    saveSettings({ recognition: { displayId: id } })
  })

  $('#btn-pick-region').addEventListener('click', async () => {
    try {
      const res = await api('picker:open', { displayId: state.settings.recognition.displayId })
      if (res.canceled) return
      if (res.region) {
        state.settings.recognition.region = res.region
        $('#region-info').textContent = `小地图区域：${Math.round(res.region.x)}, ${Math.round(res.region.y)} · ${Math.round(res.region.width)}×${Math.round(res.region.height)}`
      }
      recogInitDone = false
      toast('已更新小地图区域')
    } catch (err) {
      toast(err.message)
    }
  })

  $('#btn-save-shortcuts').addEventListener('click', async () => {
    const shortcuts = {}
    for (const key of SHORTCUT_FIELDS) {
      const el = $(`[data-shortcut="${key}"]`)
      shortcuts[key] = el ? el.value.trim() : ''
    }
    const res = await saveSettings({ shortcuts })
    if (res) toast('快捷键已保存')
  })

  $('#btn-reset-shortcuts').addEventListener('click', async () => {
    const defaults = { togglePanel: 'Alt+Z', markComplete: 'Alt+X', nextTarget: 'Alt+C', toggleRecognition: 'Alt+V' }
    for (const [k, v] of Object.entries(defaults)) {
      const el = $(`[data-shortcut="${k}"]`)
      if (el) el.value = v
    }
    await saveSettings({ shortcuts: defaults })
    toast('已恢复默认快捷键')
  })

  $('#btn-import-map').addEventListener('click', async () => {
    try {
      const res = await api('map:import')
      if (res.canceled) return
      state.map = res.map
      await loadMapImage()
      syncSettingsUI()
      toast('高清地图已导入')
    } catch (err) {
      toast(err.message)
    }
  })

  $('#btn-reset-map').addEventListener('click', async () => {
    const res = await api('map:reset')
    state.map = res.map
    await loadMapImage()
    syncSettingsUI()
    toast('已恢复示例地图')
  })

  $('#btn-open-data').addEventListener('click', () => api('app:openUserData'))

  $('#btn-import-markers').addEventListener('click', async () => {
    try {
      const res = await api('markers:import')
      if (res.canceled) return
      state.markers = res.markers
      rebuildMarkers()
      toast(`已导入 ${res.count} 个点位`)
    } catch (err) {
      toast(err.message)
    }
  })

  $('#btn-route-import').addEventListener('click', async () => {
    try {
      const res = await api('routes:import')
      if (res.canceled) return
      state.routes = res.routes
      renderRoutes()
      toast(`已导入 ${res.count} 条路线`)
    } catch (err) {
      toast(err.message)
    }
  })

  $('#btn-add-marker').addEventListener('click', () => startAddMarker())
  $('#btn-manual-locate').addEventListener('click', () => startManualLocate())
}

/* ------------------------------------------------------------------ */
/* 账号                                                                */
/* ------------------------------------------------------------------ */
function renderAccount () {
  const a = state.account
  const bound = Boolean(a && a.bound)
  $('#account-name').textContent = bound ? (a.nickname || '已绑定库街区账号') : '未绑定库街区账号'
  $('#account-sub').textContent = bound
    ? `账号标识：${a.uid || '已加密保存在本机'}`
    : '当前使用本地模式，全部地图与标记功能可用。'
  const badge = $('#account-badge')
  badge.textContent = bound ? '已绑定' : '本地'
  badge.classList.toggle('on', bound)
  $('#btn-logout').disabled = !bound
  $('#badge-mode').textContent = bound ? '账号已绑定' : '本地模式'
  $('#badge-mode').classList.toggle('on', bound)
  $('#sync-hint').textContent = a.configured
    ? '已配置官方授权接口，可拉取 / 回传探索进度。'
    : '当前为本地模式：官方进度同步需在下方填写库街区官方授权接口。也可用「导出 / 导入进度文件」在本地互通。'
}

async function pullAccountProgress ({ silent = false } = {}) {
  try {
    const res = await api('sync:pull')
    state.progress.completed = { ...state.progress.completed, ...res.completed }
    await persistProgress()
    if (res.markers) state.markers = res.markers
    rebuildMarkers()
    if (!silent) {
      toast(`已从账号拉取 ${Object.keys(res.completed).length} 条完成记录${res.markerCount ? ` · ${res.markerCount} 个公开点位` : ''}`, 3200)
    }
    return true
  } catch (err) {
    if (!silent) banner(err.message, { primary: '知道了' })
    else console.warn('[sync] 自动拉取失败：', err.message)
    return false
  }
}

function bindAccount () {
  $('#btn-login').addEventListener('click', async () => {
    if (!state.account?.configured) {
      const go = await confirmDialog(
        '尚未配置官方授权接口',
        '本工具只通过库街区官方授权页登录。请先在「官方授权接口配置（高级）」中填写官方开放平台提供的授权地址、令牌接口与 client_id，再执行登录。是否现在前往填写？',
        '去填写'
      )
      if (go) {
        state.activeTab = 'account'
        switchTab('account')
        document.querySelector('details.card')?.setAttribute('open', '')
      }
      return
    }
    try {
      await api('account:openAuthorize')
      const fields = await promptDialog({
        title: '完成官方授权登录',
        fields: [
          { name: 'code', label: '授权码（官方授权页回调返回）', placeholder: '粘贴官方页面返回的 code' },
          { name: 'redirectUri', label: 'redirect_uri', value: state.settings.account.redirectUri || '', placeholder: '与官方登记一致' }
        ],
        okText: '完成登录'
      })
      if (!fields) return
      if (!fields.code) {
        toast('请填写授权码')
        return
      }
      const status = await api('account:completeLogin', fields)
      state.account = status
      renderAccount()
      toast('已通过官方授权绑定账号，凭证已加密保存在本机')
      /* 绑定后自动拉取该账号已完成点位 */
      await pullAccountProgress()
    } catch (err) {
      toast(`登录失败：${err.message}`, 3600)
    }
  })

  $('#btn-logout').addEventListener('click', async () => {
    const ok = await confirmDialog('退出登录', '退出后将清除本机保存的登录凭证，本地标记不受影响。确定退出？', '退出')
    if (!ok) return
    state.account = await api('account:logout')
    renderAccount()
    toast('已退出登录，本机凭证已清除')
  })

  $('#btn-save-account').addEventListener('click', async () => {
    const account = { ...state.settings.account }
    for (const el of $$('[data-account]')) account[el.dataset.account] = el.value.trim()
    account.provider = account.tokenUrl && account.clientId ? 'kuro' : 'local'
    state.settings = await saveSettings({ account })
    state.account = await api('account:status')
    renderAccount()
    toast(account.provider === 'kuro' ? '官方接口配置已保存' : '已保存（仍为本地模式）')
  })

  $('#btn-sync-pull').addEventListener('click', () => pullAccountProgress())

  $('#btn-sync-push').addEventListener('click', async () => {
    try {
      const res = await api('sync:push')
      toast(`已回传 ${res.count} 条完成记录`)
    } catch (err) {
      banner(err.message, { primary: '知道了' })
    }
  })

  $('#btn-progress-export').addEventListener('click', async () => {
    try {
      const res = await api('sync:export')
      if (res.canceled) return
      toast(`已导出 ${res.count} 条记录到 ${res.filePath}`, 3600)
    } catch (err) {
      toast(err.message)
    }
  })

  $('#btn-progress-import').addEventListener('click', async () => {
    try {
      const res = await api('sync:import')
      if (res.canceled) return
      state.progress = res.progress
      await rebuildMarkers()
      toast(`已导入 ${Object.keys(res.completed).length} 条完成记录`)
    } catch (err) {
      toast(err.message)
    }
  })
}

/* ------------------------------------------------------------------ */
/* 抽屉 / 标签 / 窗口按钮                                              */
/* ------------------------------------------------------------------ */
function switchTab (tab) {
  state.activeTab = tab
  for (const btn of $$('#tabs .tab')) btn.classList.toggle('active', btn.dataset.tab === tab)
  for (const panel of $$('.tab-panel')) panel.classList.toggle('active', panel.dataset.panel === tab)
  $('#drawer').classList.remove('collapsed')
  $('#drawer-toggle').innerHTML = icon('chevron-down')
}

function bindDrawer () {
  for (const btn of $$('#tabs .tab')) {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab))
  }
  $('#drawer-toggle').addEventListener('click', () => {
    const drawer = $('#drawer')
    const collapsed = drawer.classList.toggle('collapsed')
    $('#drawer-toggle').innerHTML = icon(collapsed ? 'chevron-up' : 'chevron-down')
    setTimeout(() => view?.resize(), 60)
  })
}

function bindWindowButtons () {
  $('#btn-pin').addEventListener('click', async () => {
    const next = !state.settings.window.alwaysOnTop
    state.settings.window.alwaysOnTop = next
    await api('window:action', { action: 'toggleAlwaysOnTop' })
    await saveSettings({ window: { alwaysOnTop: next } })
    $('#btn-pin').classList.toggle('active', next)
    $('#set-ontop').checked = next
    toast(next ? '窗口已置顶' : '已取消置顶')
  })

  $('#btn-theme').addEventListener('click', async () => {
    const next = state.settings.window.theme === 'light' ? 'dark' : 'light'
    await saveSettings({ window: { theme: next } })
    applyTheme(next)
    $('#set-theme').checked = next === 'light'
  })

  $('#btn-open-settings').addEventListener('click', () => switchTab('settings'))
  $('#btn-min').addEventListener('click', () => api('window:action', { action: 'minimize' }))
  $('#btn-close').addEventListener('click', () => api('window:action', { action: 'hide' }))

  $('#btn-zoom-in').addEventListener('click', () => view.zoomBy(1.4))
  $('#btn-zoom-out').addEventListener('click', () => view.zoomBy(1 / 1.4))
  $('#btn-recenter').addEventListener('click', () => {
    if (state.player) {
      view.centerOnPlayer()
      toast('已回到当前位置')
    } else {
      startManualLocate()
    }
  })

  $('#recog-toggle').addEventListener('change', () => toggleRecognition())

  $('#modal-ok').addEventListener('click', () => closeModal(true))
  $('#modal-cancel').addEventListener('click', () => closeModal(false))
  $('#modal-close').addEventListener('click', () => closeModal(false))
  $('.modal-backdrop').addEventListener('click', () => closeModal(false))

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#modal-root').hidden) closeModal(false)
  })
}

/* ------------------------------------------------------------------ */
/* 快捷键动作                                                          */
/* ------------------------------------------------------------------ */
function bindShortcutEvents () {
  window.wuwa.on('shortcut:action', (action) => {
    if (action === 'toggle-panel') {
      const drawer = $('#drawer')
      const collapsed = drawer.classList.toggle('collapsed')
      $('#drawer-toggle').innerHTML = icon(collapsed ? 'chevron-up' : 'chevron-down')
      setTimeout(() => view?.resize(), 60)
      toast(collapsed ? '面板已收起' : '面板已展开', 1200)
    } else if (action === 'mark-complete') {
      const route = currentRoute()
      const t = route?.points?.[state.targetIndex]
      if (t && t.markerId) {
        setCompleted(t.markerId, !isCompleted(t.markerId))
        toast(isCompleted(t.markerId) ? '已标记完成' : '已取消完成')
      } else if (state.selectedId) {
        toggleCompleted(state.selectedId)
        toast(isCompleted(state.selectedId) ? '已标记完成' : '已取消完成')
      } else {
        toast('请先选择一个点位或加载路线')
      }
    } else if (action === 'next-target') {
      if (currentRoute()) moveRouteTarget(1)
      else toast('当前没有加载路线')
    } else if (action === 'toggle-recognition') {
      toggleRecognition()
    }
  })

  window.wuwa.on('settings:changed', (settings) => {
    state.settings = settings
    syncSettingsUI()
    refreshMapData()
  })
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
async function loadMapImage () {
  try {
    await view.setMap({
      url: state.map.imageUrl,
      width: state.map.width,
      height: state.map.height,
      worldWidthMeters: state.map.worldWidthMeters
    })
    recogInitDone = false
    if (recognition) {
      recognition.resetTracker()
      recognition.stop()
    }
    rebuildMarkers()
    if (state.settings.recognition.enabled) startRecognition(true)
  } catch (err) {
    toast(`地图加载失败：${err.message}`)
  }
}

function renderDisplays (displays) {
  const select = $('#set-display')
  if (!displays || !displays.length) {
    select.innerHTML = '<option value="">未检测到显示器</option>'
    return
  }
  const current = state.settings.recognition.displayId
  select.innerHTML = displays.map(d =>
    `<option value="${d.id}"${d.id === current ? ' selected' : ''}>${esc(d.name)}${d.primary ? '（主）' : ''} ${d.bounds.width}×${d.bounds.height}</option>`
  ).join('')
}

async function boot () {
  setupMap()
  bindDrawer()
  bindWindowButtons()
  bindPointsList()
  bindRoutes()
  bindRouteBar()
  bindFilters()
  bindSearch()
  bindSettings()
  bindAccount()
  bindShortcutEvents()

  let payload
  try {
    payload = await api('app:bootstrap')
  } catch (err) {
    toast(`初始化失败：${err.message}`)
    return
  }

  state.settings = payload.settings
  state.progress = payload.progress
  state.account = payload.account
  state.map = payload.map
  state.markers = payload.markers
  state.routes = payload.routes
  state.appInfo = { version: payload.appVersion, platform: payload.platform }
  $('#app-version').textContent = `v${payload.appVersion}`

  renderChips()
  renderAccount()
  syncSettingsUI()
  renderDisplays(payload.displays || [])
  await loadMapImage()

  if (state.settings.activeRouteId) {
    state.activeRouteId = state.settings.activeRouteId
    const route = currentRoute()
    if (route) {
      state.targetIndex = 0
      view.setRoute(route, 0)
    } else {
      state.activeRouteId = null
    }
    renderRoutes()
  }
  renderRouteBar()
  view.resize()

  if (!state.settings.recognition.region) {
    banner('首次使用：请在「设置 → 识图定位」中框选游戏小地图区域，再开启识图定位；也可以直接手动点击地图定位。', {
      primary: '去设置',
      onPrimary: () => switchTab('settings')
    })
  }

  setTimeout(() => view.fit(), 30)

  /* 已绑定官方账号时，启动后静默拉取一次云端进度 */
  if (state.account?.bound && state.account?.configured) {
    setTimeout(() => pullAccountProgress({ silent: true }), 1200)
  }
}

boot()
