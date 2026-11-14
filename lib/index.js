/**
 * dsh-copyback — DeepSeek Harness host plugin.
 *
 * Wraps the local clipboard-history daemon (scripts/clipboard_daemon.py, MIT by
 * 飞哥/FeiGe) as three model-callable tools: recall what you copied earlier, copy an
 * entry back to the system clipboard, and manage recording / pin / delete.
 *
 * Privacy stance (same as the upstream skill):
 * - The daemon runs on 127.0.0.1 only, data stays under the user's local app
 *   data dir; nothing is uploaded, no credentials are read.
 * - Clipboard content marked secret by browsers / password managers
 *   (Clipboard Viewer Ignore, CanIncludeInClipboardHistory, ...) is skipped.
 * - On Windows only foreground app copies are recorded by default
 *   (background process writes are ignored). On macOS/Linux owner detection is
 *   unavailable, so first-run seeds settings to record plain clipboard text.
 */
import { spawn } from 'node:child_process'
import { mkdir, readFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dsh-copyback'
export const inject = ['tools']

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const DAEMON = path.join(ROOT, 'scripts', 'clipboard_daemon.py')

const PORT_BASE = Number(process.env.DSH_COPYBACK_PORT) || 18765
const DATA_DIR = process.env.DSH_COPYBACK_DATA || (process.platform === 'win32'
  ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'dsh-copyback')
  : path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'dsh-copyback'))

const OBJECT = { type: 'object', additionalProperties: true }
const renderObject = (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }]

function pythonBin() {
  return process.env.DSH_COPYBACK_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
}

function runtimePath() {
  return path.join(DATA_DIR, 'runtime.json')
}

async function readRuntime() {
  try {
    const data = JSON.parse(await readFile(runtimePath(), 'utf8'))
    if (Number.isInteger(data?.port) && typeof data?.token === 'string') return data
  } catch { /* first run or stale file */ }
  return null
}

async function probe(port, timeoutMs = 1500) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    })
    return res.ok
  } catch {
    return false
  }
}

/**
 * First run on a platform where clipboard-owner detection is unavailable
 * (macOS/Linux): seed settings so recording actually captures text.
 * Windows keeps the upstream default (only foreground copies are recorded).
 */
async function seedSettingsIfNeeded() {
  const file = path.join(DATA_DIR, 'settings.json')
  if (existsSync(file)) return
  await mkdir(DATA_DIR, { recursive: true })
  const seed = { recording: true, user_copy_only: process.platform === 'win32' }
  await import('node:fs/promises').then((fs) => fs.writeFile(file, JSON.stringify(seed, null, 2), 'utf8'))
}

function spawnDaemon(firstRun) {
  const argv = [DAEMON, 'serve', '--port', String(PORT_BASE)]
  if (firstRun) argv.push('--recording') // afterwards the daemon resumes the persisted choice
  const child = spawn(pythonBin(), argv, {
    cwd: ROOT,
    env: { ...process.env, CH_DATA_DIR: DATA_DIR },
    stdio: 'ignore',
    detached: true,
    windowsHide: true,
  })
  child.unref()
}

async function ensureDaemon(exec) {
  let rt = await readRuntime()
  if (rt && (await probe(rt.port))) return rt
  await seedSettingsIfNeeded()
  spawnDaemon(!rt)
  const deadline = Date.now() + 20000
  while (Date.now() < deadline) {
    if (exec?.signal?.aborted) throw new Error('aborted')
    rt = await readRuntime()
    if (rt && (await probe(rt.port))) return rt
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error(
    `clipboard daemon did not become healthy within 20s (data dir: ${DATA_DIR}). `
    + 'Install Python 3 or set DSH_COPYBACK_PYTHON; check port availability around '
    + `${PORT_BASE} or set DSH_COPYBACK_PORT.`)
}

async function api(rt, apiPath, payload, exec, timeoutMs = 8000) {
  const timeout = AbortSignal.timeout(timeoutMs)
  const signal = exec?.signal ? AbortSignal.any([exec.signal, timeout]) : timeout
  const res = await fetch(`http://127.0.0.1:${rt.port}${apiPath}`, {
    method: payload === undefined ? 'GET' : 'POST',
    headers: {
      'X-CH-Token': rt.token,
      ...(payload !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
    signal,
  })
  const data = await res.json()
  if (!res.ok || (data && data.error)) {
    const msg = data?.error || `HTTP ${res.status}`
    const err = new Error(`${apiPath} failed: ${msg}`)
    err.data = data
    throw err
  }
  return data
}

// ---------------------------------------------------------------- pure helpers

/** Local YYYY-MM-DD of a unix timestamp. */
export function dayKey(ts, now = new Date()) {
  const d = new Date(Number(ts) * 1000)
  if (Number.isNaN(d.getTime())) return null
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Keep only entries recorded on the requested day ("today"/"yesterday"/YYYY-MM-DD). */
export function filterWithin(items, within, now = new Date()) {
  if (!within || within === 'all') return [...items]
  const shift = { today: 0, yesterday: 1 }[within]
  const base = shift === undefined ? within : (() => {
    const d = new Date(now)
    d.setDate(d.getDate() - shift)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  })()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(base)) return [...items]
  return items.filter((it) => dayKey(it?.ts, now) === base)
}

const PREVIEW_CHARS = 400

/** Bounded row list, safe to hand to a model. */
export function summarizeItems(items, limit = 20) {
  return items.slice(0, limit).map((it) => ({
    id: it.id,
    time: new Date(Number(it.ts) * 1000).toISOString(),
    chars: it.chars ?? (it.text ? it.text.length : 0),
    source: it.source ?? it.app ?? null,
    pinned: Boolean(it.pinned),
    preview: typeof it.text === 'string' ? it.text.slice(0, PREVIEW_CHARS) : '',
  }))
}

// ------------------------------------------------------------------ the plugin

export function apply(ctx) {
  const searchTool = defineTool({
    name: 'copyback_search',
    description: 'Search your local clipboard history (text you copied earlier on this machine). Returns id, time, source app, pinned flag and a preview per entry, newest first. Read-only; the recorder daemon is started automatically on first use.',
    parameters: {
      query: { type: 'string', description: 'Substring to match against copied text; omit for the newest entries' },
      limit: { type: 'number', description: 'Max entries to return (default 20, max 100)' },
      within: { type: 'string', description: 'Restrict to "today", "yesterday", or a "YYYY-MM-DD" local date' },
    },
    output: { schema: OBJECT, render: renderObject },
    async execute(args, exec) {
      const rt = await ensureDaemon(exec)
      const state = await api(rt, '/api/state', undefined, exec)
      const limit = Math.min(Math.max(Number(args?.limit) || 20, 1), 100)
      const q = String(args?.query ?? '')
      const data = await api(rt, `/api/items?limit=200&q=${encodeURIComponent(q)}`, undefined, exec)
      const items = filterWithin(Array.isArray(data.items) ? data.items : [], args?.within)
      return {
        recording: Boolean(state.recording),
        total: items.length,
        shown: Math.min(items.length, limit),
        items: summarizeItems(items, limit),
        note: 'Pass an id to copyback_copy to put an entry back on the system clipboard.',
      }
    },
  })

  const copyTool = defineTool({
    name: 'copyback_copy',
    description: 'Put one clipboard-history entry (by id from copyback_search) back onto the system clipboard, so the user can paste it anywhere with Ctrl+V / Cmd+V. Shows what is being copied.',
    parameters: {
      id: { type: 'string', required: true, description: 'Entry id returned by copyback_search' },
    },
    output: { schema: OBJECT, render: renderObject },
    async execute(args, exec) {
      const rt = await ensureDaemon(exec)
      const data = await api(rt, '/api/copy', { id: String(args?.id ?? '') }, exec)
      const text = typeof data.text === 'string' ? data.text : ''
      return {
        ok: Boolean(data.ok),
        copied: text.slice(0, 200),
        chars: text.length,
        note: 'Written to the system clipboard. The user can now paste it directly.',
      }
    },
  })

  const manageTool = defineTool({
    name: 'copyback_manage',
    description: 'Manage the clipboard-history recorder: pin/unpin or delete one entry, clear all history, start/stop recording, or report daemon status. Destructive actions (delete one, clear all) act only on the local history store, never on any other file.',
    parameters: {
      action: {
        type: 'string',
        required: true,
        description: 'One of: pin, unpin, delete, clear, start_recording, stop_recording, status',
      },
      id: { type: 'string', description: 'Entry id — required for pin / unpin / delete' },
    },
    output: { schema: OBJECT, render: renderObject },
    async execute(args, exec) {
      const action = String(args?.action ?? '')
      const rt = await ensureDaemon(exec)
      switch (action) {
        case 'pin':
        case 'unpin': {
          if (!args?.id) throw new Error('pin/unpin requires id')
          return api(rt, '/api/pin', { id: String(args.id), pinned: action === 'pin' }, exec)
        }
        case 'delete': {
          if (!args?.id) throw new Error('delete requires id')
          return api(rt, '/api/delete', { id: String(args.id) }, exec)
        }
        case 'clear':
          return api(rt, '/api/clear', { confirm: '清空' }, exec)
        case 'start_recording':
        case 'stop_recording':
          return api(rt, '/api/recording', { enabled: action === 'start_recording' }, exec)
        case 'status': {
          const state = await api(rt, '/api/state', undefined, exec)
          return {
            recording: Boolean(state.recording),
            count: state.count,
            platform: state.platform,
            secret_filter: state.secret_filter,
            user_copy_only: state.user_copy_only,
            storage_path: state.storage_path,
            version: state.version,
          }
        }
        default:
          throw new Error(`unknown action: ${action}`)
      }
    },
  })

  for (const tool of [searchTool, copyTool, manageTool]) ctx.tools.register(tool)
}
