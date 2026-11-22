(function () {
  const sel = document.getElementById('sel')
  const tag = sel.querySelector('.tag')
  let startX = 0
  let startY = 0
  let dragging = false

  function rectFrom (x, y) {
    return {
      x: Math.min(startX, x),
      y: Math.min(startY, y),
      width: Math.abs(x - startX),
      height: Math.abs(y - startY)
    }
  }

  function paint (rect) {
    if (rect.width < 2 || rect.height < 2) {
      sel.style.display = 'none'
      return
    }
    sel.style.display = 'block'
    sel.style.left = rect.x + 'px'
    sel.style.top = rect.y + 'px'
    sel.style.width = rect.width + 'px'
    sel.style.height = rect.height + 'px'
    tag.textContent = Math.round(rect.width) + ' × ' + Math.round(rect.height)
    tag.style.left = '0'
    tag.style.top = rect.y < 30 ? '4px' : '-26px'
  }

  window.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return
    dragging = true
    startX = e.clientX
    startY = e.clientY
    paint({ x: startX, y: startY, width: 0, height: 0 })
  })

  window.addEventListener('mousemove', (e) => {
    if (!dragging) return
    paint(rectFrom(e.clientX, e.clientY))
  })

  window.addEventListener('mouseup', (e) => {
    if (!dragging) return
    dragging = false
    const rect = rectFrom(e.clientX, e.clientY)
    if (rect.width < 24 || rect.height < 24) {
      sel.style.display = 'none'
      return
    }
    if (window.wuwa && window.wuwa.picker) window.wuwa.picker.done(rect)
  })

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && window.wuwa && window.wuwa.picker) window.wuwa.picker.cancel()
  })

  window.addEventListener('contextmenu', (e) => {
    e.preventDefault()
    if (window.wuwa && window.wuwa.picker) window.wuwa.picker.cancel()
  })
})()
