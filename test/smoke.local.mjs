/** Smoke test: real daemon + real clipboard, sandboxed data dir. Not shipped. */
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { execSync } from 'node:child_process'

const dir = await mkdtemp(path.join(tmpdir(), 'dsh-copyback-smoke-'))
process.env.DSH_COPYBACK_DATA = dir
// 沙盒关闭「前台复制判定」，否则后台写入的测试文本不会入册
await writeFile(path.join(dir, 'settings.json'), JSON.stringify({ recording: true, user_copy_only: false }))

// 注意：必须动态 import——lib/index.js 在模块加载时读取 DSH_COPYBACK_DATA，
// 静态 import 会被提升到 env 设置之前。
const { apply } = await import('../lib/index.js')

const setClip = (text) => execSync(
  process.platform === 'win32'
    ? `powershell -NoProfile -Command "Set-Clipboard -Value '${text}'"`
    : `printf '%s' '${text}' | pbcopy`)

const getClip = () => execSync(
  process.platform === 'win32'
    ? 'powershell -NoProfile -Command "Get-Clipboard"'
    : 'pbpaste', { encoding: 'utf8' }).trim()

const results = {}
const tools = {}
apply({ tools: { register: (t) => { tools[t.name] = t } } })
try {
  // 先触发守护进程拉起（启动时当前剪贴板会被视作残留跳过），再写入测试文本
  const pre = await tools.copyback_manage.execute({ action: 'status' })
  console.error('[pre-boot]', JSON.stringify(pre))
  setClip('dsh-copyback-smoke-alpha-12345')
  console.error('[clipboard now]', getClip())
  await new Promise((r) => setTimeout(r, 5000))   // 等轮询入册

  const st = await tools.copyback_manage.execute({ action: 'status' })
  console.error('[state]', JSON.stringify(st))
  const search = await tools.copyback_search.execute({ query: 'smoke-alpha' })
  results.search = { found: search.items.length > 0, total: search.total, recording: search.recording }
  const hit = search.items[0]
  if (!hit) throw new Error('search found nothing')

  await tools.copyback_manage.execute({ action: 'pin', id: hit.id })
  setClip('overwritten-not-in-history')
  const copy = await tools.copyback_copy.execute({ id: hit.id })
  results.copy = { ok: copy.ok, clipboard: getClip(), preview: copy.copied.slice(0, 40) }

  const status = await tools.copyback_manage.execute({ action: 'status' })
  results.status = { recording: status.recording, count: status.count, platform: status.platform }

  const del = await tools.copyback_manage.execute({ action: 'delete', id: hit.id })
  results.delete = del
  const after = await tools.copyback_search.execute({ query: 'smoke-alpha' })
  results.afterDelete = { total: after.total }
} finally {
  console.log(JSON.stringify(results, null, 2))
  await rm(dir, { recursive: true, force: true }).catch(() => {})
}
