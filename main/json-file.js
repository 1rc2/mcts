'use strict'

const fs = require('node:fs')
const path = require('node:path')

function isPlainObject (v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function deepMerge (base, patch) {
  const out = Array.isArray(base) ? base.slice() : { ...base }
  for (const [k, v] of Object.entries(patch || {})) {
    if (isPlainObject(v) && isPlainObject(out[k])) out[k] = deepMerge(out[k], v)
    else out[k] = v
  }
  return out
}

/** 极简 JSON 持久化：原子写入，读取失败时回退默认值。 */
class JsonFile {
  constructor (file, defaults) {
    this.file = file
    this.defaults = defaults
    this.data = this._read()
  }

  _read () {
    try {
      const raw = fs.readFileSync(this.file, 'utf8')
      return deepMerge(this.defaults, JSON.parse(raw))
    } catch {
      return structuredClone(this.defaults)
    }
  }

  get () {
    return this.data
  }

  set (next) {
    this.data = next
    this._write()
    return this.data
  }

  patch (partial) {
    this.data = deepMerge(this.data, partial)
    this._write()
    return this.data
  }

  _write () {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true })
      const tmp = `${this.file}.tmp`
      fs.writeFileSync(tmp, JSON.stringify(this.data, null, 1), 'utf8')
      fs.renameSync(tmp, this.file)
    } catch (err) {
      console.error('[json-file] write failed', this.file, err)
    }
  }
}

module.exports = { JsonFile, deepMerge }
