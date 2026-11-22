import { typeMeta, clamp } from './meta.js'

const MIN_SCALE_FACTOR = 0.75
const MAX_SCALE_FACTOR = 14

export class MapView {
  constructor (canvas, handlers = {}) {
    this.canvas = canvas
    this.ctx = canvas.getContext('2d')
    this.handlers = handlers

    this.image = null
    this.mapWidth = 1000
    this.mapHeight = 1000
    this.worldWidthMeters = 9000

    this.scale = 1
    this.tx = 0
    this.ty = 0

    this.markers = []
    this.query = ''
    this.filters = {}
    this.hideCompleted = false
    this.completed = {}
    this.selectedId = null
    this.hoverId = null

    this.route = null
    this.targetIndex = 0
    this.player = null

    this.mode = 'idle'
    this.pointers = new Map()
    this.dragging = false
    this.moved = 0
    this.lastPointer = null
    this.pinch = null

    this.anim = null
    this.follow = true
    this.pulse = 0
    this.needsAnimation = false
    this.userAdjusted = false
    this._raf = 0
    this._pulseTimer = 0

    this._bind()
    this.resize()
  }

  /* --------------------------- setup --------------------------- */
  setMap ({ url, width, height, worldWidthMeters }) {
    this.mapWidth = width || 1000
    this.mapHeight = height || 1000
    this.worldWidthMeters = worldWidthMeters || 9000
    this.image = null
    this.userAdjusted = false
    return new Promise((resolve, reject) => {
      const img = new Image()
      img.onload = () => {
        this.image = img
        this.fit(false)
        this.render()
        resolve(img)
      }
      img.onerror = () => reject(new Error('地图图片加载失败'))
      img.src = url
    })
  }

  metersPerPixel () {
    return this.worldWidthMeters / this.mapWidth
  }

  setData ({ markers, completed, filters, query, hideCompleted }) {
    this.markers = markers || []
    this.completed = completed || {}
    this.filters = filters || {}
    this.query = query || ''
    this.hideCompleted = Boolean(hideCompleted)
    this.render()
  }

  setSelected (id) {
    this.selectedId = id || null
    this.render()
  }

  setRoute (route, targetIndex = 0) {
    this.route = route || null
    this.targetIndex = targetIndex || 0
    this.needsAnimation = Boolean(this.route)
    this.render()
  }

  setPlayer (pos) {
    this.player = pos || null
    this.render()
  }

  setMode (mode) {
    this.mode = mode
    this.canvas.classList.toggle('crosshair', mode === 'add' || mode === 'locate')
    this.render()
  }

  /* --------------------------- viewport --------------------------- */
  resize () {
    const dpr = window.devicePixelRatio || 1
    const rect = this.canvas.getBoundingClientRect()
    const w = Math.max(1, Math.round(rect.width))
    const h = Math.max(1, Math.round(rect.height))
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
      this.canvas.width = Math.round(w * dpr)
      this.canvas.height = Math.round(h * dpr)
    }
    this.cssWidth = w
    this.cssHeight = h
    this.dpr = dpr
    /* 面板布局稳定前保持自动适配，避免初次启动地图被裁切 */
    if (this.image && !this.userAdjusted && !this.anim) this.fit(false)
    this.render()
  }

  fit (animate = true) {
    const w = this.cssWidth
    const h = this.cssHeight
    const scale = Math.min(w / this.mapWidth, h / this.mapHeight) * 0.98
    const tx = (w - this.mapWidth * scale) / 2
    const ty = (h - this.mapHeight * scale) / 2
    if (animate) this.animateTo(scale, tx, ty)
    else {
      this.scale = scale
      this.tx = tx
      this.ty = ty
    }
  }

  zoomBy (factor, cx, cy) {
    this.userAdjusted = true
    const px = cx ?? this.cssWidth / 2
    const py = cy ?? this.cssHeight / 2
    const fitScale = Math.min(this.cssWidth / this.mapWidth, this.cssHeight / this.mapHeight)
    const next = clamp(this.scale * factor, fitScale * MIN_SCALE_FACTOR, fitScale * MAX_SCALE_FACTOR)
    const ratio = next / this.scale
    this.tx = px - (px - this.tx) * ratio
    this.ty = py - (py - this.ty) * ratio
    this.scale = next
    this.render()
  }

  animateTo (scale, tx, ty, duration = 220) {
    this.anim = {
      from: { scale: this.scale, tx: this.tx, ty: this.ty },
      to: { scale, tx, ty },
      start: performance.now(),
      duration
    }
    this.needsAnimation = true
    this.render()
  }

  centerOn (u, v, zoom = null, animate = true) {
    this.userAdjusted = true
    const x = u * this.mapWidth
    const y = v * this.mapHeight
    const fitScale = Math.min(this.cssWidth / this.mapWidth, this.cssHeight / this.mapHeight)
    const scale = zoom ?? Math.max(this.scale, fitScale * 2.6)
    const tx = this.cssWidth / 2 - x * scale
    const ty = this.cssHeight / 2 - y * scale
    if (animate) this.animateTo(scale, tx, ty)
    else {
      this.scale = scale
      this.tx = tx
      this.ty = ty
      this.render()
    }
  }

  centerOnPlayer (animate = true) {
    if (!this.player) return false
    this.follow = true
    this.userAdjusted = true
    const x = this.player.x
    const y = this.player.y
    const tx = this.cssWidth / 2 - x * this.scale
    const ty = this.cssHeight / 2 - y * this.scale
    if (animate) this.animateTo(this.scale, tx, ty)
    else {
      this.tx = tx
      this.ty = ty
      this.render()
    }
    return true
  }

  toScreen (px, py) {
    return { x: px * this.scale + this.tx, y: py * this.scale + this.ty }
  }

  toMap (sx, sy) {
    return { x: (sx - this.tx) / this.scale, y: (sy - this.ty) / this.scale }
  }

  /* --------------------------- interaction --------------------------- */
  _bind () {
    const c = this.canvas

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        this.resize()
        this.render()
      }
    })

    c.addEventListener('wheel', (e) => {
      e.preventDefault()
      const rect = c.getBoundingClientRect()
      const factor = Math.exp(-e.deltaY * 0.0016)
      this.zoomBy(factor, e.clientX - rect.left, e.clientY - rect.top)
    }, { passive: false })

    c.addEventListener('pointerdown', (e) => {
      c.setPointerCapture(e.pointerId)
      this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })
      this.lastPointer = { x: e.clientX, y: e.clientY }
      this.moved = 0
      this.dragging = true
      c.classList.add('dragging')
      this.anim = null
    })

    c.addEventListener('pointermove', (e) => {
      const rect = c.getBoundingClientRect()
      const sx = e.clientX - rect.left
      const sy = e.clientY - rect.top

      if (this.pointers.has(e.pointerId)) {
        const prev = this.pointers.get(e.pointerId)
        const dx = e.clientX - prev.x
        const dy = e.clientY - prev.y
        this.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY })
        this.moved += Math.abs(dx) + Math.abs(dy)
        if (this.moved > 3) {
          if (this.pointers.size === 1) {
            this.follow = false
            this.userAdjusted = true
            this.tx += dx
            this.ty += dy
            this.render()
          }
        }
      }

      if (!this.dragging) {
        const hit = this.hitTest(sx, sy)
        const id = hit ? hit.id : null
        if (id !== this.hoverId) {
          this.hoverId = id
          this.canvas.style.cursor = hit ? 'pointer' : (this.mode === 'add' || this.mode === 'locate' ? 'crosshair' : 'grab')
          if (this.handlers.onHover) this.handlers.onHover(hit, sx, sy)
          this.render()
        } else if (hit && this.handlers.onHoverMove) {
          this.handlers.onHoverMove(hit, sx, sy)
        }
      }
    })

    const endPointer = (e) => {
      if (!this.pointers.has(e.pointerId)) return
      this.pointers.delete(e.pointerId)
      const wasDragging = this.dragging
      this.dragging = this.pointers.size > 0
      if (!this.dragging) c.classList.remove('dragging')

      if (wasDragging && this.moved <= 4 && this.pointers.size === 0) {
        const rect = c.getBoundingClientRect()
        const sx = e.clientX - rect.left
        const sy = e.clientY - rect.top
        const hit = this.hitTest(sx, sy)
        if (hit) {
          this.selectedId = hit.id
          this.render()
          if (this.handlers.onMarkerClick) this.handlers.onMarkerClick(hit)
        } else {
          const mapPt = this.toMap(sx, sy)
          if (this.handlers.onMapClick) {
            this.handlers.onMapClick({
              u: clamp(mapPt.x / this.mapWidth, 0, 1),
              v: clamp(mapPt.y / this.mapHeight, 0, 1),
              screenX: sx,
              screenY: sy
            }, this.mode)
          }
        }
      }
    }
    c.addEventListener('pointerup', endPointer)
    c.addEventListener('pointercancel', endPointer)
    c.addEventListener('pointerleave', () => {
      if (this.hoverId) {
        this.hoverId = null
        if (this.handlers.onHover) this.handlers.onHover(null)
        this.render()
      }
    })
  }

  hitTest (sx, sy) {
    let best = null
    let bestDist = 12
    for (const m of this.markers) {
      if (!this._markerVisible(m)) continue
      const p = this.toScreen(m.x, m.y)
      const d = Math.hypot(p.x - sx, p.y - sy)
      if (d < bestDist) {
        bestDist = d
        best = m
      }
    }
    return best
  }

  _markerVisible (m) {
    if (this.filters[m.type] === false) return false
    if (this.hideCompleted && this.completed[m.id]) return false
    return true
  }

  _matchesQuery (m) {
    if (!this.query) return true
    const q = this.query.trim().toLowerCase()
    if (!q) return true
    return (
      String(m.name).toLowerCase().includes(q) ||
      String(m.region || '').toLowerCase().includes(q) ||
      String(m.drops || '').toLowerCase().includes(q) ||
      String(m.note || '').toLowerCase().includes(q)
    )
  }

  /* --------------------------- rendering --------------------------- */
  render () {
    /* 窗口最小化到托盘时不绘制，避免后台空转 */
    if (this._raf || document.hidden) return
    this._raf = requestAnimationFrame(() => {
      this._raf = 0
      this._draw()
    })
  }

  _draw () {
    const ctx = this.ctx
    const dpr = this.dpr || 1
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, this.cssWidth, this.cssHeight)

    const now = performance.now()
    if (this.anim) {
      const t = clamp((now - this.anim.start) / this.anim.duration, 0, 1)
      const e = 1 - (1 - t) ** 3
      this.scale = this.anim.from.scale + (this.anim.to.scale - this.anim.from.scale) * e
      this.tx = this.anim.from.tx + (this.anim.to.tx - this.anim.from.tx) * e
      this.ty = this.anim.from.ty + (this.anim.to.ty - this.anim.from.ty) * e
      if (t >= 1) this.anim = null
    }

    if (this.image) {
      ctx.imageSmoothingEnabled = this.scale < 2
      ctx.drawImage(this.image, this.tx, this.ty, this.mapWidth * this.scale, this.mapHeight * this.scale)
    } else {
      ctx.fillStyle = '#101a20'
      ctx.fillRect(0, 0, this.cssWidth, this.cssHeight)
    }

    this._drawRoute(ctx)
    this._drawMarkers(ctx)
    this._drawPlayer(ctx, now)

    this.pulse = now

    if (this.anim) {
      this.render()
      return
    }
    /* 仅路线目标需要呼吸动画：降到约 22fps，避免长时间占用 GPU/CPU */
    if (this.needsAnimation && !this._pulseTimer) {
      this._pulseTimer = setTimeout(() => {
        this._pulseTimer = 0
        this.render()
      }, 45)
    }
  }

  _drawRoute (ctx) {
    if (!this.route || !this.route.points || !this.route.points.length) return
    const pts = this.route.points.map(p => this.toScreen(p.u * this.mapWidth, p.v * this.mapHeight))

    ctx.save()
    ctx.lineJoin = 'round'
    ctx.lineCap = 'round'
    ctx.beginPath()
    pts.forEach((p, i) => { if (i === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y) })
    ctx.strokeStyle = 'rgba(10, 20, 24, 0.65)'
    ctx.lineWidth = 5
    ctx.stroke()
    ctx.setLineDash([7, 5])
    ctx.strokeStyle = 'rgba(95, 211, 196, 0.95)'
    ctx.lineWidth = 2
    ctx.stroke()
    ctx.setLineDash([])

    if (this.scale > 0.16) {
      pts.forEach((p, i) => {
        const done = this.route.points[i].markerId && this.completed[this.route.points[i].markerId]
        ctx.beginPath()
        ctx.arc(p.x, p.y, 8, 0, Math.PI * 2)
        ctx.fillStyle = done ? 'rgba(95, 211, 196, 0.22)' : 'rgba(12, 22, 26, 0.82)'
        ctx.fill()
        ctx.strokeStyle = 'rgba(95, 211, 196, 0.9)'
        ctx.lineWidth = 1.4
        ctx.stroke()
        ctx.fillStyle = done ? 'rgba(95, 211, 196, 0.75)' : '#dff7f3'
        ctx.font = '600 9px "Microsoft YaHei UI", sans-serif'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText(String(i + 1), p.x, p.y + 0.5)
      })
    }

    const target = this.route.points[this.targetIndex]
    if (target) {
      const p = this.toScreen(target.u * this.mapWidth, target.v * this.mapHeight)
      const pulse = 1 + Math.sin(performance.now() / 320) * 0.18
      ctx.beginPath()
      ctx.arc(p.x, p.y, 13 * pulse, 0, Math.PI * 2)
      ctx.strokeStyle = 'rgba(255, 214, 120, 0.95)'
      ctx.lineWidth = 2
      ctx.stroke()
      ctx.beginPath()
      ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2)
      ctx.fillStyle = '#ffd678'
      ctx.fill()
    }
    ctx.restore()
  }

  _drawMarkers (ctx) {
    /* 远景时点位数以万计，按颜色/透明度合批绘制，避免逐个 beginPath+fill */
    if (this.scale < 0.16) {
      this._drawMarkersBatch(ctx)
      return
    }
    const q = this.query.trim()
    const now = performance.now()
    for (const m of this.markers) {
      if (!this._markerVisible(m)) continue
      const p = this.toScreen(m.x, m.y)
      if (p.x < -20 || p.y < -20 || p.x > this.cssWidth + 20 || p.y > this.cssHeight + 20) continue

      const meta = typeMeta(m.type)
      const done = Boolean(this.completed[m.id])
      const matched = q ? this._matchesQuery(m) : true
      const isTarget = this.route && this.route.points[this.targetIndex] &&
        (this.route.points[this.targetIndex].markerId === m.id)
      const isHover = m.id === this.hoverId
      const isSelected = m.id === this.selectedId

      let alpha = 1
      if (q && !matched) alpha = 0.1
      else if (done) alpha = 0.34

      ctx.globalAlpha = alpha
      if (isTarget) {
        const ring = 9 + Math.sin(now / 300) * 2
        ctx.beginPath()
        ctx.arc(p.x, p.y, ring + 4, 0, Math.PI * 2)
        ctx.strokeStyle = 'rgba(255, 214, 120, 0.85)'
        ctx.lineWidth = 1.4
        ctx.stroke()
      }

      const r = isHover || isSelected ? 8 : 6.2
      drawShape(ctx, meta.shape, p.x, p.y, r, meta.color, done)

      if (done) {
        ctx.strokeStyle = 'rgba(10, 18, 20, 0.85)'
        ctx.lineWidth = 1.6
        ctx.beginPath()
        ctx.moveTo(p.x - 2.6, p.y)
        ctx.lineTo(p.x - 0.4, p.y + 2.2)
        ctx.lineTo(p.x + 3, p.y - 2.4)
        ctx.stroke()
      }

      if (isSelected || isHover) {
        ctx.beginPath()
        ctx.arc(p.x, p.y, r + 3.5, 0, Math.PI * 2)
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.75)'
        ctx.lineWidth = 1.2
        ctx.stroke()
      }
      ctx.globalAlpha = 1
    }
  }

  _drawMarkersBatch (ctx) {
    const q = this.query.trim()
    const buckets = new Map()
    const special = []
    for (const m of this.markers) {
      if (!this._markerVisible(m)) continue
      const p = this.toScreen(m.x, m.y)
      if (p.x < -20 || p.y < -20 || p.x > this.cssWidth + 20 || p.y > this.cssHeight + 20) continue

      const done = Boolean(this.completed[m.id])
      const matched = q ? this._matchesQuery(m) : true
      const alpha = q && !matched ? 0.1 : (done ? 0.34 : 1)
      const meta = typeMeta(m.type)
      const key = `${meta.color}|${alpha}`
      let list = buckets.get(key)
      if (!list) {
        list = []
        buckets.set(key, list)
      }
      list.push(p.x, p.y)

      if (m.id === this.hoverId || m.id === this.selectedId ||
        (this.route && this.route.points[this.targetIndex] &&
          this.route.points[this.targetIndex].markerId === m.id)) {
        special.push({ p, done, meta })
      }
    }

    for (const [key, pts] of buckets) {
      const [color, alpha] = key.split('|')
      ctx.globalAlpha = Number(alpha)
      ctx.fillStyle = color
      ctx.beginPath()
      for (let i = 0; i < pts.length; i += 2) {
        ctx.rect(pts[i] - 2.2, pts[i + 1] - 2.2, 4.4, 4.4)
      }
      ctx.fill()
    }
    ctx.globalAlpha = 1

    for (const { p, meta } of special) {
      ctx.beginPath()
      ctx.arc(p.x, p.y, 9, 0, Math.PI * 2)
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)'
      ctx.lineWidth = 1.4
      ctx.stroke()
      ctx.beginPath()
      ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2)
      ctx.fillStyle = meta.color
      ctx.fill()
    }
  }

  _drawPlayer (ctx, now) {
    if (!this.player) return
    const p = this.toScreen(this.player.x, this.player.y)
    const acc = 10 + (1 - clamp(this.player.score || 0, 0, 1)) * 34
    ctx.save()
    ctx.beginPath()
    ctx.arc(p.x, p.y, acc, 0, Math.PI * 2)
    ctx.fillStyle = 'rgba(95, 211, 196, 0.10)'
    ctx.fill()
    ctx.strokeStyle = 'rgba(95, 211, 196, 0.45)'
    ctx.lineWidth = 1
    ctx.setLineDash([4, 4])
    ctx.stroke()
    ctx.setLineDash([])

    const pulse = 1 + Math.sin(now / 380) * 0.12
    ctx.beginPath()
    ctx.arc(p.x, p.y, 7 * pulse, 0, Math.PI * 2)
    ctx.fillStyle = 'rgba(95, 211, 196, 0.28)'
    ctx.fill()
    ctx.beginPath()
    ctx.arc(p.x, p.y, 3.4, 0, Math.PI * 2)
    ctx.fillStyle = '#7ef0df'
    ctx.fill()
    ctx.strokeStyle = 'rgba(6, 30, 28, 0.9)'
    ctx.lineWidth = 1
    ctx.stroke()
    ctx.restore()
  }

  screenPositionOfMarker (marker) {
    return this.toScreen(marker.x, marker.y)
  }
}

function drawShape (ctx, shape, x, y, r, color, done) {
  ctx.save()
  ctx.beginPath()
  if (shape === 'square') {
    roundRect(ctx, x - r * 0.82, y - r * 0.82, r * 1.64, r * 1.64, r * 0.32)
  } else if (shape === 'diamond') {
    ctx.moveTo(x, y - r)
    ctx.lineTo(x + r * 0.78, y)
    ctx.lineTo(x, y + r)
    ctx.lineTo(x - r * 0.78, y)
    ctx.closePath()
  } else if (shape === 'circle') {
    ctx.arc(x, y, r * 0.78, 0, Math.PI * 2)
  } else if (shape === 'ring') {
    ctx.arc(x, y, r * 0.78, 0, Math.PI * 2)
  } else if (shape === 'hex') {
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI / 3) * i - Math.PI / 6
      const px = x + Math.cos(a) * r * 0.9
      const py = y + Math.sin(a) * r * 0.9
      if (i === 0) ctx.moveTo(px, py)
      else ctx.lineTo(px, py)
    }
    ctx.closePath()
  } else if (shape === 'star') {
    for (let i = 0; i < 10; i++) {
      const a = (Math.PI / 5) * i - Math.PI / 2
      const rad = i % 2 === 0 ? r : r * 0.45
      const px = x + Math.cos(a) * rad
      const py = y + Math.sin(a) * rad
      if (i === 0) ctx.moveTo(px, py)
      else ctx.lineTo(px, py)
    }
    ctx.closePath()
  } else {
    ctx.arc(x, y - r * 0.25, r * 0.62, 0, Math.PI * 2)
    ctx.moveTo(x - r * 0.32, y + r * 0.25)
    ctx.lineTo(x, y + r * 0.95)
    ctx.lineTo(x + r * 0.32, y + r * 0.25)
    ctx.closePath()
  }

  if (shape === 'ring') {
    ctx.strokeStyle = color
    ctx.lineWidth = 2.2
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(x, y, r * 0.28, 0, Math.PI * 2)
    ctx.fillStyle = color
    ctx.fill()
  } else {
    ctx.fillStyle = done ? 'rgba(120, 132, 138, 0.55)' : color
    ctx.fill()
    ctx.strokeStyle = 'rgba(8, 16, 20, 0.75)'
    ctx.lineWidth = 1.1
    ctx.stroke()
  }
  ctx.restore()
}

function roundRect (ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2)
  ctx.moveTo(x + rr, y)
  ctx.arcTo(x + w, y, x + w, y + h, rr)
  ctx.arcTo(x + w, y + h, x, y + h, rr)
  ctx.arcTo(x, y + h, x, y, rr)
  ctx.arcTo(x, y, x + w, y, rr)
  ctx.closePath()
}
