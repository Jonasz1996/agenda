// Agenda 2.0: FullCalendar, bewerken in een eigen venster, afvalkalender uit recycleapp.be.
(() => {
  const body = document.body
  const csrf = body.dataset.csrf
  const canEdit = !!body.dataset.user
  const $ = (s) => document.querySelector(s)

  const COLORS = { afval: '#2e7d32', werk: '#1565c0', 'privé': '#8e24aa' }
  const WASTE = [
    [/pmd|blauw/i, '#1e88e5'], [/gft|groen/i, '#43a047'], [/papier|karton/i, '#f9a825'],
    [/glas/i, '#00897b'], [/rest|zwart|huisvuil/i, '#424242'], [/snoei|tuin/i, '#6d4c41'],
  ]
  const color = (e) => {
    for (const [re, c] of WASTE) if (re.test(e.title)) return c
    return COLORS[e.category] || '#5c6bc0'
  }

  async function api(path, data) {
    const r = await fetch(path, {
      method: data ? 'POST' : 'GET',
      headers: data ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf } : {},
      body: data ? JSON.stringify(data) : undefined,
      credentials: 'same-origin',
    })
    let out = null
    try { out = await r.json() } catch { /* geen JSON */ }
    if (!r.ok) throw new Error((out && out.error) || `HTTP ${r.status}`)
    return out
  }

  // --- datum en tijd voor <input type=datetime-local> ---------------------------------------------------
  const pad = (n) => String(n).padStart(2, '0')
  const localInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  const dayOnly = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`

  // --- kalender -------------------------------------------------------------------------------------------
  const cal = new FullCalendar.Calendar($('#calendar'), {
    locale: 'nl',
    timeZone: 'local',
    firstDay: 1,
    height: 'auto',
    initialView: window.innerWidth < 700 ? 'listMonth' : 'dayGridMonth',
    headerToolbar: { left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,listMonth' },
    nowIndicator: true,
    editable: canEdit,
    selectable: canEdit,
    events: (info, ok, fail) => {
      api(`/api/events?start=${encodeURIComponent(info.startStr)}&end=${encodeURIComponent(info.endStr)}`)
        .then((list) => ok(list.map((e) => ({ ...e, color: color(e), extendedProps: { category: e.category, notes: e.notes, source: e.source } }))))
        .catch(fail)
    },
    dateClick: (info) => canEdit && openEvent(null, info.date, info.allDay),
    select: (info) => canEdit && openEvent(null, info.start, info.allDay, info.end),
    eventClick: (info) => { info.jsEvent.preventDefault(); if (canEdit) openEvent(info.event) },
    eventDrop: (info) => move(info),
    eventResize: (info) => move(info),
  })
  cal.render()

  async function move(info) {
    const e = info.event
    try {
      await api('/api/update_event', {
        id: Number(e.id), allDay: e.allDay,
        start: e.allDay ? dayOnly(e.start) : e.start.toISOString(),
        end: e.end ? (e.allDay ? dayOnly(e.end) : e.end.toISOString()) : null,
      })
      cal.refetchEvents(); loadNext()
    } catch (err) { info.revert(); alertBox(err.message) }
  }

  // --- volgende afspraken bovenaan -----------------------------------------------------------------------
  async function loadNext() {
    const now = new Date()
    const until = new Date(now.getTime() + 8 * 86400000)
    try {
      const list = await api(`/api/events?start=${encodeURIComponent(now.toISOString())}&end=${encodeURIComponent(until.toISOString())}`)
      const fmt = new Intl.DateTimeFormat('nl-BE', { weekday: 'short', day: 'numeric', month: 'numeric' })
      const tfmt = new Intl.DateTimeFormat('nl-BE', { hour: '2-digit', minute: '2-digit' })
      const el = $('#next')
      el.replaceChildren()
      if (!list.length) { el.textContent = 'Niets gepland de komende week.'; return }
      for (const e of list.slice(0, 6)) {
        const chip = document.createElement('span')
        chip.className = 'chip'
        chip.style.setProperty('--c', color(e))
        const d = new Date(e.allDay ? e.start + 'T00:00' : e.start)
        const today = new Date(); today.setHours(0, 0, 0, 0)
        const diff = Math.round((new Date(d).setHours(0, 0, 0, 0) - today) / 86400000)
        const when = diff === 0 ? 'vandaag' : diff === 1 ? 'morgen' : fmt.format(d)
        chip.textContent = `${when}${e.allDay ? '' : ' ' + tfmt.format(d)} · ${e.title}`
        el.append(chip)
      }
    } catch { /* stil: de kalender toont het ook */ }
  }
  loadNext()

  // --- afspraak bewerken ----------------------------------------------------------------------------------
  const dlg = $('#ev')
  const form = $('#ev-form')
  let current = null

  function openEvent(ev, date, allDay, endDate) {
    current = ev
    form.reset()
    $('#ev-err').textContent = ''
    $('#ev-title').textContent = ev ? 'Afspraak bewerken' : 'Nieuwe afspraak'
    $('#ev-del').hidden = !ev
    const start = ev ? ev.start : date
    let end = ev ? ev.end : endDate
    const ad = ev ? ev.allDay : allDay
    if (!ev && !ad) { start.setHours(start.getHours() || 9) }
    if (!end) end = new Date(start.getTime() + (ad ? 86400000 : 3600000))
    if (ad) end = new Date(end.getTime() - 60000) // einde van de vorige dag tonen
    form.title.value = ev ? ev.title : ''
    form.allDay.checked = ad
    form.start.value = localInput(start)
    form.end.value = localInput(end)
    form.category.value = ev ? (ev.extendedProps.category || '') : ''
    form.notes.value = ev ? (ev.extendedProps.notes || '') : ''
    $('#ev-src').textContent = ev && ev.extendedProps.source === 'recycle'
      ? 'Uit recycleapp.be. Bewerk je hem, dan past de dagelijkse sync hem niet meer aan; verwijderen onthoudt de sync ook.'
      : ''
    dlg.showModal()
    form.title.focus()
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault()
    const ad = form.allDay.checked
    const s = new Date(form.start.value)
    const en = form.end.value ? new Date(form.end.value) : null
    const data = {
      title: form.title.value, allDay: ad, category: form.category.value, notes: form.notes.value,
      start: ad ? dayOnly(s) : s.toISOString(),
      end: en ? (ad ? dayOnly(new Date(en.getTime() + 86400000)) : en.toISOString()) : null,
    }
    try {
      if (current) await api('/api/update_event', { id: Number(current.id), ...data })
      else await api('/api/add_event', data)
      dlg.close(); cal.refetchEvents(); loadNext()
    } catch (err) { $('#ev-err').textContent = err.message }
  })
  $('#ev-cancel').addEventListener('click', () => dlg.close())
  $('#ev-del').addEventListener('click', async () => {
    if (!current) return
    if ($('#ev-del').dataset.sure !== '1') {
      $('#ev-del').dataset.sure = '1'; $('#ev-del').textContent = 'Zeker?'
      setTimeout(() => { $('#ev-del').dataset.sure = ''; $('#ev-del').textContent = 'Verwijderen' }, 3000)
      return
    }
    try {
      await api('/api/delete_event', { id: Number(current.id) })
      dlg.close(); cal.refetchEvents(); loadNext()
    } catch (err) { $('#ev-err').textContent = err.message }
  })

  // --- afvalkalender --------------------------------------------------------------------------------------
  const rc = $('#rc')
  const rcForm = $('#rc-form')
  const showStatus = (st) => {
    const parts = []
    if (st.synced) parts.push(`Laatst opgehaald: ${new Date(st.synced).toLocaleString('nl-BE')}`)
    if (st.result) parts.push(`(${st.result})`)
    if (!st.configured) parts.push('Nog geen adres ingesteld.')
    $('#rc-status').textContent = parts.join(' ')
    $('#rc-err').textContent = st.error || ''
  }
  $('#recycle-open')?.addEventListener('click', async () => {
    try {
      const st = await api('/api/recycle')
      rcForm.postcode.value = st.postcode || ''
      rcForm.street.value = st.street || ''
      rcForm.number.value = st.number || ''
      showStatus(st)
    } catch (err) { $('#rc-err').textContent = err.message }
    rc.showModal()
  })
  $('#rc-cancel').addEventListener('click', () => rc.close())
  async function syncNow() {
    $('#rc-err').textContent = ''
    $('#rc-status').textContent = 'Bezig met ophalen…'
    try {
      const r = await api('/api/recycle/sync', {})
      showStatus(r.status); cal.refetchEvents(); loadNext()
    } catch (err) {
      $('#rc-err').textContent = err.message
      $('#rc-status').textContent = ''
    }
  }
  $('#rc-sync').addEventListener('click', syncNow)
  rcForm.addEventListener('submit', async (e) => {
    e.preventDefault()
    try {
      await api('/api/recycle', { postcode: rcForm.postcode.value, street: rcForm.street.value, number: rcForm.number.value })
      await syncNow()
    } catch (err) { $('#rc-err').textContent = err.message }
  })

  function alertBox(msg) {
    const el = $('#next')
    const p = document.createElement('span')
    p.className = 'chip err'
    p.textContent = msg
    el.prepend(p)
    setTimeout(() => p.remove(), 6000)
  }
})()
