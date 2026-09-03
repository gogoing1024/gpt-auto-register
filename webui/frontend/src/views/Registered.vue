<script setup>
import { computed, onActivated, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listRegistered, getRegistered, deleteRegistered,
  bulkDeleteRegistered, bulkDeleteAccounts, checkPlus,
  listExportFormats, exportRegistered, updateCredentials,
} from '@/api/register'
import { copyText, fmtTime } from '@/api/request'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const { form } = storeToRefs(useFormStore())
// 检测用的代理必须能从代理池里挑：以前这页只在代码里读 form.proxy，页面上
// 连个输入框都没有，主人在代理池换了密码，这里还在用 localStorage 里的旧值，
// 结果是 curl:(97) 代理鉴权被拒 → 静默降级直连 → 拿真实 IP 打 chatgpt.com。
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
// dataVersion 要走 storeToRefs 才保持响应（watch 用）；bumpData 是 action，直接从
// store 实例上取 —— storeToRefs 只转 state/getter，把 action 解构出来会丢 this。
const { dataVersion } = storeToRefs(runtime)

const PAGE_SIZE_OPTIONS = [50, 100, 500, 1000]
const pageSize = ref(50)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const filter = ref('all')
const selected = ref([])
const loading = ref(false)
const checking = ref(false)
const checkResult = ref('')

// 按邮箱搜索
const searchText = ref('')
const appliedSearch = ref('')
const searchActive = computed(() => !!appliedSearch.value)
const isSearchFocused = ref(false)

const PLUS_TYPE = {
  plus_eligible: 'success', plus_active: 'primary', free: 'warning',
  // token_invalid（401 且响应体没有封号措辞）仍与 banned 分开显示——判据不同，
  // 不能混成一个。但配色从橙改红：AT 未到期却 401 = 被吊销，实测多半就是封号，
  // 橙色（=号还在）会让主人以为重新登录就能救回来。
  token_invalid: 'danger',
  banned: 'danger', error: 'danger',
}
function plusOf(row) { return row.plus_check || null }

// 自动检测：按库里的顺序全量查 Plus，已检测的也再跑一遍。
// 游标写进 form（localStorage）：刷新后按上次最后一个号接着扫，不要从头来。
// 只有主人把开关关掉再开，才重置到最新一条。
// 扫完一轮**不再**立刻从 0 再来：以前是一轮接一轮永不停，等于一直在打 chatgpt.com。
// 现在收完一轮记下时间（也进 localStorage，刷新页面不会重置），
// 满 AUTO_ROUND_COOLDOWN_HOURS 小时后才自动开下一轮；期间新注册进来的号等下一轮，
// 急的话手动点「检查未检测」。开关关掉再开 = 主人明确要跑，不受这个限制。
const AUTO_BATCH = 5
const AUTO_NEXT_MS = 800
const AUTO_IDLE_MS = 10000
const AUTO_BACKOFF_MS = 20000
const AUTO_ROUND_COOLDOWN_HOURS = 6
const AUTO_ROUND_COOLDOWN_MS = AUTO_ROUND_COOLDOWN_HOURS * 60 * 60 * 1000
// 冷却期间不挂一个 6 小时的长 setTimeout：电脑睡眠 / 后台标签页节流会让长定时器
// 漂得离谱。改成每分钟醒一次看墙上时钟到没到，到点才真正开下一轮。
const AUTO_COOLDOWN_POLL_MS = 60 * 1000
let autoTimer = 0
let autoBusy = false
let autoOffset = 0
let autoResumed = false
const autoSkipUntil = new Map()

// 距本轮冷却结束还有多久（ms），≤0 表示可以开新一轮。
// 上限卡在一个冷却周期：系统时间被改到未来再改回来时，别让一个诡异的时间戳把检测锁死几天。
function autoCooldownLeft() {
  const done = Number(form.value.autoCheckRoundDoneAt) || 0
  if (!done) return 0
  return Math.min(done + AUTO_ROUND_COOLDOWN_MS - Date.now(), AUTO_ROUND_COOLDOWN_MS)
}

function fmtClock(ms) {
  return new Date(ms).toLocaleString('zh-CN', {
    hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function showCooldownHint() {
  const done = Number(form.value.autoCheckRoundDoneAt) || 0
  if (!done) return
  checkResult.value =
    `自动检测本轮已完成（${fmtClock(done)}），` +
    `${fmtClock(done + AUTO_ROUND_COOLDOWN_MS)} 后自动重新运行（间隔 ${AUTO_ROUND_COOLDOWN_HOURS} 小时）`
}

function saveAutoCursor(lastEmail) {
  form.value.autoCheckOffset = autoOffset
  if (lastEmail !== undefined) form.value.autoCheckLastEmail = lastEmail || ''
}

function resetAutoCursor() {
  autoOffset = 0
  autoResumed = true
  saveAutoCursor('')
}

async function seekAfterEmail(email) {
  const scan = 100
  let offset = 0
  for (let i = 0; i < 50; i++) {
    const { items, total: t } = await listRegistered({
      filter: 'all', limit: scan, offset,
    })
    if (!items?.length) break
    const idx = items.findIndex((r) => r.email === email)
    if (idx >= 0) {
      autoOffset = offset + idx + 1
      return
    }
    offset += items.length
    if (offset >= (t || 0)) break
  }
  autoOffset = Math.max(0, parseInt(form.value.autoCheckOffset, 10) || 0)
}

async function ensureAutoResumed() {
  if (autoResumed) return
  autoResumed = true
  const last = String(form.value.autoCheckLastEmail || '').trim()
  if (last) await seekAfterEmail(last)
  else autoOffset = Math.max(0, parseInt(form.value.autoCheckOffset, 10) || 0)
}

function isAutoSkipped(email) {
  const until = autoSkipUntil.get(email)
  if (!until) return false
  if (Date.now() >= until) {
    autoSkipUntil.delete(email)
    return false
  }
  return true
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const { items, total: t, search } = await listRegistered({
      limit: pageSize.value, offset: (page.value - 1) * pageSize.value, filter: filter.value,
      search: appliedSearch.value,
    })
    rows.value = items
    total.value = t
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function doSearch() {
  appliedSearch.value = searchText.value.trim()
  load(true)
}

function clearSearch() {
  searchText.value = ''
  if (!appliedSearch.value) return
  appliedSearch.value = ''
  load(true)
}

// 监听搜索框的清空事件 (点击右侧 ✖ 图标时触发)
watch(searchText, (val) => {
  if (!val && appliedSearch.value) {
    clearSearch()
  }
})

function onSearchPaste(e) {
  const text = e.clipboardData?.getData('text') || ''
  if (text.includes('\n') || text.includes('----')) {
    e.preventDefault()
    const cleaned = text.split(/\r?\n/)
      .map(line => line.split('----')[0].trim())
      .filter(Boolean)
      .join(' ')
    if (!cleaned) return
    const input = e.target
    const start = input.selectionStart || 0
    const end = input.selectionEnd || 0
    const prefix = searchText.value.slice(0, start)
    const suffix = searchText.value.slice(end)
    const padLeft = (prefix && !prefix.endsWith(' ')) ? ' ' : ''
    const padRight = (suffix && !suffix.startsWith(' ')) ? ' ' : ''
    searchText.value = prefix + padLeft + cleaned + padRight + suffix
  }
}

function collectEmails(mode) {
  if (mode === 'selected') return selected.value.map((r) => r.email)
  if (mode === 'unchecked') return rows.value.filter((r) => !plusOf(r)).map((r) => r.email)
  return rows.value.map((r) => r.email) // all（当前页）
}

function applyCheckResults(results, note) {
  let plus = 0, free = 0, banned = 0, failed = 0, badToken = 0
  for (const [email, info] of Object.entries(results)) {
    const row = rows.value.find((r) => r.email === email)
    if (row) row.plus_check = info
    if (info.status === 'plus_eligible' || info.status === 'plus_active') plus++
    else if (info.status === 'banned') banned++
    else if (info.status === 'free') free++
    else if (info.status === 'token_invalid') badToken++
    else if (info.status === 'error') failed++
    if (info.status === 'no_at' || info.status === 'not_found') {
      autoSkipUntil.set(email, Date.now() + 60 * 60 * 1000)
    } else if (info.status === 'error') {
      autoSkipUntil.set(email, Date.now() + 30 * 1000)
    }
  }
  // failed / note 不入库，只是这一次的现场说明：
  // 以前网络/代理挂了这里只会显示「0 可用Plus, 0 Free, 0 封号」，看不出是没检测成。
  // badToken 从 2026-08-10 起是**会入库**的结论，措辞也跟着改：
  // AT 没过期却 401 = 被吊销，大概率就是封号，不该再说得像只是要重新登录。
  const parts = [`完成: ${plus} 可用Plus, ${free} Free, ${banned} 封号`]
  if (badToken) parts.push(`${badToken} 个凭证失效（AT 被吊销，多半已封）`)
  if (failed) parts.push(`${failed} 个没检测成`)
  if (note) parts.push(note)
  checkResult.value = parts.join(' · ')
  return { note, failed }
}

async function runCheck(emails, label) {
  if (!emails.length || checking.value) return null
  checking.value = true
  checkResult.value = `${label || '检查中'}... (${emails.length} 个)`
  try {
    const { results, note } = await checkPlus(emails, proxyText(form.value), {
      timeout: Math.min(180000, 30000 + emails.length * 15000),
    })
    return applyCheckResults(results || {}, note)
  } catch (e) {
    checkResult.value = ''
    ElMessage.error('检查失败: ' + e.message)
    return null
  } finally { checking.value = false }
}

async function doCheck(mode) {
  const emails = collectEmails(mode)
  if (!emails.length) { ElMessage.info('当前页没有可检测的号'); return }
  await runCheck(emails)
}

function scheduleAutoCheck(delay) {
  if (autoTimer) clearTimeout(autoTimer)
  autoTimer = window.setTimeout(() => { autoTick() }, delay)
}

async function pickAutoEmails(max) {
  await ensureAutoResumed()
  const scan = 50
  const picked = []
  let total = 0
  let wrapped = false
  for (let i = 0; i < 8 && picked.length < max; i++) {
    const { items, total: t } = await listRegistered({
      filter: 'all', limit: scan, offset: autoOffset,
    })
    total = t || 0
    if (!total) {
      // 表是空的：谈不上「一轮」，不算收轮、不进 6 小时冷却，
      // autoTick 按空闲间隔再看，否则第一个注册进来的号要白等 6 小时。
      resetAutoCursor()
      break
    }
    if (autoOffset >= total || !items?.length) {
      // 扫到底 = 本轮收工。以前这里 picked 为空会 continue，立刻从 0 接着挑
      // 下一轮的号，一轮接一轮永不停。现在到此为止，把 wrapped 交给 autoTick 进冷却。
      resetAutoCursor()
      wrapped = true
      break
    }
    for (const r of items) {
      autoOffset++
      if ((r.at_len || 0) <= 0) continue
      if (isAutoSkipped(r.email)) continue
      picked.push(r.email)
      if (picked.length >= max) break
    }
  }
  // 刚收完一轮时游标已经回到 0，不能把本批最后一号写成续扫点，
  // 否则刷新会从那一号后面接着，新一轮开头被跳过。
  if (wrapped) saveAutoCursor('')
  else saveAutoCursor(picked.length ? picked[picked.length - 1] : undefined)
  return { emails: picked, total, wrapped }
}

async function autoTick() {
  autoTimer = 0
  if (!form.value.autoCheckPlus) return
  if (checking.value || autoBusy) {
    scheduleAutoCheck(1500)
    return
  }
  // 上一轮收工还没满 6 小时：什么都不查，每分钟醒来看一眼时钟。
  // 提示只在没别的话可显示时才写（刷新页面回来是空的），
  // 别把主人刚手动点「检测选中」得到的结果给盖掉。
  const wait = autoCooldownLeft()
  if (wait > 0) {
    if (!checkResult.value) showCooldownHint()
    scheduleAutoCheck(Math.min(wait, AUTO_COOLDOWN_POLL_MS))
    return
  }
  autoBusy = true
  try {
    const { emails, total, wrapped } = await pickAutoEmails(AUTO_BATCH)
    let r = null
    if (emails.length) {
      // 收轮那一批游标已经回 0，进度不能显示成 0/total
      const pos = wrapped ? total : autoOffset
      r = await runCheck(emails, total ? `全量检测中 ${pos}/${total}` : '全量检测中')
      await load()
    }
    if (!form.value.autoCheckPlus) return
    if (wrapped) {
      // 最后一批查完才算本轮结束，从这一刻起算 6 小时
      form.value.autoCheckRoundDoneAt = Date.now()
      showCooldownHint()
      scheduleAutoCheck(AUTO_COOLDOWN_POLL_MS)
      return
    }
    if (!emails.length) {
      scheduleAutoCheck(AUTO_IDLE_MS)
      return
    }
    scheduleAutoCheck(r?.note ? AUTO_BACKOFF_MS : AUTO_NEXT_MS)
  } catch (_) {
    if (form.value.autoCheckPlus) scheduleAutoCheck(AUTO_IDLE_MS)
  } finally {
    autoBusy = false
  }
}

// customClass 里的 pre-line 让消息里的 \n 真的换行。
// 不用 dangerouslyUseHTMLString：消息里会拼邮箱、文件名这些数据，走 HTML 等于开 XSS 口子。
async function confirm(msg) {
  try {
    await ElMessageBox.confirm(msg, '确认', {
      type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消',
      customClass: 'confirm-multiline',
    })
    return true
  }
  catch (_) { return false }
}
async function deleteOne(email) {
  if (!(await confirm(`删除 ${email} 的凭证？`))) return
  try { await deleteRegistered(email); ElMessage.success('已删除'); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`确定删除选中的 ${emails.length} 条凭证？(不可恢复)`))) return
  try { const r = await bulkDeleteRegistered({ emails }); ElMessage.success(`已删除 ${r.deleted} 条`); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteAll() {
  if (!(await confirm('这会清空注册结果表里的所有凭证！邮箱列表不受影响，确定？'))) return
  if (!(await confirm('再次确认：真的要删除全部凭证吗？此操作不可恢复！'))) return
  try { const r = await bulkDeleteRegistered({ all: true }); ElMessage.success(`已清空 ${r.deleted} 条`); load() }
  catch (e) { ElMessage.error(e.message) }
}

// ──────────── 批量导出 ────────────
// 格式清单来自后端 export_formats.py，下拉菜单是 v-for 出来的：
// 以后加格式只改后端那一个文件，这里一行都不用动。
const exportFormats = ref([])
const exporting = ref(false)
const exportVisible = ref(false)
const exportText = ref('')
const exportCount = ref(0)
const exportFilename = ref('')
const exportLabel = ref('')
// 这一批导出的到底是哪些号 —— 「下载并删除」照着它删，来自后端 r.emails。
// 为什么要后端给、为什么在导出那一刻就存下来：
//   · 「导出全部」是跨页的，前端手里只有当前页 20 行，自己凑必漏；
//   · 弹窗开着的时候主人可能改勾选、翻页，后台自动跑号还会插进新号进来，
//     那时再去读 selected/表格，删的就不是刚下载的那批了。
const exportedEmails = ref([])
const deletingExported = ref(false)

const exportBtnText = computed(() =>
  selected.value.length ? `导出选中 (${selected.value.length})` : '导出全部',
)

async function loadExportFormats() {
  if (exportFormats.value.length) return
  try {
    const { formats } = await listExportFormats()
    exportFormats.value = formats || []
  } catch (e) { ElMessage.error('加载导出格式失败: ' + e.message) }
}

async function doExport(fmt) {
  const emails = selected.value.map((r) => r.email)
  // 没勾选 = 导出全部（跨页，不只当前页）
  const payload = emails.length ? { format: fmt.id, emails } : { format: fmt.id, all: true }
  exporting.value = true
  try {
    const r = await exportRegistered(payload)
    exportedEmails.value = (r.emails || []).filter(Boolean)
    // download 模式（CPA zip / SUB2API json）：不弹预览，直接落盘
    if (r.mode === 'download') {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime)
      ElMessage.success(`已下载 ${r.filename}（${r.count} 个号）`)
      return
    }
    exportText.value = r.text || ''
    exportCount.value = r.count || 0
    exportFilename.value = r.filename || 'export.txt'
    exportLabel.value = r.label || fmt.label
    exportVisible.value = true
  } catch (e) { ElMessage.error('导出失败: ' + e.message) }
  finally { exporting.value = false }
}

function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function saveBlob(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function downloadExport() {
  saveBlob(exportText.value, exportFilename.value, 'text/plain;charset=utf-8')
}

// ──────────── 下载并删除 ────────────
// 主人的原话：「不然分不清楚越堆越多」。导出的 txt 里邮箱/密码/2FA/取件url 都齐了，
// 这两张表就没有留存价值了，一起清掉。
//
// ⚠️ 顺序**必须**是「先下载、再确认、最后删」：
//    删库是不可恢复的，而浏览器下载可能被拦（弹窗拦截 / 用户点了取消 / 磁盘满）。
//    先把文件落盘再问，主人是在**手里已经有 txt** 的前提下点的确认。
//    确认框里再报一遍将要删的两张表各多少条，删完之前还有最后一次反悔机会。
async function downloadAndDelete() {
  downloadExport()

  const emails = exportedEmails.value
  if (!emails.length) {
    ElMessage.warning('这批导出没有拿到 email 列表，只下载不删除')
    return
  }

  const ok = await confirm(
    `已下载 ${exportFilename.value}。\n\n` +
    `现在删除这 ${emails.length} 个号：\n` +
    `  · 注册结果（凭证、2FA secret）\n` +
    `  · 邮箱列表（号池那一行，含取件链接）\n\n` +
    `删掉后只剩刚下载的 txt 这一份，不可恢复。确定？`,
  )
  if (!ok) return

  deletingExported.value = true
  try {
    // 两张表分别删。先删注册结果：它是主人真正在看的那张表，
    // 万一号池那边报错（比如这批号根本不是号池导入的、压根没有对应行），
    // 至少结果表已经清干净了，不会出现"删了一半还看得见"。
    const r1 = await bulkDeleteRegistered({ emails })
    let poolDeleted = 0
    try {
      const r2 = await bulkDeleteAccounts({ emails })
      poolDeleted = r2.deleted || 0
    } catch (e) {
      // 号池删失败不算整体失败：凭证已经清掉了，主人该知道的是号池还剩着
      ElMessage.warning('注册结果已删，但邮箱列表删除失败: ' + e.message)
    }
    ElMessage.success(`已删除：注册结果 ${r1.deleted} 条 / 邮箱列表 ${poolDeleted} 条`)
    exportVisible.value = false
    exportedEmails.value = []
    selected.value = []
    load(true)          // 回第一页：这一批没了，停在旧页码多半是空页
    runtime.bumpData()  // 通知「邮箱列表」那一页也刷新，否则主人切过去还看得到已删的号
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  } finally {
    deletingExported.value = false
  }
}

// 凭证弹窗
const credVisible = ref(false)
const credEmail = ref('')
const credData = ref(null)
// 展示顺序 + 中文别名。密码和 2FA 密钥排最前：登录要用的就这两个，
// 后面那堆 token 是喂给 API 的，日常打开弹窗多半只是来抄前两行。
// 末尾几个（factor_id / device_id / csrf / cookie）基本只在排查问题时才看。
const CRED_META = [
  ['password', '登录密码'],
  ['totp_secret', '2FA 密钥'],
  ['access_token', '访问令牌'],
  ['session_token', '会话令牌'],
  ['refresh_token', '刷新令牌'],
  ['id_token', 'ID 令牌'],
  ['totp_factor_id', '2FA factor'],
  ['device_id', '设备 ID'],
  ['csrf_token', 'CSRF'],
  ['cookie_header', 'Cookie'],
]
// 超过这个长度才用多行框。32 位 secret、36 位 uuid、16 位密码都能在单行里
// 一眼看全，之前一律给 2 行 textarea，短字段白占一倍高度、长 token 又只露两行。
const INLINE_MAX = 80
const credRows = computed(() => {
  if (!credData.value) return []
  return CRED_META
    .filter(([k]) => credData.value[k])
    .map(([k, label]) => {
      const val = String(credData.value[k])
      return { key: k, label, val, short: val.length <= INLINE_MAX, critical: k === 'totp_secret' }
    })
})
async function viewCred(email) {
  try {
    const { data } = await getRegistered(email)
    credData.value = data
    credEmail.value = email
    credVisible.value = true
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
async function copyCell(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
function copyAllJson() {
  if (credData.value) copyText(JSON.stringify(credData.value, null, 2))
}

// ── 手动编辑凭证 ──
// 只改本地库，不同步 OpenAI。改完的值会被登录流程直接用上
// （registrar 的 account_callback 走 db.get_registered，不区分数据来源）。
const editVisible = ref(false)
const editSaving = ref(false)
const editEmail = ref('')
const editPassword = ref('')
const editSecret = ref('')
// 打开弹窗时的原值，用来判断哪些字段真被改过（没改的不传，后端就不碰）
const editOrigPassword = ref('')
const editOrigSecret = ref('')

function openEdit(row) {
  editEmail.value = row.email
  editPassword.value = row.password || ''
  editSecret.value = row.totp_secret || ''
  editOrigPassword.value = row.password || ''
  editOrigSecret.value = row.totp_secret || ''
  editVisible.value = true
}

async function saveEdit() {
  const pw = editPassword.value
  const sec = editSecret.value.trim()
  const payload = { email: editEmail.value }
  // 只把真正改动过的字段传给后端 —— 没动的字段不传，后端就不会碰它
  if (pw !== editOrigPassword.value) payload.password = pw
  if (sec !== editOrigSecret.value) payload.totp_secret = sec
  if (payload.password === undefined && payload.totp_secret === undefined) {
    ElMessage.info('没有改动')
    editVisible.value = false
    return
  }
  // secret 是唯一「服务端取不回」的凭证：覆盖掉原值 = 该号 2FA 永久锁死。
  // 只在「原本就有 secret」且「确实要改」时拦一道，新填不打扰。
  if (payload.totp_secret !== undefined && editOrigSecret.value) {
    try {
      await ElMessageBox.confirm(
        `该账号已有 2FA secret：\n${editOrigSecret.value}\n\n` +
        '覆盖后原 secret 将永久丢失，服务端取不回。\n' +
        '若原 secret 仍是账号上生效的那个，覆盖会导致该号 2FA 永远登不上。',
        '确认覆盖 2FA secret？',
        { type: 'warning', confirmButtonText: '确认覆盖', cancelButtonText: '取消' },
      )
    } catch { return }
  }
  editSaving.value = true
  try {
    const r = await updateCredentials(payload)
    ElMessage.success(`已保存：${(r.changed || []).join(' + ') || '无改动'}`)
    editVisible.value = false
    await load()
  } catch (e) {
    // 后端 400 会带具体原因（如「TOTP secret 含非法字符」），原样透出
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally { editSaving.value = false }
}

watch(page, () => load())
watch(pageSize, () => load(true))
watch(dataVersion, () => {
  load()
  if (form.value.autoCheckPlus) scheduleAutoCheck(600)
})
watch(() => form.value.autoCheckPlus, (on) => {
  if (on) {
    resetAutoCursor()
    // 主人亲手关掉再开 = 现在就要跑一轮，6 小时冷却只管「自动」重开
    form.value.autoCheckRoundDoneAt = 0
    checkResult.value = '自动检测已开启，正在全量检测（含已检测）'
    scheduleAutoCheck(200)
  } else if (autoTimer) {
    clearTimeout(autoTimer)
    autoTimer = 0
  }
})
onActivated(() => {
  load()
  if (form.value.autoCheckPlus) scheduleAutoCheck(400)
})
onUnmounted(() => {
  if (autoTimer) clearTimeout(autoTimer)
})
</script>
<template>
  <div class="page">
    <el-card shadow="never">
      <template #header><span class="section-title" style="margin: 0">注册结果</span></template>

      <!-- 按邮箱搜索 -->
      <div class="search-row">
        <div class="search-wrap">
          <el-input
            v-model="searchText" type="textarea" class="mono search-box"
            :class="{ 'is-focused': isSearchFocused }"
            autosize resize="none"
            placeholder="按邮箱搜索：一行一个"
            @keydown.enter.ctrl.prevent="doSearch"
            @keydown.enter.meta.prevent="doSearch"
            @focus="isSearchFocused = true"
            @blur="isSearchFocused = false"
            @paste="onSearchPaste"
          />
        </div>
        <div class="search-btns">
          <el-button type="primary" @click="doSearch"><el-icon><Search /></el-icon>搜索</el-button>
          <el-button v-if="searchText || searchActive" @click="clearSearch">清除</el-button>
          <!-- 刷新挪到搜索行：下面那条工具栏少一个按钮，「自动检测」的位置就稳定了 -->
          <el-button @click="load(false)"><el-icon><Refresh /></el-icon>刷新</el-button>
        </div>
      </div>

      <el-space wrap class="toolbar" style="margin-bottom: 12px">
        <el-select v-model="filter" style="width: 130px" @change="load(true)">
          <el-option label="全部" value="all" />
          <el-option label="有 RT" value="has_rt" />
          <el-option label="无 RT" value="no_rt" />
          <el-option label="无 密码" value="no_password" />
          <el-option label="无 2FA" value="no_2fa" />
          <el-option label="未检测" value="unchecked" />
          <el-option label="Free" value="free" />
          <el-option label="可领Plus" value="plus" />
          <el-option label="已封号" value="banned" />
          <el-option label="凭证失效" value="token_invalid" />
        </el-select>
        <el-select
          v-model="form.proxy" filterable clearable allow-create default-first-option
          :reserve-keyword="false" placeholder="检测代理（留空直连）"
          style="width: 260px"
        >
          <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
        </el-select>
        <el-button :loading="checking" @click="doCheck('unchecked')">检查未检测</el-button>
        <el-button :loading="checking" @click="doCheck('all')">重新检查</el-button>
        <el-button :loading="checking" :disabled="!selected.length" @click="doCheck('selected')">
          检测选中 ({{ selected.length }})
        </el-button>
        <el-switch v-model="form.autoCheckPlus" active-text="自动检测" />
      </el-space>

      <!-- 导出 / 删除单独一行：跟上面的检测区分开，互相不会因为换行挤来挤去 -->
      <el-space wrap class="toolbar" style="margin-bottom: 12px">
        <el-dropdown trigger="click" @command="doExport" @visible-change="(v) => v && loadExportFormats()">
          <el-button :loading="exporting">
            <el-icon><Download /></el-icon>{{ exportBtnText }}
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="f in exportFormats" :key="f.id" :command="f" :divided="f.mode === 'download' && f.id === 'cpa'">
                {{ f.label }}
                <span v-if="f.note" class="hint" style="margin-left: 6px">{{ f.note }}</span>
              </el-dropdown-item>
              <el-dropdown-item v-if="!exportFormats.length" disabled>加载中...</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-divider direction="vertical" />
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          删除选中 ({{ selected.length }})
        </el-button>
        <el-button type="danger" plain @click="deleteAll">清空全部</el-button>
        <span class="hint">{{ checkResult }}</span>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
        <!-- 密码直接明文列出：随机 16 位，是登录账号的必需品，
             藏进「查看凭证」弹窗每次都要多点两下。列表接口本来就在返回它。
             图标放在文字**后面**：放前面会把值整体右推 27px（见 .cell-copy 注释）。 -->
        <el-table-column label="密码" min-width="170">
          <template #default="{ row }">
            <el-button
              v-if="row.password" size="small" text type="primary"
              class="cell-copy mono" @click="copyText(row.password)"
            >
              {{ row.password }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <!-- 2FA secret 同样明文列出：它是唯一「服务端取不回」的凭证，
             丢了这个号就永久锁死，必须一眼看见、一点就能复制。
             min-width 必须装得下 32 位 base32：.cell 带 overflow:hidden，
             宽度不够会**无声截断**，肉眼核对时看到的是残缺值。实测需 ~250px。 -->
        <el-table-column label="2FA" min-width="260">
          <template #default="{ row }">
            <el-button
              v-if="row.totp_secret" size="small" text type="warning"
              class="cell-copy mono" @click="copyText(row.totp_secret)"
            >
              {{ row.totp_secret }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Plus状态" width="120">
          <template #default="{ row }">
            <StatusDot v-if="plusOf(row)" :type="PLUS_TYPE[plusOf(row).status] || 'info'" :text="plusOf(row).label" />
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="access" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.at_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'access_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.at_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="session" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.st_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'session_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.st_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="refresh" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.rt_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'refresh_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.rt_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right" class-name="col-ops">
          <template #default="{ row }">
            <!-- flex + 取消相邻按钮默认 margin-left，否则「删除」会折到第二行、把行高撑开。
                 三颗按钮实测 132px（96px 文字 + 6px×2 内边距），配合下面收窄的 cell padding
                 刚好放进 160，再宽就是一片空白。nowrap 下放不下会裁字，别再往下调。 -->
            <div class="row-ops">
              <el-button size="small" text @click="viewCred(row.email)">查看凭证</el-button>
              <el-button size="small" text type="warning" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" @click="deleteOne(row.email)">删除</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="searchActive ? '没有匹配的凭证（换个条件，或点「清除」看全部）' : '暂无注册结果，去「单次注册」或「全自动批量」跑号'"
            :image-size="70"
          />
        </template>
      </el-table>
      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" v-model:page-size="pageSize"
          :page-sizes="PAGE_SIZE_OPTIONS" :total="total"
          layout="sizes, prev, pager, next, total" background
        />
      </div>

      <el-dialog v-model="exportVisible" width="720px" top="8vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span style="font-weight: 600">导出 · {{ exportLabel }}</span>
            <el-tag size="small" type="info">共 {{ exportCount }} 行</el-tag>
          </div>
        </template>
        <el-input
          :model-value="exportText" type="textarea" :rows="14" readonly
          class="mono export-area"
        />
        <template #footer>
          <el-button @click="copyText(exportText)">
            <el-icon><CopyDocument /></el-icon>复制全部
          </el-button>
          <el-button type="primary" @click="downloadExport">
            <el-icon><Download /></el-icon>下载 {{ exportFilename }}
          </el-button>
          <!-- 危险动作放最右、danger 色，和左边的纯下载拉开距离，避免手滑。
               先下载文件、再弹二次确认，确认框里会报清楚要删哪两张表各多少条。 -->
          <el-button
            type="danger" plain
            :loading="deletingExported"
            :disabled="!exportedEmails.length"
            @click="downloadAndDelete"
          >
            <el-icon><Delete /></el-icon>下载并删除这 {{ exportedEmails.length }} 个号
          </el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="credVisible" width="720px" top="6vh">
        <template #header>
          <div class="cred-head">
            <span class="mono cred-email">{{ credEmail }}</span>
            <el-button size="small" @click="copyAllJson">
              <el-icon><CopyDocument /></el-icon>复制全部 JSON
            </el-button>
          </div>
        </template>
        <!-- 限高 + 内部滚动：字段多的号（7 个以上）弹窗会顶出视口，
             底部的字段直接被裁掉，而 el-dialog 默认不给 body 滚动条。 -->
        <div class="cred-body">
          <div v-for="r in credRows" :key="r.key" class="cred-row">
            <div class="cred-row-head">
              <span class="mono cred-key" :class="{ critical: r.critical }">{{ r.key }}</span>
              <span class="cred-alias">{{ r.label }}</span>
              <el-tag size="small" type="info" round>{{ r.val.length }}</el-tag>
              <el-button class="cred-copy" size="small" text type="primary" @click="copyText(r.val)">
                <el-icon><CopyDocument /></el-icon>复制
              </el-button>
            </div>
            <el-input v-if="r.short" :model-value="r.val" readonly size="small" class="mono" />
            <el-input v-else :model-value="r.val" type="textarea" :rows="3" readonly class="mono" />
          </div>
          <el-empty v-if="!credRows.length" description="无凭证字段" :image-size="70" />
        </div>
      </el-dialog>

      <!-- 手动编辑凭证：把外部已知的密码/2FA 补进来，或修正记录错误 -->
      <el-dialog v-model="editVisible" title="编辑凭证" width="560px" top="10vh">
        <el-alert
          type="warning" :closable="false" show-icon style="margin-bottom: 16px"
          title="仅修改本地记录，不会同步到 OpenAI"
          description="这里改密码不等于改了账号密码。填入的值会被登录流程直接使用。"
        />
        <el-form label-position="top">
          <el-form-item label="邮箱">
            <el-input :model-value="editEmail" class="mono" disabled />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="editPassword" class="mono" placeholder="留空表示该号无密码" />
          </el-form-item>
          <el-form-item label="2FA Secret">
            <el-input
              v-model="editSecret" class="mono"
              placeholder="base32，支持带空格/小写/otpauth:// 链接，会自动规范化"
            />
            <div class="hint" style="margin-top: 6px; line-height: 1.6">
              服务端取不回此值，覆盖后原 secret 永久丢失。清空则该号按无 2FA 处理。
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="editVisible = false">取消</el-button>
          <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
        </template>
      </el-dialog>
    </el-card>
  </div>
</template>

<style scoped>
.search-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
}
.search-wrap {
  position: relative;
  width: 520px;
  height: 32px;
  max-width: 100%;
}
.search-box {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  transition: box-shadow 0.2s;
}
.search-box.is-focused {
  z-index: 100;
}
.search-box.is-focused :deep(.el-textarea__inner) {
  box-shadow: var(--el-box-shadow-light);
  overflow: hidden !important;
}
.search-box:not(.is-focused) :deep(.el-textarea__inner) {
  height: 32px !important;
  min-height: 32px !important;
  line-height: 20px !important;
  padding-top: 5px !important;
  padding-bottom: 5px !important;
  overflow: hidden !important;
  white-space: nowrap;
}
.search-box :deep(.el-textarea__inner) {
  font-size: 12px;
  line-height: 1.5;
}
.search-btns {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
/* el-space 的每一项都是 display:flex，且给子元素塞了 flex:1（= flex:1 1 0%，
   见 element-plus 的 .el-space__item>*）。于是行宽不够时控件是被**压窄**而不是换行，
   开关的 active-text「自动检测」就被折成两行。禁止收缩：放不下就整项换行。 */
.toolbar :deep(.el-space__item) {
  flex-shrink: 0;
}
.toolbar :deep(.el-switch__label) {
  white-space: nowrap;
}

/* 表格里「点一下就复制」的明文单元格（密码 / 2FA secret）。
   :deep 是必需的：.el-button 由 Element Plus 渲染，scoped 的属性选择器打不到它。

   为什么要重置 padding —— Element Plus 有两个长得很像的类：
     .el-button--text  （旧版 type="text"）  padding 左右为 0
     .el-button.is-text（新版 text 属性）    继承 --small 的 5px 11px
   我们用的是后者，于是 11px padding + 12px 图标 + 4px 间隙 = 值被整体右推 27px，
   同列的表头和空值「—」都贴着 cell 左沿，一眼就看出错位。 */
:deep(.el-button.cell-copy.el-button--small) {
  padding: 0 6px 0 0;
  height: 20px;
  font-size: 12px;
}
/* 图标默认透明但**保留占位**：用 opacity 而不是 display:none，
   否则 hover 时图标撑开宽度会把文字挤得左右抖。 */
:deep(.cell-copy .ico) {
  margin-left: 5px;
  opacity: 0;
  transition: opacity 0.12s;
}
:deep(.cell-copy:hover .ico) { opacity: 0.65; }

.row-ops {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  white-space: nowrap;
}
.row-ops :deep(.el-button) {
  margin-left: 0;
  padding: 0 6px;
}
/* 默认 cell 左右各 12px，操作列不需要这么松，省下的 8px 直接换成列宽 */
:deep(.col-ops .cell) {
  padding-left: 8px;
  padding-right: 8px;
}

/* ── 查看凭证弹窗 ── */
.cred-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.cred-email {
  font-weight: 600;
  word-break: break-all;
}
.cred-body {
  max-height: 68vh;
  overflow-y: auto;
  padding-right: 6px;
}
.cred-row + .cred-row { margin-top: 14px; }
.cred-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.cred-key {
  font-weight: 600;
  font-size: 13px;
}
/* 2FA 密钥标成警示色：这一列抄错一个字符，号就登不上了 */
.cred-key.critical { color: var(--el-color-warning); }
.cred-alias {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
/* 复制按钮统一贴右，纵向排成一列，不跟着字段名长短左右乱跳 */
.cred-copy { margin-left: auto; }
.cred-row :deep(.el-textarea__inner),
.cred-row :deep(.el-input__inner) {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px;
}
</style>

<!-- 非 scoped：ElMessageBox 是挂到 body 上的，不在本组件的 scope 属性范围内，
     scoped 样式打不到它。只作用在自家 customClass 上，不会污染别处的确认框。 -->
<style>
.confirm-multiline .el-message-box__message { white-space: pre-line; }
</style>
