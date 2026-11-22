'use strict'

const { desktopCapturer, screen } = require('electron')

const MAX_CAPTURE_WIDTH = 1920

function listDisplays () {
  const primaryId = screen.getPrimaryDisplay().id
  return screen.getAllDisplays().map((d, index) => ({
    id: d.id,
    name: d.label || `显示器 ${index + 1}`,
    primary: d.id === primaryId,
    bounds: d.bounds,
    scaleFactor: d.scaleFactor
  }))
}

/**
 * 截取指定显示器上的一块矩形区域（仅读取屏幕像素，不接触游戏进程）。
 * region 使用 DIP 坐标，相对该显示器左上角。
 */
async function captureRegion (options = {}) {
  const displays = screen.getAllDisplays()
  const display = displays.find(d => d.id === options.displayId) || screen.getPrimaryDisplay()
  const dipW = display.size.width
  const dipH = display.size.height
  const scale = display.scaleFactor || 1

  const capScale = Math.min(1, MAX_CAPTURE_WIDTH / Math.max(1, dipW * scale))
  const thumbW = Math.max(64, Math.round(dipW * scale * capScale))
  const thumbH = Math.max(64, Math.round(dipH * scale * capScale))

  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width: thumbW, height: thumbH },
    fetchWindowIcons: false
  })

  let source = sources.find(s => String(s.display_id) === String(display.id))
  if (!source && sources.length === 1) source = sources[0]
  if (!source) throw new Error('无法获取屏幕画面，请检查系统屏幕录制权限')

  const image = source.thumbnail
  if (!image || image.isEmpty()) throw new Error('屏幕画面为空，可能处于独占全屏或受保护的画面')

  const imgSize = image.getSize()
  const pxPerDip = imgSize.width / dipW

  const region = options.region
  if (!region || !(region.width > 0) || !(region.height > 0)) {
    throw new Error('尚未框选游戏小地图区域，请先在设置中完成框选')
  }
  let cx = Math.round(region.x * pxPerDip)
  let cy = Math.round(region.y * pxPerDip)
  let cw = Math.round(region.width * pxPerDip)
  let ch = Math.round(region.height * pxPerDip)
  cx = Math.max(0, Math.min(cx, imgSize.width - 8))
  cy = Math.max(0, Math.min(cy, imgSize.height - 8))
  cw = Math.max(8, Math.min(cw, imgSize.width - cx))
  ch = Math.max(8, Math.min(ch, imgSize.height - cy))

  const cropped = image.crop({ x: cx, y: cy, width: cw, height: ch })
  return {
    dataURL: cropped.toDataURL(),
    width: cw,
    height: ch,
    display: { id: display.id, scaleFactor: scale, bounds: display.bounds },
    capturedAt: Date.now()
  }
}

module.exports = { listDisplays, captureRegion }
