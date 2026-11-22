'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { safeStorage } = require('electron')

/**
 * 凭证只写入本机 userData 目录，并使用操作系统提供的密钥链加密：
 * Windows -> DPAPI；若系统加密不可用则拒绝落盘（绝不明文保存）。
 */
class SecureStore {
  constructor (userDataDir) {
    this.file = path.join(userDataDir, 'credentials.bin')
  }

  encryptionAvailable () {
    try {
      return safeStorage.isEncryptionAvailable()
    } catch {
      return false
    }
  }

  save (payload) {
    if (!this.encryptionAvailable()) {
      throw new Error('当前系统加密服务不可用，出于安全考虑不会保存登录凭证')
    }
    const buf = safeStorage.encryptString(JSON.stringify(payload))
    fs.mkdirSync(path.dirname(this.file), { recursive: true })
    fs.writeFileSync(this.file, buf, { mode: 0o600 })
  }

  load () {
    try {
      if (!fs.existsSync(this.file)) return null
      if (!this.encryptionAvailable()) return null
      const raw = fs.readFileSync(this.file)
      const json = safeStorage.decryptString(raw)
      const parsed = JSON.parse(json)
      return parsed && typeof parsed === 'object' ? parsed : null
    } catch {
      this.clear()
      return null
    }
  }

  clear () {
    try {
      fs.rmSync(this.file, { force: true })
    } catch (err) {
      console.error('[secure-store] clear failed', err)
    }
  }

  exists () {
    return fs.existsSync(this.file)
  }
}

module.exports = { SecureStore }
