import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..')
const readJson = (rel) => JSON.parse(fs.readFileSync(path.join(ROOT, rel), 'utf8'))

const ALLOWED_TYPES = new Set(['chest', 'anchor', 'material', 'echo', 'enemy', 'puzzle', 'quest', 'npc', 'custom'])

test('地图元数据合法', () => {
  const map = readJson('data/map.json')
  assert.ok(map.width > 0 && map.height > 0)
  assert.ok(map.worldWidthMeters > 0)
  assert.ok(Array.isArray(map.regions) && map.regions.length > 0)
  for (const r of map.regions) {
    assert.ok(Number.isFinite(r.u) && r.u >= 0 && r.u <= 1)
    assert.ok(Number.isFinite(r.v) && r.v >= 0 && r.v <= 1)
  }
})

test('内置点位：坐标归一化、id 唯一、类型合法', () => {
  const data = readJson('data/markers.json')
  const markers = data.markers
  assert.ok(Array.isArray(markers) && markers.length > 0)
  const seen = new Set()
  for (const m of markers) {
    assert.ok(m.id && !seen.has(m.id), `重复或缺失 id: ${m.id}`)
    seen.add(m.id)
    assert.ok(ALLOWED_TYPES.has(m.type), `非法类型: ${m.type}`)
    assert.ok(Number.isFinite(m.u) && m.u >= 0 && m.u <= 1, `u 越界: ${m.id}`)
    assert.ok(Number.isFinite(m.v) && m.v >= 0 && m.v <= 1, `v 越界: ${m.id}`)
    assert.ok(String(m.name || '').length > 0)
  }
})

test('内置路线：点位坐标合法，引用的 markerId 存在', () => {
  const routes = readJson('data/routes.json').routes
  const ids = new Set(readJson('data/markers.json').markers.map(m => m.id))
  assert.ok(Array.isArray(routes) && routes.length > 0)
  for (const r of routes) {
    assert.ok(r.id && r.name)
    assert.ok(Array.isArray(r.points) && r.points.length > 0)
    for (const p of r.points) {
      assert.ok(Number.isFinite(p.u) && p.u >= 0 && p.u <= 1)
      assert.ok(Number.isFinite(p.v) && p.v >= 0 && p.v <= 1)
      if (p.markerId) assert.ok(ids.has(p.markerId), `路线 ${r.id} 引用了不存在的点位 ${p.markerId}`)
    }
  }
})

test('图标资源存在且非空', () => {
  for (const rel of ['assets/icon.png', 'assets/tray.png', 'assets/maps/huanglong.jpg']) {
    const stat = fs.statSync(path.join(ROOT, rel))
    assert.ok(stat.size > 1000, `${rel} 文件过小`)
  }
})
