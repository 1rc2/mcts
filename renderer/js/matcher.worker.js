import { buildPyramid, locate } from './match-core.js'

let pyramid = null
let last = null

function reply (msg) {
  self.postMessage(msg)
}

self.onmessage = (event) => {
  const msg = event.data
  if (msg.type === 'init') {
    const t0 = Date.now()
    const gray = new Uint8Array(msg.buffer)
    pyramid = buildPyramid(gray, msg.width, msg.height, 6)
    last = null
    reply({ type: 'ready', levels: pyramid.levels.length, ms: Date.now() - t0 })
    return
  }

  if (msg.type === 'reset') {
    last = null
    reply({ type: 'reset-ok' })
    return
  }

  if (msg.type === 'setPosition') {
    last = { x: msg.x, y: msg.y, angle: Number.isFinite(msg.angle) ? msg.angle : 0 }
    reply({ type: 'position-ok' })
    return
  }

  if (msg.type === 'locate') {
    if (!pyramid) {
      reply({ type: 'result', id: msg.id, ok: false, reason: 'not-ready' })
      return
    }
    const patchSource = {
      data: new Uint8Array(msg.buffer),
      width: msg.width,
      height: msg.height
    }
    const res = locate({
      pyramid,
      patchSource,
      mapDiameter: msg.mapDiameter,
      minScore: msg.minScore,
      last
    })
    if (res.ok) {
      if (last && Number.isFinite(last.x)) {
        const dist = Math.hypot(res.x - last.x, res.y - last.y)
        if (dist < 320) {
          res.x = res.x * 0.68 + last.x * 0.32
          res.y = res.y * 0.68 + last.y * 0.32
        }
      }
      last = { x: res.x, y: res.y, angle: res.angle }
    }
    reply({ type: 'result', id: msg.id, ...res })
    return
  }
}
