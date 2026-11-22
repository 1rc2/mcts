'use strict'

const path = require('node:path')
const { JsonFile } = require('./json-file')

const DEFAULT_SETTINGS = {
  schema: 1,
  window: {
    width: 480,
    height: 860,
    x: null,
    y: null,
    alwaysOnTop: true,
    opacity: 0.96,
    locked: false,
    theme: 'dark'
  },
  filters: {
    chest: true,
    anchor: true,
    material: true,
    echo: true,
    enemy: true,
    puzzle: true,
    quest: true,
    npc: true,
    hideCompleted: false
  },
  shortcuts: {
    togglePanel: 'Alt+Z',
    markComplete: 'Alt+X',
    nextTarget: 'Alt+C',
    toggleRecognition: 'Alt+V'
  },
  recognition: {
    enabled: false,
    intervalMs: 1500,
    displayId: null,
    region: null,
    mapDiameter: 720,
    minScore: 0.55,
    autoCenter: true
  },
  account: {
    provider: 'local',
    authorizeUrl: '',
    tokenUrl: '',
    profileUrl: '',
    pullUrl: '',
    pushUrl: '',
    clientId: '',
    redirectUri: ''
  },
  map: {
    imagePath: null,
    fileName: null
  },
  activeRouteId: null,
  drawer: { open: true, tab: 'points' }
}

const DEFAULT_PROGRESS = {
  schema: 1,
  completed: {},
  custom: [],
  favorites: [],
  localRoutes: [],
  updatedAt: 0
}

class AppStore {
  constructor (userDataDir) {
    this.dir = userDataDir
    this.settings = new JsonFile(path.join(userDataDir, 'settings.json'), DEFAULT_SETTINGS)
    this.progress = new JsonFile(path.join(userDataDir, 'progress.json'), DEFAULT_PROGRESS)
  }

  getSettings () {
    return this.settings.get()
  }

  updateSettings (patch) {
    return this.settings.patch(patch || {})
  }

  getProgress () {
    return this.progress.get()
  }

  saveProgress (next) {
    const prev = this.progress.get()
    const merged = {
      ...prev,
      ...(next || {}),
      schema: 1,
      updatedAt: Date.now()
    }
    return this.progress.set(merged)
  }

  addCustomMarker (marker) {
    const p = this.progress.get()
    const id = marker.id || `custom-${Date.now().toString(36)}`
    const item = {
      id,
      type: marker.type || 'custom',
      name: (marker.name || '自定义点位').slice(0, 60),
      note: (marker.note || '').slice(0, 500),
      u: Number(marker.u),
      v: Number(marker.v),
      region: '自定义',
      custom: true,
      createdAt: Date.now()
    }
    p.custom = [...p.custom.filter(m => m.id !== id), item]
    this.progress.set(p)
    return item
  }

  updateCustomMarker (id, patch) {
    const p = this.progress.get()
    const item = p.custom.find(m => m.id === id)
    if (!item) return null
    Object.assign(item, {
      name: patch.name !== undefined ? String(patch.name).slice(0, 60) : item.name,
      note: patch.note !== undefined ? String(patch.note).slice(0, 500) : item.note
    })
    this.progress.set(p)
    return item
  }

  removeCustomMarker (id) {
    const p = this.progress.get()
    p.custom = p.custom.filter(m => m.id !== id)
    delete p.completed[id]
    this.progress.set(p)
    return true
  }

  saveLocalRoute (route) {
    const p = this.progress.get()
    const id = route.id || `local-route-${Date.now().toString(36)}`
    const item = { ...route, id, local: true, savedAt: Date.now() }
    p.localRoutes = [...p.localRoutes.filter(r => r.id !== id), item]
    this.progress.set(p)
    return item
  }

  removeLocalRoute (id) {
    const p = this.progress.get()
    p.localRoutes = p.localRoutes.filter(r => r.id !== id)
    p.favorites = p.favorites.filter(f => f !== id)
    this.progress.set(p)
    return true
  }

  toggleFavorite (id) {
    const p = this.progress.get()
    const has = p.favorites.includes(id)
    p.favorites = has ? p.favorites.filter(f => f !== id) : [...p.favorites, id]
    this.progress.set(p)
    return { favorited: !has, favorites: p.favorites }
  }
}

module.exports = { AppStore, DEFAULT_SETTINGS, DEFAULT_PROGRESS }
