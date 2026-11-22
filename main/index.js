'use strict'

const {
  app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, screen,
  safeStorage, protocol, nativeImage, dialog, shell
} = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const { AppStore } = require('./store')
const { SecureStore } = require('./secure-store')
const capture = require('./capture')
const { openRegionPicker } = require('./picker')
const { AccountService, SyncError } = require('./sync')

const APP_ROOT = path.join(__dirname, '..')
const IS_DEV = process.argv.includes('--dev')

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'app',
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true }
  }
])

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon'
}

let mainWindow = null
let tray = null
let store = null
let secureStore = null
let account = null
let isQuitting = false
let boundsTimer = null

const SHORTCUT_MAP = {
  togglePanel: 'toggle-panel',
  markComplete: 'mark-complete',
  nextTarget: 'next-target',
  toggleRecognition: 'toggle-recognition'
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
}

app.on('second-instance', () => {
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
})

/* ------------------------------------------------------------------ */
/* 静态资源协议：app://bundle/... 读取安装目录，app://user/... 读取用户目录 */
/* ------------------------------------------------------------------ */
async function serveAppFile (request) {
  let url
  try {
    url = new URL(request.url)
  } catch {
    return new Response('Bad request', { status: 400 })
  }
  const host = url.hostname
  const base = host === 'bundle'
    ? APP_ROOT
    : host === 'user'
      ? app.getPath('userData')
      : null
  if (!base) return new Response('Not found', { status: 404 })

  const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  const root = path.resolve(base)
  const target = path.resolve(root, rel)
  if (target !== root && !target.startsWith(root + path.sep)) {
    return new Response('Forbidden', { status: 403 })
  }
  try {
    const data = await fs.promises.readFile(target)
    const type = MIME[path.extname(target).toLowerCase()] || 'application/octet-stream'
    return new Response(data, {
      status: 200,
      headers: {
        'Content-Type': type,
        'Cache-Control': 'no-store',
        'Cross-Origin-Opener-Policy': 'same-origin'
      }
    })
  } catch {
    return new Response('Not found', { status: 404 })
  }
}

/* ------------------------------------------------------------------ */
/* 数据读取                                                            */
/* ------------------------------------------------------------------ */
function readJson (rel) {
  return JSON.parse(fs.readFileSync(path.join(APP_ROOT, rel), 'utf8'))
}

function getMapInfo () {
  const base = readJson('data/map.json')
  const s = store.getSettings()
  if (s.map && s.map.imagePath) {
    return {
      ...base,
      imageUrl: `app://user/${s.map.imagePath}`,
      width: s.map.width || base.width,
      height: s.map.height || base.height,
      fileName: s.map.fileName || path.basename(s.map.imagePath),
      custom: true
    }
  }
  return { ...base, imageUrl: `app://bundle/${base.image || 'assets/maps/world-map.jpg'}`, custom: false }
}

function getMarkers () {
  const bundled = readJson('data/markers.json')
  const remote = account.getRemoteMarkers()
  return {
    bundled: bundled.markers || [],
    remote: remote.markers || [],
    remoteSource: remote.source || '',
    remoteUpdatedAt: remote.updatedAt || 0
  }
}

function getRoutes () {
  const bundled = readJson('data/routes.json')
  const progress = store.getProgress()
  return {
    bundled: bundled.routes || [],
    local: progress.localRoutes || []
  }
}

function normalizeRoutes (list) {
  if (!Array.isArray(list)) return []
  const out = []
  for (const raw of list.slice(0, 200)) {
    if (!raw || typeof raw !== 'object') continue
    const points = []
    for (const p of (Array.isArray(raw.points) ? raw.points : []).slice(0, 500)) {
      const u = Number(p && p.u)
      const v = Number(p && p.v)
      if (!Number.isFinite(u) || !Number.isFinite(v) || u < 0 || u > 1 || v < 0 || v > 1) continue
      points.push({
        u: +u.toFixed(5),
        v: +v.toFixed(5),
        markerId: p.markerId ? String(p.markerId).slice(0, 80) : undefined,
        label: p.label ? String(p.label).slice(0, 60) : undefined
      })
    }
    if (!points.length) continue
    out.push({
      id: `local-route-${Date.now().toString(36)}-${out.length}`,
      name: String(raw.name || '导入路线').slice(0, 60),
      author: String(raw.author || '本机导入').slice(0, 40),
      desc: String(raw.desc || '').slice(0, 200),
      points
    })
  }
  return out
}

function bootstrapPayload () {
  return {
    settings: store.getSettings(),
    progress: store.getProgress(),
    account: account.status(),
    map: getMapInfo(),
    markers: getMarkers(),
    routes: getRoutes(),
    appVersion: app.getVersion(),
    platform: process.platform,
    displays: capture.listDisplays()
  }
}

/* ------------------------------------------------------------------ */
/* 窗口与托盘                                                          */
/* ------------------------------------------------------------------ */
function boundsVisible (bounds) {
  if (!bounds || typeof bounds.x !== 'number' || typeof bounds.y !== 'number') return false
  return screen.getAllDisplays().some(d => {
    const b = d.bounds
    const overlapX = Math.min(bounds.x + (bounds.width || 0), b.x + b.width) - Math.max(bounds.x, b.x)
    const overlapY = Math.min(bounds.y + (bounds.height || 0), b.y + b.height) - Math.max(bounds.y, b.y)
    return overlapX > 60 && overlapY > 60
  })
}

function applyWindowSettings () {
  if (!mainWindow || mainWindow.isDestroyed()) return
  const w = store.getSettings().window
  mainWindow.setAlwaysOnTop(Boolean(w.alwaysOnTop), 'floating')
  mainWindow.setOpacity(Math.min(1, Math.max(0.35, Number(w.opacity) || 1)))
  if (w.locked) {
    mainWindow.setResizable(false)
    mainWindow.setMinimumSize(mainWindow.getSize()[0], mainWindow.getSize()[1])
    mainWindow.setMaximumSize(mainWindow.getSize()[0], mainWindow.getSize()[1])
  } else {
    mainWindow.setResizable(true)
    mainWindow.setMinimumSize(360, 520)
    mainWindow.setMaximumSize(9999, 9999)
  }
  rebuildTrayMenu()
}

function createWindow () {
  const s = store.getSettings()
  const savable = boundsVisible({ x: s.window.x, y: s.window.y, width: s.window.width, height: s.window.height })

  mainWindow = new BrowserWindow({
    width: s.window.width || 480,
    height: s.window.height || 860,
    x: savable ? s.window.x : undefined,
    y: savable ? s.window.y : undefined,
    minWidth: 360,
    minHeight: 520,
    frame: false,
    show: false,
    backgroundColor: '#10161b',
    roundedCorners: true,
    skipTaskbar: false,
    title: '鸣潮探索助手',
    icon: path.join(APP_ROOT, 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(APP_ROOT, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
      backgroundThrottling: false
    }
  })

  mainWindow.loadURL('app://bundle/renderer/index.html')
  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
    applyWindowSettings()
    if (IS_DEV) mainWindow.webContents.openDevTools({ mode: 'detach' })
  })

  mainWindow.on('close', (event) => {
    if (isQuitting) return
    event.preventDefault()
    mainWindow.hide()
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  const rememberBounds = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (mainWindow.isMinimized() || mainWindow.isMaximized() || mainWindow.isFullScreen()) return
    clearTimeout(boundsTimer)
    boundsTimer = setTimeout(() => {
      const [width, height] = mainWindow.getSize()
      const [x, y] = mainWindow.getPosition()
      store.updateSettings({ window: { x, y, width, height } })
    }, 350)
  }
  mainWindow.on('move', rememberBounds)
  mainWindow.on('resize', rememberBounds)

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
}

function showWindow () {
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
}

function rebuildTrayMenu () {
  if (!tray) return
  const s = store.getSettings()
  const visible = Boolean(mainWindow && mainWindow.isVisible())
  const menu = Menu.buildFromTemplate([
    {
      label: visible ? '隐藏面板' : '显示面板',
      click: () => {
        if (!mainWindow) return
        if (mainWindow.isVisible()) mainWindow.hide()
        else showWindow()
        rebuildTrayMenu()
      }
    },
    { type: 'separator' },
    {
      label: '窗口置顶',
      type: 'checkbox',
      checked: Boolean(s.window.alwaysOnTop),
      click: (item) => {
        store.updateSettings({ window: { alwaysOnTop: item.checked } })
        applyWindowSettings()
        sendToRenderer('settings:changed', store.getSettings())
      }
    },
    {
      label: '识图定位',
      type: 'checkbox',
      checked: Boolean(s.recognition.enabled),
      click: () => sendToRenderer('shortcut:action', 'toggle-recognition')
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true
        app.quit()
      }
    }
  ])
  tray.setContextMenu(menu)
  tray.setToolTip('鸣潮探索助手')
}

function createTray () {
  const icon = nativeImage.createFromPath(path.join(APP_ROOT, 'assets', 'tray.png'))
  tray = new Tray(icon)
  tray.on('click', () => {
    if (!mainWindow) return
    if (mainWindow.isVisible() && !mainWindow.isFocused()) showWindow()
    else if (mainWindow.isVisible()) mainWindow.hide()
    else showWindow()
    rebuildTrayMenu()
  })
  rebuildTrayMenu()
}

/* ------------------------------------------------------------------ */
/* 全局快捷键                                                          */
/* ------------------------------------------------------------------ */
function sendToRenderer (channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload)
  }
}

function registerShortcuts () {
  globalShortcut.unregisterAll()
  const shortcuts = store.getSettings().shortcuts
  const failed = []
  const registered = []
  for (const [key, action] of Object.entries(SHORTCUT_MAP)) {
    const accelerator = String(shortcuts[key] || '').trim()
    if (!accelerator) continue
    try {
      const ok = globalShortcut.register(accelerator, () => {
        /* 窗口在托盘时，快捷键先恢复窗口；否则只切换面板 */
        if (action === 'toggle-panel' && mainWindow && !mainWindow.isVisible()) {
          showWindow()
          return
        }
        sendToRenderer('shortcut:action', action)
      })
      if (ok) registered.push({ key, accelerator })
      else failed.push({ key, accelerator, reason: '快捷键已被系统或其他程序占用' })
    } catch (err) {
      failed.push({ key, accelerator, reason: err.message })
    }
  }
  return { registered, failed }
}

/* ------------------------------------------------------------------ */
/* IPC                                                                */
/* ------------------------------------------------------------------ */
function handle (channel, fn) {
  ipcMain.handle(channel, async (event, payload) => {
    try {
      const data = await fn(event, payload)
      return { ok: true, data }
    } catch (err) {
      const code = err instanceof SyncError ? err.code : (err && err.code) || 'error'
      return { ok: false, error: { code, message: (err && err.message) || String(err) } }
    }
  })
}

function registerIpc () {
  handle('app:bootstrap', () => bootstrapPayload())

  handle('settings:update', (event, patch) => {
    const next = store.updateSettings(patch || {})
    applyWindowSettings()
    if (patch && patch.shortcuts) {
      const result = registerShortcuts()
      return { settings: next, shortcuts: result }
    }
    return { settings: next }
  })

  handle('shortcuts:reload', () => registerShortcuts())

  handle('window:action', (event, payload = {}) => {
    if (!mainWindow) return { visible: false }
    const action = payload.action
    if (action === 'minimize') {
      mainWindow.hide()
      rebuildTrayMenu()
      return { hidden: true }
    }
    if (action === 'hide') {
      mainWindow.hide()
      rebuildTrayMenu()
      return { hidden: true }
    }
    if (action === 'show') {
      showWindow()
      rebuildTrayMenu()
      return { hidden: false }
    }
    if (action === 'toggleAlwaysOnTop') {
      const next = !store.getSettings().window.alwaysOnTop
      store.updateSettings({ window: { alwaysOnTop: next } })
      applyWindowSettings()
      return { alwaysOnTop: next }
    }
    if (action === 'setOpacity') {
      const value = Math.min(1, Math.max(0.35, Number(payload.value) || 1))
      store.updateSettings({ window: { opacity: value } })
      applyWindowSettings()
      return { opacity: value }
    }
    if (action === 'setLocked') {
      const locked = Boolean(payload.value)
      if (!locked) {
        mainWindow.setMaximumSize(9999, 9999)
        mainWindow.setResizable(true)
      }
      store.updateSettings({ window: { locked } })
      applyWindowSettings()
      return { locked }
    }
    if (action === 'quit') {
      isQuitting = true
      app.quit()
      return { quitting: true }
    }
    return { ok: false }
  })

  handle('progress:save', (event, progress) => {
    const saved = store.saveProgress(progress || {})
    return { updatedAt: saved.updatedAt }
  })

  handle('map:import', async () => {
    if (!mainWindow) return { canceled: true }
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '导入高清大地图图片',
      properties: ['openFile'],
      filters: [{ name: '图片', extensions: ['png', 'jpg', 'jpeg', 'webp'] }]
    })
    if (result.canceled || !result.filePaths.length) return { canceled: true }
    const file = result.filePaths[0]
    const image = nativeImage.createFromPath(file)
    if (image.isEmpty()) throw new Error('无法读取该图片文件')
    const size = image.getSize()
    if (size.width < 256 || size.height < 256) throw new Error('图片尺寸过小，请使用完整大地图')
    const dir = path.join(app.getPath('userData'), 'maps')
    fs.mkdirSync(dir, { recursive: true })
    const ext = (path.extname(file) || '.jpg').toLowerCase()
    const fileName = `world-${Date.now()}${ext}`
    fs.copyFileSync(file, path.join(dir, fileName))
    store.updateSettings({
      map: { imagePath: `maps/${fileName}`, fileName: path.basename(file), width: size.width, height: size.height }
    })
    return { canceled: false, map: getMapInfo() }
  })

  handle('map:reset', () => {
    store.updateSettings({ map: { imagePath: null, fileName: null, width: null, height: null } })
    return { map: getMapInfo() }
  })

  handle('markers:import', async () => {
    const res = await account.importMarkersFile(mainWindow)
    return { ...res, markers: getMarkers() }
  })

  handle('routes:import', async () => {
    if (!mainWindow) return { canceled: true }
    const picked = await dialog.showOpenDialog(mainWindow, {
      title: '导入探索路线（JSON）',
      properties: ['openFile'],
      filters: [{ name: 'JSON', extensions: ['json'] }]
    })
    if (picked.canceled || !picked.filePaths.length) return { canceled: true }
    let data
    try {
      data = JSON.parse(fs.readFileSync(picked.filePaths[0], 'utf8'))
    } catch (err) {
      throw new Error(`无法解析文件：${err.message}`)
    }
    const list = Array.isArray(data) ? data : (data && data.routes)
    const routes = normalizeRoutes(list)
    if (!routes.length) throw new Error('文件中没有可用路线（points 需要包含 0~1 的 u/v 坐标）')
    const p = store.getProgress()
    store.saveProgress({ localRoutes: [...p.localRoutes, ...routes] })
    return { canceled: false, routes: getRoutes(), count: routes.length }
  })

  handle('capture:region', async (event, payload = {}) => {
    const s = store.getSettings()
    return capture.captureRegion({
      displayId: payload.displayId ?? s.recognition.displayId,
      region: payload.region || s.recognition.region
    })
  })

  handle('displays:list', () => capture.listDisplays())

  handle('picker:open', async (event, payload = {}) => {
    const s = store.getSettings()
    if (mainWindow) mainWindow.hide()
    const rect = await openRegionPicker(payload.displayId ?? s.recognition.displayId)
    if (mainWindow) showWindow()
    if (rect) store.updateSettings({ recognition: { region: rect } })
    return { canceled: !rect, region: rect || store.getSettings().recognition.region }
  })

  handle('account:status', () => account.status())
  handle('account:openAuthorize', () => account.openAuthorizePage())
  handle('account:completeLogin', (event, payload) => account.completeLogin(payload || {}))
  handle('account:logout', () => account.logout())
  handle('sync:pull', async () => {
    const res = await account.pullProgress()
    return { ...res, markers: getMarkers() }
  })
  handle('sync:push', async () => account.pushProgress(store.getProgress()))
  handle('sync:export', () => account.exportProgress(mainWindow, store.getProgress()))

  handle('sync:import', async () => {
    const res = await account.importProgress(mainWindow)
    if (res.canceled) return res
    const p = store.getProgress()
    const mergedCompleted = { ...p.completed, ...res.completed }
    const customMap = new Map(p.custom.map(m => [m.id, m]))
    for (const item of res.custom || []) {
      if (item && item.id) customMap.set(item.id, item)
    }
    const saved = store.saveProgress({
      completed: mergedCompleted,
      custom: [...customMap.values()],
      favorites: p.favorites,
      localRoutes: p.localRoutes
    })
    return { ...res, progress: saved }
  })

  handle('app:openUserData', () => shell.openPath(app.getPath('userData')))
  handle('app:info', () => ({
    version: app.getVersion(),
    userData: app.getPath('userData'),
    safeStorage: (() => { try { return safeStorage.isEncryptionAvailable() } catch { return false } })()
  }))
}

/* ------------------------------------------------------------------ */
/* 生命周期                                                            */
/* ------------------------------------------------------------------ */
app.whenReady().then(() => {
  app.setAppUserModelId('com.local.wuwa.mapcompanion')
  Menu.setApplicationMenu(null)

  protocol.handle('app', serveAppFile)

  const userDataDir = app.getPath('userData')
  store = new AppStore(userDataDir)
  secureStore = new SecureStore(userDataDir)
  account = new AccountService({ store, secureStore, userDataDir })

  registerIpc()
  createWindow()
  createTray()
  registerShortcuts()

  app.on('activate', () => {
    if (!mainWindow) createWindow()
    else showWindow()
  })
})

app.on('before-quit', () => {
  isQuitting = true
})

app.on('will-quit', () => {
  globalShortcut.unregisterAll()
})

app.on('window-all-closed', () => {
  /* 托盘常驻，不退出 */
})
