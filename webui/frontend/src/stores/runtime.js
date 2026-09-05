import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createSSE } from '@/api/request'
import { getRunLogs, listRuns } from '@/api/register'
import { useStatsStore } from './stats'

let _logId = 0
const MAX_LOGS = 2000
const EVENT_PREFIX = '__EVENT__:'

function classify(line) {
  const l = (line || '').toLowerCase()
  if (l.includes('error') || l.includes('失败') || l.includes('拒绝')) return 'err'
  if (l.includes('warning') || l.includes('warn')) return 'warn'
  if (l.includes('成功') || l.includes('完成') || l.includes('命中') || l.includes('ok')) return 'ok'
  return ''
}

function isReauth(channel) {
  return channel === 'reauth'
}

// 运行时状态：注册/全自动一套日志，重新授权另一套。
// 放在 store 里是为了「切换菜单页面时，后台任务和日志不中断」。
export const useRuntimeStore = defineStore('runtime', () => {
  const logs = ref([])            // 单次注册 + 全自动
  const reauthLogs = ref([])      // 只给注册结果弹窗
  const autoStatus = ref({ state: 'stopped', registered_ok: 0, registered_fail: 0 })
  const banner = ref('')
  const lastRunResult = ref(null)
  const lastReauthResult = ref(null)
  const reauthBatch = ref({ total: 0, ok: 0, fail: 0, current: '', done: false })
  const dataVersion = ref(0)
  const runningSingle = ref(false)
  const runningReauth = ref(false)
  const reauthRestoreOpen = ref(false)

  const runStreams = new Map()    // runId -> info（见 streamRun）
  let eventsEs = null
  let eventsTimer = 0
  let restored = false
  let reauthBatching = false
  let visibilityBound = false

  function logList(channel) {
    return isReauth(channel) ? reauthLogs : logs
  }
  function resultRef(channel) {
    return isReauth(channel) ? lastReauthResult : lastRunResult
  }

  function addLog(text, kind, channel = 'register') {
    const arr = logList(channel)
    arr.value.push({ id: ++_logId, text, kind: kind ?? classify(text) })
    if (arr.value.length > MAX_LOGS) arr.value.splice(0, arr.value.length - MAX_LOGS)
  }
  function clearLogs() { logs.value = [] }
  function clearReauthLogs() { reauthLogs.value = [] }
  function bumpData() { dataVersion.value++ }
  function dismissBanner() { banner.value = '' }
  function ackReauthRestore() { reauthRestoreOpen.value = false }

  function syncRunning() {
    let reg = 0
    let rea = 0
    for (const info of runStreams.values()) {
      if (info.channel === 'reauth') rea += 1
      else reg += 1
    }
    runningSingle.value = reg > 0
    runningReauth.value = rea > 0 || reauthBatching
  }

  function beginReauthBatch(total = 0, current = '') {
    reauthBatching = true
    runningReauth.value = true
    reauthBatch.value = {
      total: Number(total) || 0,
      ok: 0,
      fail: 0,
      current: current || '',
      done: false,
    }
    lastReauthResult.value = null
  }
  function applyReauthState(d) {
    if (!d || typeof d !== 'object') return
    const total = Number(d.total) || 0
    const active = !!d.active
    // 本地刚点开始、服务端还没回包时，空闲快照不要把进度条抹掉。
    if (!active && !total && (reauthBatching || reauthBatch.value.total)) return
    reauthBatch.value = {
      total,
      ok: Number(d.ok) || 0,
      fail: Number(d.fail) || 0,
      current: d.current || '',
      done: !active && total > 0,
    }
    if (active) {
      reauthBatching = true
      runningReauth.value = true
    } else {
      reauthBatching = false
      syncRunning()
    }
  }
  function endReauthBatch() {
    reauthBatching = false
    if (reauthBatch.value.total) {
      reauthBatch.value = { ...reauthBatch.value, done: true, current: '' }
    }
    syncRunning()
  }
  function abortReauthBatch() {
    reauthBatching = false
    reauthBatch.value = { total: 0, ok: 0, fail: 0, current: '', done: false }
    syncRunning()
  }

  function applyStatus(d, { silentPhase = false, channel = 'register' } = {}) {
    const result = resultRef(channel)
    if (d.kind === 'done') {
      // 重新授权按整批计数，单号 done 不写成「整批完成」。
      if (d.reauth || isReauth(channel)) return
      result.value = {
        email: d.email,
        password: d.password || '',
        totp_secret: d.totp_secret || '',
        access_token_len: d.access_token_len,
        partial: d.partial,
        reauth: d.reauth,
        ok: d.ok,
        fail: d.fail,
      }
      addLog(
        `注册完成: ${d.email}${d.password ? ' / ' + d.password : ''}`
        + ` (access_token=${d.access_token_len}${d.partial ? ', 部分凭证' : ''})`,
        'ok',
        channel,
      )
    } else if (d.kind === 'error') {
      if (isReauth(channel)) {
        addLog('错误: ' + d.message, 'err', channel)
        return
      }
      result.value = { email: d.email, error: d.message }
      addLog('错误: ' + d.message, 'err', channel)
    } else if (d.kind === 'phase' && !silentPhase) {
      addLog(`phase=${d.phase} email=${d.email}`, 'evt', channel)
    }
  }

  function applyHistoryLine(line, channel = 'register') {
    if ((line || '').startsWith(EVENT_PREFIX)) {
      try {
        applyStatus(JSON.parse(line.slice(EVENT_PREFIX.length)), { silentPhase: true, channel })
      } catch (_) {}
      return
    }
    addLog(line, undefined, channel)
  }

  function streamRun(runId, offset = 0, opts = {}) {
    const channel = isReauth(opts.channel) ? 'reauth' : 'register'
    if (!runId) return Promise.resolve()
    const existing = runStreams.get(runId)
    if (existing) return existing.done || Promise.resolve()

    let resolveDone = () => {}
    const done = new Promise((resolve) => { resolveDone = resolve })
    // pos 记在 info 上而不是闭包里：标签页切到后台会断开连接省出 HTTP 槽位，
    // 回到前台按同一个 offset 续订，日志不会重复也不会丢。
    const info = {
      es: null, channel, done,
      pos: offset || 0, ended: false, retries: 0, suspended: false, attach: null,
    }
    runStreams.set(runId, info)
    if (channel === 'reauth') runningReauth.value = true
    else runningSingle.value = true

    const finish = () => {
      if (info.ended) return
      info.ended = true
      runStreams.delete(runId)
      syncRunning()
      useStatsStore().refresh()
      bumpData()
      resolveDone()
    }

    const attach = () => {
      if (info.ended || info.suspended) return
      if (channel === 'reauth') runningReauth.value = true
      else runningSingle.value = true
      const es = createSSE(`/api/runs/${encodeURIComponent(runId)}/stream?offset=${info.pos}`, {
        log: (e) => {
          try {
            const d = JSON.parse(e.data)
            if (d.line) addLog(d.line, undefined, channel)
            if (typeof d.offset === 'number') info.pos = d.offset
          } catch (_) {}
        },
        status: (e) => {
          try { applyStatus(JSON.parse(e.data), { channel }) } catch (_) {}
        },
        end: () => {
          try { es.close() } catch (_) {}
          finish()
        },
      }, () => {
        try { es.close() } catch (_) {}
        if (info.suspended) return
        if (!info.ended && info.retries < 8) {
          info.retries += 1
          setTimeout(attach, 1000)
          return
        }
        finish()
      })
      info.es = es
    }
    info.attach = attach
    attach()
    return done
  }

  async function hydrateRun(run, channel = 'register') {
    const hist = await getRunLogs(run.run_id, { tail: 2000 })
    const tag = hist.alive ? '进行中' : (hist.status === 'done' ? '已完成' : '已结束')
    addLog(`[client] 恢复日志 ${hist.email || run.email || ''} (run=${run.run_id}, ${tag})`, 'evt', channel)
    for (const line of hist.lines || []) applyHistoryLine(line, channel)
    if (hist.alive) {
      window.setTimeout(() => {
        streamRun(run.run_id, hist.next_offset || 0, { channel })
      }, 400)
    }
    return hist
  }

  async function restoreChannel(channel) {
    const kind = channel
    const running = await listRuns(20, { status: 'running', kind })
    let items = [...(running.items || [])]
    if (!items.length) {
      const recent = await listRuns(1, { kind })
      items = (recent.items || []).slice(0, 1)
    }
    items.sort((a, b) => (a.started_at || 0) - (b.started_at || 0))
    let anyAlive = false
    for (const run of items) {
      const hist = await hydrateRun(run, channel)
      if (hist.alive) anyAlive = true
    }
    return anyAlive
  }

  async function restoreLogs() {
    if (restored) return
    restored = true
    try {
      await restoreChannel('register')
    } catch (_) {}
    try {
      const reauthAlive = await restoreChannel('reauth')
      if (reauthAlive) {
        reauthRestoreOpen.value = true
        reauthBatching = true
        runningReauth.value = true
      }
    } catch (_) {}
  }

  // auto + reauth 合成一条 /api/events。原来两条分开，加上 run 日志流就是每页 3 条，
  // 开三个标签页刚好把浏览器的 6 条连接吃干净（实测刷新 60s 都出不来页面）。
  function connectEvents() {
    if (eventsTimer) { clearTimeout(eventsTimer); eventsTimer = 0 }
    if (eventsEs) { try { eventsEs.close() } catch (_) {} eventsEs = null }
    if (typeof document !== 'undefined' && document.hidden) return
    const es = createSSE('/api/events', {
      auto_state: (e) => {
        try { autoStatus.value = JSON.parse(e.data) } catch (_) {}
      },
      auto_run_started: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] 开始注册 ${d.email} (run=${d.run_id})`, 'evt')
          streamRun(d.run_id)
        } catch (_) {}
      },
      auto_run_finished: (e) => {
        try {
          const d = JSON.parse(e.data)
          const tag = d.ok ? '[成功]' : (d.category === 'network' ? '[网络错误，号已 release]' : '[失败]')
          addLog(`[auto] ${tag} ${d.email} 完成`, d.ok ? 'ok' : 'err')
          useStatsStore().refresh()
          bumpData()
        } catch (_) {}
      },
      circuit_break: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] 熔断: ${d.reason}`, 'err')
          banner.value = d.reason
        } catch (_) {}
      },
      reauth_state: (e) => {
        try { applyReauthState(JSON.parse(e.data)) } catch (_) {}
      },
      reauth_run_started: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[reauth] 开始 ${d.email} (run=${d.run_id})`, 'evt', 'reauth')
          if (d.email) reauthBatch.value = { ...reauthBatch.value, current: d.email, done: false }
          streamRun(d.run_id, 0, { channel: 'reauth' })
        } catch (_) {}
      },
      reauth_run_finished: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[reauth] ${d.ok ? '[成功]' : '[失败]'} ${d.email}`, d.ok ? 'ok' : 'err', 'reauth')
          useStatsStore().refresh()
          bumpData()
        } catch (_) {}
      },
      reauth_batch_done: (e) => {
        try {
          const d = JSON.parse(e.data || '{}')
          addLog(`[reauth] 队列结束${d.n ? ` (共 ${d.n} 个号)` : ''}`, 'evt', 'reauth')
        } catch (_) {}
        endReauthBatch()
      },
    }, () => {
      try { es.close() } catch (_) {}
      eventsEs = null
      if (!eventsTimer) eventsTimer = setTimeout(() => { eventsTimer = 0; connectEvents() }, 2000)
    })
    eventsEs = es
    bindVisibility()
  }

  function suspendStreams() {
    if (eventsTimer) { clearTimeout(eventsTimer); eventsTimer = 0 }
    if (eventsEs) { try { eventsEs.close() } catch (_) {} eventsEs = null }
    for (const info of runStreams.values()) {
      info.suspended = true
      if (info.es) { try { info.es.close() } catch (_) {} info.es = null }
    }
  }

  function resumeStreams() {
    connectEvents()
    for (const info of runStreams.values()) {
      if (info.ended || !info.suspended) continue
      info.suspended = false
      info.retries = 0
      if (info.attach) info.attach()
    }
  }

  // 后台标签页不占连接：切走就断，切回来按 offset 续。
  function bindVisibility() {
    if (visibilityBound || typeof document === 'undefined') return
    visibilityBound = true
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) suspendStreams()
      else resumeStreams()
    })
  }

  return {
    logs, reauthLogs, autoStatus, banner,
    lastRunResult, lastReauthResult, reauthBatch, dataVersion,
    runningSingle, runningReauth, reauthRestoreOpen,
    addLog, clearLogs, clearReauthLogs, bumpData, dismissBanner,
    streamRun, connectEvents, restoreLogs, ackReauthRestore,
    beginReauthBatch, endReauthBatch, abortReauthBatch,
  }
})
