/**
 * 纯计算的地图匹配核心（无 DOM 依赖，可在 Worker 与 Node 测试中复用）。
 *
 * 思路：
 *  - 使用 Sobel 梯度幅值而不是原始灰度，降低亮度差异、以及小地图随镜头旋转带来的影响；
 *  - 构建 1/2 降采样金字塔，在小比例尺上做全局/局部搜索；
 *  - 用零均值归一化互相关（ZNCC）打分；
 *  - 中心区域（角色箭头）与圆形外的 UI 边缘通过 mask 排除。
 */

export function toGrayscale (rgba, width, height) {
  const out = new Uint8Array(width * height)
  for (let i = 0, p = 0, n = width * height; i < n; i++, p += 4) {
    out[i] = (rgba[p] * 77 + rgba[p + 1] * 150 + rgba[p + 2] * 29) >> 8
  }
  return out
}

export function gradientMagnitude (gray, width, height) {
  const out = new Uint8Array(width * height)
  for (let y = 1; y < height - 1; y++) {
    const row = y * width
    for (let x = 1; x < width - 1; x++) {
      const i = row + x
      const tl = gray[i - width - 1], t = gray[i - width], tr = gray[i - width + 1]
      const l = gray[i - 1], r = gray[i + 1]
      const bl = gray[i + width - 1], b = gray[i + width], br = gray[i + width + 1]
      const gx = (tr + 2 * r + br) - (tl + 2 * l + bl)
      const gy = (bl + 2 * b + br) - (tl + 2 * t + tr)
      let m = Math.abs(gx) + Math.abs(gy)
      if (m > 255) m = 255
      out[i] = m
    }
  }
  return out
}

export function downsample2 (src, width, height) {
  const w = Math.max(1, width >> 1)
  const h = Math.max(1, height >> 1)
  const out = new Uint8Array(w * h)
  for (let y = 0; y < h; y++) {
    const y0 = y * 2 * width
    const y1 = Math.min(height - 1, y * 2 + 1) * width
    for (let x = 0; x < w; x++) {
      const x0 = x * 2
      const x1 = Math.min(width - 1, x0 + 1)
      const s = src[y0 + x0] + src[y0 + x1] + src[y1 + x0] + src[y1 + x1]
      out[y * w + x] = s >> 2
    }
  }
  return { data: out, width: w, height: h }
}

/** 由原始灰度建立梯度金字塔（level 0 为全分辨率）。 */
export function buildPyramid (gray, width, height, maxLevels = 6) {
  let base = gradientMagnitude(gray, width, height)
  const levels = [{ width, height, data: base, scale: 1 }]
  let w = width
  let h = height
  for (let i = 1; i < maxLevels; i++) {
    if (w <= 48 || h <= 48) break
    const next = downsample2(levels[i - 1].data, w, h)
    w = next.width
    h = next.height
    levels.push({ width: w, height: h, data: next.data, scale: 2 ** i })
  }
  return { levels }
}

/** 区域平均重采样（灰度）。 */
export function resizeGray (src, sw, sh, dw, dh) {
  if (sw === dw && sh === dh) return src.slice()
  const out = new Uint8Array(dw * dh)
  for (let y = 0; y < dh; y++) {
    const sy0 = Math.floor((y * sh) / dh)
    const sy1 = Math.max(sy0 + 1, Math.floor(((y + 1) * sh) / dh))
    for (let x = 0; x < dw; x++) {
      const sx0 = Math.floor((x * sw) / dw)
      const sx1 = Math.max(sx0 + 1, Math.floor(((x + 1) * sw) / dw))
      let sum = 0
      let count = 0
      for (let sy = sy0; sy < sy1 && sy < sh; sy++) {
        const row = sy * sw
        for (let sx = sx0; sx < sx1 && sx < sw; sx++) {
          sum += src[row + sx]
          count++
        }
      }
      out[y * dw + x] = count ? (sum / count) | 0 : 0
    }
  }
  return out
}

/**
 * 圆形 mask：只统计内切圆内的像素，并挖掉中心区域（角色箭头）。
 * @returns {Float32Array} 1 = 参与计算，0 = 忽略
 */
export function makeMask (size, centerHole = 0.15) {
  const mask = new Float32Array(size * size)
  const c = (size - 1) / 2
  const r = size / 2 - 0.5
  const hole = r * centerHole
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - c
      const dy = y - c
      const d = Math.sqrt(dx * dx + dy * dy)
      mask[y * size + x] = d <= r && d >= hole ? 1 : 0
    }
  }
  return mask
}

export function normalizePatch (src, size, mask) {
  const data = new Float32Array(size * size)
  let sum = 0
  let count = 0
  for (let i = 0; i < data.length; i++) {
    if (mask && mask[i] === 0) continue
    sum += src[i]
    count++
  }
  const mean = count ? sum / count : 0
  let norm = 0
  for (let i = 0; i < data.length; i++) {
    if (mask && mask[i] === 0) {
      data[i] = 0
      continue
    }
    const v = src[i] - mean
    data[i] = v
    norm += v * v
  }
  return { data, norm: Math.sqrt(norm), size, mask }
}

/** 在指定 level 的某个位置计算 ZNCC。 */
export function scoreAt (level, patch, px, py) {
  const { size, data: p, norm, mask } = patch
  const w = level.width
  const data = level.data
  if (px < 0 || py < 0 || px + size > level.width || py + size > level.height) return -1
  let sumW = 0
  let sumW2 = 0
  let dot = 0
  let n = 0
  for (let y = 0; y < size; y++) {
    const row = (py + y) * w + px
    const prow = y * size
    for (let x = 0; x < size; x++) {
      const mi = prow + x
      if (mask && mask[mi] === 0) continue
      const v = data[row + x]
      sumW += v
      sumW2 += v * v
      dot += p[mi] * v
      n++
    }
  }
  if (n < 16) return -1
  const varW = sumW2 - (sumW * sumW) / n
  if (varW <= 1e-6) return -1
  const denom = Math.sqrt(varW) * norm
  if (denom <= 1e-6) return -1
  return dot / denom
}

/**
 * 在矩形范围内搜索。
 * @returns {{x:number,y:number,score:number}}
 */
export function searchLevel (level, patch, options = {}) {
  const size = patch.size
  const maxX = level.width - size
  const maxY = level.height - size
  if (maxX < 0 || maxY < 0) return { x: 0, y: 0, score: -1 }
  const stride = Math.max(1, options.stride || 1)
  const x0 = Math.max(0, Math.floor(options.x0 ?? 0))
  const y0 = Math.max(0, Math.floor(options.y0 ?? 0))
  const x1 = Math.min(maxX, Math.ceil(options.x1 ?? maxX))
  const y1 = Math.min(maxY, Math.ceil(options.y1 ?? maxY))
  let best = { x: x0, y: y0, score: -1 }
  for (let y = y0; y <= y1; y += stride) {
    for (let x = x0; x <= x1; x += stride) {
      const s = scoreAt(level, patch, x, y)
      if (s > best.score) best = { x, y, score: s }
    }
  }
  return best
}

export function pickStartLevel (levels, mapDiameter) {
  let index = Math.round(Math.log2(Math.max(1, mapDiameter / 48)))
  index = Math.max(0, Math.min(index, levels.length - 1))
  while (index < levels.length - 1 && mapDiameter / (2 ** index) > 96) index++
  while (index > 0 && mapDiameter / (2 ** index) < 24) index--
  return index
}

/** 绕中心旋转灰度图（双线性），用于适配随镜头旋转的小地图。 */
export function rotateGray (src, w, h, deg) {
  const out = new Uint8Array(w * h)
  const rad = (deg * Math.PI) / 180
  const cos = Math.cos(rad)
  const sin = Math.sin(rad)
  const cx = (w - 1) / 2
  const cy = (h - 1) / 2
  for (let y = 0; y < h; y++) {
    const dy = y - cy
    for (let x = 0; x < w; x++) {
      const dx = x - cx
      const sx = cx + dx * cos - dy * sin
      const sy = cy + dx * sin + dy * cos
      if (sx < 0 || sy < 0 || sx > w - 1.001 || sy > h - 1.001) continue
      const x0 = sx | 0
      const y0 = sy | 0
      const fx = sx - x0
      const fy = sy - y0
      const i0 = y0 * w + x0
      const i1 = i0 + w
      out[y * w + x] = (
        src[i0] * (1 - fx) * (1 - fy) +
        src[i0 + 1] * fx * (1 - fy) +
        src[i1] * (1 - fx) * fy +
        src[i1 + 1] * fx * fy
      ) | 0
    }
  }
  return out
}

function preparePatch (source, mapDiameter, scale) {
  const size = Math.round(mapDiameter / scale)
  if (size < 16 || size > 320) return null
  const src = resizeGray(source.data, source.width, source.height, size, size)
  return normalizePatch(src, size, makeMask(size))
}

function refine (levels, source, mapDiameter, index, centerX, centerY, radius) {
  const level = levels[index]
  if (!level) return null
  const scale = level.scale
  const patch = preparePatch(source, mapDiameter, scale)
  if (!patch) return null
  const size = patch.size
  if (size > level.width || size > level.height) return null
  const bx = Math.round(centerX / scale - size / 2)
  const by = Math.round(centerY / scale - size / 2)
  const best = searchLevel(level, patch, {
    x0: bx - radius, y0: by - radius, x1: bx + radius, y1: by + radius, stride: 1
  })
  if (best.score < 0) return null
  return {
    x: (best.x + size / 2) * scale,
    y: (best.y + size / 2) * scale,
    score: best.score
  }
}

/**
 * 定位入口。
 * @param {object} p
 * @param {{levels:Array}} p.pyramid
 * @param {{data:Uint8Array,width:number,height:number}} p.patchSource 小地图中心区域灰度图
 * @param {number} p.mapDiameter 该区域对应的地图像素直径（视野校准值）
 * @param {number} p.minScore 置信度阈值
 * @param {{x:number,y:number,angle?:number}|null} p.last 上一次定位结果
 */
export function locate ({ pyramid, patchSource, mapDiameter, minScore = 0.55, last = null }) {
  const t0 = Date.now()
  const levels = pyramid && pyramid.levels
  if (!levels || !levels.length) return { ok: false, reason: 'no-pyramid', ms: 0 }
  if (!patchSource || patchSource.width < 8 || patchSource.height < 8) {
    return { ok: false, reason: 'no-patch', ms: Date.now() - t0 }
  }

  const baseSource = {
    data: gradientMagnitude(patchSource.data, patchSource.width, patchSource.height),
    width: patchSource.width,
    height: patchSource.height
  }

  let index = pickStartLevel(levels, mapDiameter)
  while (index < levels.length - 1 && mapDiameter / (2 ** index) > Math.min(levels[index].width, levels[index].height)) {
    index++
  }
  const startLevel = levels[index]
  const scale = startLevel.scale
  const startPatch0 = preparePatch(baseSource, mapDiameter, scale)
  if (!startPatch0 || startPatch0.size > startLevel.width || startPatch0.size > startLevel.height) {
    return { ok: false, reason: 'map-too-small', ms: Date.now() - t0 }
  }
  const startSize = startPatch0.size

  const trackingAngles = last && Number.isFinite(last.angle)
    ? [last.angle - 12, last.angle - 6, last.angle, last.angle + 6, last.angle + 12]
    : null
  const fullAngles = [0, 45, 90, 135, 180, 225, 270, 315]

  function searchWithAngle (angle, local) {
    const rotated = angle
      ? { data: rotateGray(baseSource.data, baseSource.width, baseSource.height, angle), width: baseSource.width, height: baseSource.height }
      : baseSource
    const patch = preparePatch(rotated, mapDiameter, scale)
    if (!patch) return null
    if (local) {
      const r = Math.max(4, Math.round(140 / scale))
      const cx = last.x / scale - startSize / 2
      const cy = last.y / scale - startSize / 2
      const res = searchLevel(startLevel, patch, {
        x0: Math.round(cx) - r,
        y0: Math.round(cy) - r,
        x1: Math.round(cx) + r,
        y1: Math.round(cy) + r,
        stride: 1
      })
      return { ...res, angle }
    }
    const res = searchLevel(startLevel, patch, { stride: 2 })
    const fine = searchLevel(startLevel, patch, {
      x0: res.x - 2, y0: res.y - 2, x1: res.x + 2, y1: res.y + 2, stride: 1
    })
    return { ...(fine.score >= 0 ? fine : res), angle }
  }

  let coarse = null
  if (trackingAngles) {
    for (const angle of trackingAngles) {
      const res = searchWithAngle(angle, true)
      if (res && (!coarse || res.score > coarse.score)) coarse = res
    }
  }
  if (!coarse || coarse.score < 0.3) {
    for (const angle of fullAngles) {
      const res = searchWithAngle(angle, false)
      if (res && (!coarse || res.score > coarse.score)) coarse = res
    }
  }
  if (!coarse || coarse.score < 0) return { ok: false, reason: 'search-failed', ms: Date.now() - t0 }

  const bestAngle = coarse.angle
  const rotated = bestAngle
    ? { data: rotateGray(baseSource.data, baseSource.width, baseSource.height, bestAngle), width: baseSource.width, height: baseSource.height }
    : baseSource

  let centerX = (coarse.x + startSize / 2) * scale
  let centerY = (coarse.y + startSize / 2) * scale
  let bestScore = coarse.score

  for (let step = 1; step <= 2; step++) {
    const target = index - step
    if (target < 0) break
    const res = refine(levels, rotated, mapDiameter, target, centerX, centerY, 4)
    if (!res) break
    centerX = res.x
    centerY = res.y
    bestScore = res.score
  }

  return {
    ok: bestScore >= minScore,
    x: centerX,
    y: centerY,
    score: bestScore,
    angle: bestAngle,
    threshold: minScore,
    ms: Date.now() - t0
  }
}
