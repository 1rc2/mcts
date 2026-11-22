'use strict'

const { shell, dialog } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const { JsonFile } = require('./json-file')

const ALLOWED_TYPES = new Set(['chest', 'anchor', 'material', 'echo', 'puzzle', 'quest', 'custom'])

class SyncError extends Error {
  constructor (code, message) {
    super(message)
    this.code = code
  }
}

function normalizeHttpUrl (value, label) {
  const raw = String(value || '').trim()
  if (!raw) throw new SyncError('missing_config', `${label}未配置`)
  let url
  try {
    url = new URL(raw)
  } catch {
    throw new SyncError('bad_url', `${label}不是合法的 URL`)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new SyncError('bad_url', `${label}必须使用 http/https`)
  }
  return url.toString()
}

async function requestJson (url, { method = 'GET', headers = {}, body, timeout = 15000 } = {}) {
  let res
  try {
    res = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(timeout) })
  } catch (err) {
    throw new SyncError('network', `网络请求失败：${err.message}`)
  }
  const text = await res.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!res.ok) {
    const msg = (data && (data.error_description || data.message || data.msg)) || `HTTP ${res.status}`
    throw new SyncError(`http_${res.status}`, msg)
  }
  return data
}

function validateMarkers (list) {
  if (!Array.isArray(list)) return []
  const out = []
  for (const raw of list.slice(0, 20000)) {
    if (!raw || typeof raw !== 'object') continue
    const u = Number(raw.u)
    const v = Number(raw.v)
    const type = ALLOWED_TYPES.has(raw.type) ? raw.type : 'custom'
    if (!Number.isFinite(u) || !Number.isFinite(v) || u < 0 || u > 1 || v < 0 || v > 1) continue
    const id = String(raw.id || '').slice(0, 80)
    if (!id) continue
    out.push({
      id,
      type,
      name: String(raw.name || '未命名点位').slice(0, 80),
      u: +u.toFixed(5),
      v: +v.toFixed(5),
      region: String(raw.region || '').slice(0, 40),
      drops: String(raw.drops || '').slice(0, 120),
      note: String(raw.note || '').slice(0, 500)
    })
  }
  return out
}

class AccountService {
  constructor ({ store, secureStore, userDataDir }) {
    this.store = store
    this.secure = secureStore
    this.remoteMarkersFile = new JsonFile(path.join(userDataDir, 'remote-markers.json'), {
      schema: 1,
      source: '库街区公开地图数据',
      updatedAt: 0,
      markers: []
    })
  }

  getRemoteMarkers () {
    return this.remoteMarkersFile.get()
  }

  status () {
    const token = this.secure.load()
    const settings = this.store.getSettings()
    return {
      bound: Boolean(token && token.accessToken),
      nickname: token ? token.nickname || '' : '',
      uid: token ? token.uid || '' : '',
      provider: settings.account.provider || 'local',
      encryptionAvailable: this.secure.encryptionAvailable(),
      configured: Boolean(
        String(settings.account.authorizeUrl || '').trim() &&
        String(settings.account.tokenUrl || '').trim() &&
        String(settings.account.clientId || '').trim()
      ),
      authorizeUrl: settings.account.authorizeUrl || ''
    }
  }

  async openAuthorizePage () {
    const settings = this.store.getSettings()
    const url = normalizeHttpUrl(settings.account.authorizeUrl, '官方授权地址')
    await shell.openExternal(url)
    return { opened: true }
  }

  async completeLogin ({ code, redirectUri }) {
    const settings = this.store.getSettings()
    const tokenUrl = normalizeHttpUrl(settings.account.tokenUrl, '令牌接口')
    const clientId = String(settings.account.clientId || '').trim()
    if (!clientId) throw new SyncError('missing_config', '缺少 client_id，请在设置中填写官方分配的客户端标识')

    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      code: String(code || '').trim(),
      client_id: clientId,
      redirect_uri: String(redirectUri || settings.account.redirectUri || '')
    })
    if (!body.get('code')) throw new SyncError('missing_code', '请填写官方授权后获得的授权码')

    const data = await requestJson(tokenUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
      body: body.toString()
    })
    if (!data || !data.access_token) {
      throw new SyncError('token_invalid', '令牌接口未返回 access_token，请检查授权配置')
    }
    const token = {
      provider: 'kuro',
      accessToken: data.access_token,
      refreshToken: data.refresh_token || '',
      tokenType: data.token_type || 'Bearer',
      scope: data.scope || '',
      expiresAt: data.expires_in ? Date.now() + Number(data.expires_in) * 1000 : 0,
      uid: data.uid || data.user_id || '',
      nickname: data.nickname || '',
      savedAt: Date.now()
    }
    await this._fetchProfile(token)
    this.secure.save(token)
    return this.status()
  }

  async _fetchProfile (token) {
    const settings = this.store.getSettings()
    if (!settings.account.profileUrl) return
    try {
      const data = await requestJson(normalizeHttpUrl(settings.account.profileUrl, '用户信息接口'), {
        headers: { Authorization: `Bearer ${token.accessToken}`, Accept: 'application/json' }
      })
      const d = (data && data.data) || data || {}
      token.uid = d.uid || d.user_id || token.uid
      token.nickname = d.nickname || d.name || token.nickname
    } catch {
      /* 资料接口失败不影响登录本身 */
    }
  }

  async getValidToken () {
    const token = this.secure.load()
    if (!token || !token.accessToken) return null
    if (!token.expiresAt || token.expiresAt - Date.now() > 60 * 1000) return token
    const settings = this.store.getSettings()
    if (!token.refreshToken || !settings.account.tokenUrl || !settings.account.clientId) return token
    try {
      const data = await requestJson(normalizeHttpUrl(settings.account.tokenUrl, '令牌接口'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
        body: new URLSearchParams({
          grant_type: 'refresh_token',
          refresh_token: token.refreshToken,
          client_id: settings.account.clientId
        }).toString()
      })
      if (data && data.access_token) {
        token.accessToken = data.access_token
        token.refreshToken = data.refresh_token || token.refreshToken
        token.expiresAt = data.expires_in ? Date.now() + Number(data.expires_in) * 1000 : 0
        this.secure.save(token)
      }
    } catch {
      /* 刷新失败时继续使用旧令牌，由服务端判定 */
    }
    return token
  }

  logout () {
    this.secure.clear()
    return this.status()
  }

  async pullProgress () {
    const settings = this.store.getSettings()
    if (settings.account.provider !== 'kuro') {
      throw new SyncError('official_unconfigured',
        '当前为本地模式：官方进度同步需要在「设置 → 账号与同步」中填写库街区官方授权接口。本地进度不受影响。')
    }
    const token = await this.getValidToken()
    if (!token) throw new SyncError('unauthorized', '请先完成官方授权登录')
    const url = normalizeHttpUrl(settings.account.pullUrl, '进度拉取接口')
    const data = await requestJson(url, {
      headers: { Authorization: `Bearer ${token.accessToken}`, Accept: 'application/json' }
    })
    const root = (data && data.data) || data || {}
    const completedSrc = root.completed || root.finished || root.done || []
    const completed = {}
    if (Array.isArray(completedSrc)) {
      for (const item of completedSrc) {
        const id = typeof item === 'string' ? item : item && (item.id || item.markerId)
        if (id) completed[String(id)] = Date.now()
      }
    } else if (completedSrc && typeof completedSrc === 'object') {
      for (const [id, ts] of Object.entries(completedSrc)) {
        completed[id] = Number(ts) || Date.now()
      }
    }
    const remoteMarkers = validateMarkers(root.markers)
    if (remoteMarkers.length) {
      this.remoteMarkersFile.set({
        schema: 1,
        source: '库街区公开地图数据',
        updatedAt: Date.now(),
        markers: remoteMarkers
      })
    }
    return { completed, markerCount: remoteMarkers.length, pulledAt: Date.now() }
  }

  async pushProgress (progress) {
    const settings = this.store.getSettings()
    if (settings.account.provider !== 'kuro') {
      throw new SyncError('official_unconfigured',
        '当前为本地模式：可先用「导出进度文件」保存本机进度；接入官方授权接口后再一键回传。')
    }
    const token = await this.getValidToken()
    if (!token) throw new SyncError('unauthorized', '请先完成官方授权登录')
    const url = normalizeHttpUrl(settings.account.pushUrl, '进度回传接口')
    const completed = Object.keys((progress && progress.completed) || {})
    await requestJson(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token.accessToken}`,
        'Content-Type': 'application/json',
        Accept: 'application/json'
      },
      body: JSON.stringify({ completed, updatedAt: Date.now() })
    })
    return { count: completed.length, pushedAt: Date.now() }
  }

  async exportProgress (parentWindow, progress) {
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
    const result = await dialog.showSaveDialog(parentWindow, {
      title: '导出探索进度',
      defaultPath: `wuwa-progress-${stamp}.json`,
      filters: [{ name: 'JSON', extensions: ['json'] }]
    })
    if (result.canceled || !result.filePath) return { canceled: true }
    const payload = {
      schema: 1,
      app: 'wuwa-map-companion',
      exportedAt: Date.now(),
      completed: progress.completed || {},
      custom: progress.custom || []
    }
    fs.writeFileSync(result.filePath, JSON.stringify(payload, null, 2), 'utf8')
    return { canceled: false, filePath: result.filePath, count: Object.keys(payload.completed).length }
  }

  async importProgress (parentWindow) {
    const result = await dialog.showOpenDialog(parentWindow, {
      title: '导入探索进度',
      properties: ['openFile'],
      filters: [{ name: 'JSON', extensions: ['json'] }]
    })
    if (result.canceled || !result.filePaths.length) return { canceled: true }
    const file = result.filePaths[0]
    let data
    try {
      data = JSON.parse(fs.readFileSync(file, 'utf8'))
    } catch (err) {
      throw new SyncError('bad_file', `无法解析文件：${err.message}`)
    }
    const completed = {}
    const src = (data && (data.completed || data.data)) || {}
    if (Array.isArray(src)) {
      for (const item of src) {
        const id = typeof item === 'string' ? item : item && (item.id || item.markerId)
        if (id) completed[String(id)] = Date.now()
      }
    } else if (src && typeof src === 'object') {
      for (const [id, ts] of Object.entries(src)) completed[id] = Number(ts) || Date.now()
    }
    const custom = validateMarkers(data && data.custom).map(m => ({ ...m, custom: true }))
    return { canceled: false, filePath: file, completed, custom }
  }

  async importMarkersFile (parentWindow) {
    const result = await dialog.showOpenDialog(parentWindow, {
      title: '导入点位数据（JSON）',
      properties: ['openFile'],
      filters: [{ name: 'JSON', extensions: ['json'] }]
    })
    if (result.canceled || !result.filePaths.length) return { canceled: true }
    let data
    try {
      data = JSON.parse(fs.readFileSync(result.filePaths[0], 'utf8'))
    } catch (err) {
      throw new SyncError('bad_file', `无法解析文件：${err.message}`)
    }
    const list = Array.isArray(data) ? data : (data && data.markers)
    const markers = validateMarkers(list)
    if (!markers.length) throw new SyncError('empty_file', '文件中没有可用点位（需要 id/type/u/v 字段，u、v 为 0~1 的归一化坐标）')
    this.remoteMarkersFile.set({
      schema: 1,
      source: path.basename(result.filePaths[0]),
      updatedAt: Date.now(),
      markers
    })
    return { canceled: false, markers, count: markers.length }
  }
}

module.exports = { AccountService, SyncError, validateMarkers }
