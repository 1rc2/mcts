import { toGrayscale } from './match-core.js'

const PATCH_WORK_SIZE = 220

/**
 * 识图定位控制器：
 * 屏幕截图 -> 中心区域灰度 -> Worker 内金字塔匹配 -> 地图坐标。
 * 全程只读取屏幕像素，不接触游戏进程。
 */
export class RecognitionController {
  constructor ({ onStatus, onPosition, onFail }) {
    this.onStatus = onStatus || (() => {})
    this.onPosition = onPosition || (() => {})
    this.onFail = onFail || (() => {})

    this.worker = null
    this.ready = false
    this.initializing = null
    this.enabled = false
    this.busy = false
    this.timer = null
    this.seq = 0
    this.pending = new Map()
    this.failures = 0
    this.settings = null
    this.patchCanvas = null
    this.mapKey = ''
  }

  _ensureWorker () {
    if (this.worker) return
    this.worker = new Worker(new URL('./matcher.worker.js', import.meta.url), { type: 'module' })
    this.worker.onmessage = (event) => {
      const msg = event.data
      if (msg.type === 'ready') {
        this.ready = true
        const wait = this.pending.get('init')
        if (wait) {
          this.pending.delete('init')
          wait.resolve(msg)
        }
        return
      }
      if (msg.type === 'result') {
        const wait = this.pending.get(msg.id)
        if (wait) {
          this.pending.delete(msg.id)
          wait.resolve(msg)
        }
      }
    }
    this.worker.onerror = (err) => {
      console.error('[recognition] worker error', err)
      for (const wait of this.pending.values()) wait.reject(new Error('识图模块异常'))
      this.pending.clear()
      this.ready = false
    }
  }

  /** 构建地图匹配金字塔（较耗时，首次启用识图时调用）。 */
  async buildMapPyramid (image, mapInfo) {
    const key = `${mapInfo.imageUrl}|${mapInfo.width}x${mapInfo.height}`
    if (this.ready && this.mapKey === key) return { cached: true }
    this.mapKey = key
    this.ready = false
    this.worker?.terminate()
    this.worker = null
    this._ensureWorker()
    this.onStatus({ state: 'init', message: '正在建立地图索引…' })

    const width = mapInfo.width
    const height = mapInfo.height
    const canvas = new OffscreenCanvas(width, height)
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    ctx.drawImage(image, 0, 0, width, height)
    const { data } = ctx.getImageData(0, 0, width, height)
    const gray = toGrayscale(data, width, height)

    const done = new Promise((resolve, reject) => {
      this.pending.set('init', { resolve, reject })
      setTimeout(() => {
        if (this.pending.has('init')) {
          this.pending.delete('init')
          reject(new Error('地图索引建立超时'))
        }
      }, 20000)
    })
    this.worker.postMessage({ type: 'init', width, height, buffer: gray.buffer }, [gray.buffer])
    await done
    this.onStatus({ state: 'ready', message: '识图已就绪' })
    return { cached: false }
  }

  async start (settings) {
    this.settings = settings
    this.enabled = true
    this.failures = 0
    this.onStatus({ state: 'starting', message: '启动识图…' })
    this._schedule(0)
  }

  stop (reason = '') {
    this.enabled = false
    this.busy = false
    clearTimeout(this.timer)
    this.timer = null
    this.onStatus({ state: 'off', message: reason || '识图定位已关闭' })
  }

  resetTracker () {
    this.worker?.postMessage({ type: 'reset' })
  }

  setPosition (x, y) {
    this.worker?.postMessage({ type: 'setPosition', x, y, angle: 0 })
  }

  _schedule (delay) {
    if (!this.enabled) return
    clearTimeout(this.timer)
    this.timer = setTimeout(() => this._tick(), delay)
  }

  async _tick () {
    if (!this.enabled) return
    if (this.busy) {
      this._schedule(this.settings?.intervalMs || 1500)
      return
    }
    this.busy = true
    const s = this.settings || {}
    try {
      if (!this.ready) {
        this.onStatus({ state: 'fail', message: '地图索引尚未就绪，正在重试' })
        this.busy = false
        this._schedule(1200)
        return
      }

      const shot = await window.wuwa.invoke('capture:region', {
        displayId: s.displayId ?? null,
        region: s.region
      })
      const patch = await this._extractPatch(shot)
      const res = await this._postLocate(patch, s)
      if (!res || !res.ok) {
        this.failures++
        this.onStatus({
          state: 'fail',
          message: res && res.reason === 'map-too-small' ? '地图尺寸与视野不匹配' : '未识别到小地图',
          score: res && res.score,
          ms: res && res.ms
        })
        if (this.failures >= 3) {
          this.stop('连续识别失败，已切换手动定位')
          this.onFail()
          return
        }
      } else {
        this.failures = 0
        this.onStatus({
          state: 'ok',
          message: '识图定位正常',
          score: res.score,
          ms: res.ms,
          angle: res.angle
        })
        this.onPosition({
          x: res.x,
          y: res.y,
          score: res.score,
          angle: res.angle,
          ts: Date.now()
        })
      }
    } catch (err) {
      this.failures++
      this.onStatus({ state: 'fail', message: err.message || '识图失败' })
      if (this.failures >= 3) {
        this.stop('连续识别失败，已切换手动定位')
        this.onFail()
        return
      }
    } finally {
      this.busy = false
      this._schedule(this.settings?.intervalMs || 1500)
    }
  }

  async _extractPatch (shot) {
    const bitmap = await createImageBitmap(await (await fetch(shot.dataURL)).blob())
    if (!this.patchCanvas) {
      this.patchCanvas = new OffscreenCanvas(PATCH_WORK_SIZE, PATCH_WORK_SIZE)
    }
    const ctx = this.patchCanvas.getContext('2d', { willReadFrequently: true })
    const inset = 0.1
    const sx = shot.width * inset
    const sy = shot.height * inset
    const sw = shot.width * (1 - inset * 2)
    const sh = shot.height * (1 - inset * 2)
    ctx.drawImage(bitmap, sx, sy, sw, sh, 0, 0, PATCH_WORK_SIZE, PATCH_WORK_SIZE)
    bitmap.close?.()
    const { data } = ctx.getImageData(0, 0, PATCH_WORK_SIZE, PATCH_WORK_SIZE)
    return toGrayscale(data, PATCH_WORK_SIZE, PATCH_WORK_SIZE)
  }

  _postLocate (gray, s) {
    const id = `loc-${++this.seq}`
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error('识图超时'))
        }
      }, 12000)
    })
    this.worker.postMessage({
      type: 'locate',
      id,
      width: PATCH_WORK_SIZE,
      height: PATCH_WORK_SIZE,
      buffer: gray.buffer,
      mapDiameter: s.mapDiameter || 720,
      minScore: s.minScore ?? 0.55
    }, [gray.buffer])
    return promise
  }

  dispose () {
    this.stop()
    this.worker?.terminate()
    this.worker = null
  }
}
