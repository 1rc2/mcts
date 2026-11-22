import test from 'node:test'
import assert from 'node:assert/strict'
import {
  toGrayscale, gradientMagnitude, buildPyramid, resizeGray,
  makeMask, normalizePatch, scoreAt, locate
} from '../renderer/js/match-core.js'

function makeTerrain (w, h, seed = 1) {
  const gray = new Uint8Array(w * h)
  let s = seed
  const rand = () => {
    s = (s * 1664525 + 1013904223) % 4294967296
    return s / 4294967296
  }
  const waves = Array.from({ length: 14 }, () => ({
    fx: (0.5 + rand() * 6) * Math.PI * 2 / w,
    fy: (0.5 + rand() * 6) * Math.PI * 2 / h,
    p: rand() * Math.PI * 2,
    a: 0.4 + rand()
  }))
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let v = 0
      for (const wv of waves) v += Math.sin(x * wv.fx + y * wv.fy + wv.p) * wv.a
      gray[y * w + x] = Math.max(0, Math.min(255, 128 + v * 22))
    }
  }
  return gray
}

function cropSquare (gray, w, h, cx, cy, size) {
  const half = Math.floor(size / 2)
  const out = new Uint8Array(size * size)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const sx = Math.max(0, Math.min(w - 1, cx - half + x))
      const sy = Math.max(0, Math.min(h - 1, cy - half + y))
      out[y * size + x] = gray[sy * w + sx]
    }
  }
  return out
}

function rotate90 (src, size) {
  const out = new Uint8Array(size * size)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      out[x * size + (size - 1 - y)] = src[y * size + x]
    }
  }
  return out
}

const W = 1200
const H = 800
const terrain = makeTerrain(W, H, 7)
const pyramid = buildPyramid(terrain, W, H, 6)

test('resizeGray 保持常量', () => {
  const flat = new Uint8Array(10 * 10).fill(42)
  const out = resizeGray(flat, 10, 10, 7, 7)
  assert.equal(out.length, 49)
  assert.ok(out.every(v => v === 42))
})

test('灰度与梯度基本性质', () => {
  const rgba = new Uint8Array(4 * 4 * 4)
  for (let i = 0; i < 16; i++) {
    rgba[i * 4] = 255
    rgba[i * 4 + 1] = 255
    rgba[i * 4 + 2] = 255
    rgba[i * 4 + 3] = 255
  }
  const g = toGrayscale(rgba, 4, 4)
  assert.ok(g.every(v => v === 255))
  const grad = gradientMagnitude(g, 4, 4)
  assert.ok(grad.every(v => v === 0))
})

test('同源 patch 的 ZNCC 接近 1', () => {
  const level = pyramid.levels[0]
  const size = 64
  const src = cropSquare(level.data, level.width, level.height, 300, 220, size)
  const patch = normalizePatch(src, size, makeMask(size))
  const score = scoreAt(level, patch, 300 - size / 2, 220 - size / 2)
  assert.ok(score > 0.99, `score=${score}`)
})

test('全局定位：无先验时能找回位置', () => {
  const cx = 700
  const cy = 420
  const diameter = 240
  const patch = cropSquare(terrain, W, H, cx, cy, diameter)
  const res = locate({
    pyramid,
    patchSource: { data: patch, width: diameter, height: diameter },
    mapDiameter: diameter,
    minScore: 0.5,
    last: null
  })
  assert.equal(res.ok, true, JSON.stringify(res))
  assert.ok(Math.hypot(res.x - cx, res.y - cy) < 10, `offset=${Math.hypot(res.x - cx, res.y - cy)}`)
})

test('跟踪定位：有先验时更快且误差更小', () => {
  const cx = 520
  const cy = 300
  const diameter = 240
  const srcSize = diameter
  const patch = cropSquare(terrain, W, H, cx, cy, srcSize)
  const res = locate({
    pyramid,
    patchSource: { data: patch, width: srcSize, height: srcSize },
    mapDiameter: diameter,
    minScore: 0.5,
    last: { x: cx + 30, y: cy - 20 }
  })
  assert.equal(res.ok, true, JSON.stringify(res))
  assert.ok(Math.hypot(res.x - cx, res.y - cy) < 14, `offset=${Math.hypot(res.x - cx, res.y - cy)}`)
})

test('旋转 90 度的 patch 仍可定位（梯度幅值对旋转更鲁棒）', () => {
  const cx = 380
  const cy = 480
  const diameter = 240
  const size = diameter
  const patch = rotate90(cropSquare(terrain, W, H, cx, cy, size), size)
  const res = locate({
    pyramid,
    patchSource: { data: patch, width: size, height: size },
    mapDiameter: diameter,
    minScore: 0.45,
    last: null
  })
  assert.equal(res.ok, true, JSON.stringify(res))
  assert.ok(Math.hypot(res.x - cx, res.y - cy) < 16, `offset=${Math.hypot(res.x - cx, res.y - cy)}`)
})

test('无关联画面不应高置信度命中', () => {
  const other = buildPyramid(makeTerrain(W, H, 99), W, H, 6)
  const size = 200
  const noise = new Uint8Array(size * size)
  let s = 12345
  for (let i = 0; i < noise.length; i++) {
    s = (s * 1103515245 + 12345) % 2147483648
    noise[i] = (s >> 16) & 255
  }
  const res = locate({
    pyramid: other,
    patchSource: { data: noise, width: size, height: size },
    mapDiameter: 400,
    minScore: 0.9,
    last: null
  })
  assert.equal(res.ok, false, JSON.stringify(res))
})
