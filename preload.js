'use strict'

const { contextBridge, ipcRenderer } = require('electron')

const ALLOWED_EVENTS = new Set(['shortcut:action', 'settings:changed'])

async function invoke (channel, payload) {
  const res = await ipcRenderer.invoke(channel, payload)
  if (!res || res.ok !== true) {
    const error = new Error((res && res.error && res.error.message) || '操作失败')
    error.code = (res && res.error && res.error.code) || 'error'
    throw error
  }
  return res.data
}

contextBridge.exposeInMainWorld('wuwa', {
  invoke,
  on (channel, handler) {
    if (!ALLOWED_EVENTS.has(channel) || typeof handler !== 'function') return () => {}
    const listener = (_event, payload) => handler(payload)
    ipcRenderer.on(channel, listener)
    return () => ipcRenderer.removeListener(channel, listener)
  },
  picker: {
    done (rect) { ipcRenderer.send('picker:done', rect) },
    cancel () { ipcRenderer.send('picker:cancel') }
  }
})
