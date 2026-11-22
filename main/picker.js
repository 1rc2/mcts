'use strict'

const { BrowserWindow, screen, ipcMain } = require('electron')
const path = require('node:path')

let active = null

/**
 * 全屏半透明遮罩，用于框选游戏小地图所在区域。
 * 只做屏幕区域坐标采集，不读取任何游戏数据。
 */
function openRegionPicker (displayId) {
  if (active) {
    active.win.focus()
    return active.promise
  }

  const displays = screen.getAllDisplays()
  const display = displays.find(d => d.id === displayId) || screen.getPrimaryDisplay()

  const win = new BrowserWindow({
    x: display.bounds.x,
    y: display.bounds.y,
    width: display.bounds.width,
    height: display.bounds.height,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  win.setAlwaysOnTop(true, 'screen-saver')
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })

  let settled = false
  let resolveFn = null

  const promise = new Promise(resolve => { resolveFn = resolve })

  const finish = (result) => {
    if (settled) return
    settled = true
    ipcMain.removeListener('picker:done', onDone)
    ipcMain.removeListener('picker:cancel', onCancel)
    active = null
    if (!win.isDestroyed()) win.close()
    resolveFn(result)
  }

  function onDone (event, rect) {
    if (event.sender !== win.webContents) return
    if (!rect || !(rect.width > 8) || !(rect.height > 8)) return finish(null)
    finish({
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height)
    })
  }

  function onCancel (event) {
    if (event.sender !== win.webContents) return
    finish(null)
  }

  ipcMain.on('picker:done', onDone)
  ipcMain.on('picker:cancel', onCancel)
  win.on('closed', () => finish(null))

  win.loadURL('app://bundle/renderer/picker.html')
  win.once('ready-to-show', () => {
    win.show()
    win.focus()
  })

  active = { win, promise }
  return promise
}

module.exports = { openRegionPicker }
