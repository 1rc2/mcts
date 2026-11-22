import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'

import { TYPES, typeMeta, clamp, formatDistance, debounce } from '../renderer/js/meta.js'

const require = createRequire(import.meta.url)
const { JsonFile, deepMerge } = require('../main/json-file.js')

test('clamp 限制在区间内', () => {
  assert.equal(clamp(-3, 0, 10), 0)
  assert.equal(clamp(12, 0, 10), 10)
  assert.equal(clamp(4.5, 0, 10), 4.5)
})

test('typeMeta 对未知类型回退到自定义', () => {
  assert.equal(typeMeta('chest').label, '宝箱')
  assert.equal(typeMeta('unknown-type'), TYPES.custom)
})

test('formatDistance 输出米与千米', () => {
  assert.equal(formatDistance(0), '0 m')
  assert.equal(formatDistance(123.4), '123 m')
  assert.equal(formatDistance(1500), '1.50 km')
  assert.equal(formatDistance(NaN), '')
})

test('debounce 合并连续调用', async () => {
  let count = 0
  const fn = debounce(() => { count++ }, 20)
  fn(); fn(); fn()
  await new Promise(r => setTimeout(r, 50))
  assert.equal(count, 1)
  fn()
  fn.flush()
  assert.equal(count, 2)
})

test('deepMerge 深层合并对象并整体替换数组', () => {
  const base = { a: 1, nested: { x: 1, y: 2 }, list: [1, 2] }
  const out = deepMerge(base, { nested: { y: 3 }, list: [9] })
  assert.deepEqual(out, { a: 1, nested: { x: 1, y: 3 }, list: [9] })
  assert.equal(base.nested.y, 2, '不应修改原对象')
})

test('JsonFile 读取损坏文件时回退默认值并可原子写入', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wuwa-json-'))
  const file = path.join(dir, 'state.json')
  const defaults = { schema: 1, nested: { a: 1 }, list: [] }
  fs.writeFileSync(file, '{ not-json', 'utf8')

  const store = new JsonFile(file, defaults)
  assert.deepEqual(store.get(), defaults)

  store.patch({ nested: { a: 2 } })
  const reread = new JsonFile(file, defaults)
  assert.equal(reread.get().nested.a, 2)
  assert.equal(reread.get().schema, 1)

  fs.rmSync(dir, { recursive: true, force: true })
})
