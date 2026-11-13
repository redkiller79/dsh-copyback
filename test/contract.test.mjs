import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const pkg = JSON.parse(await readFile(path.join(ROOT, 'package.json'), 'utf8'))

test('manifest: installable DSH bundle with no install-time scripts', async () => {
  assert.equal(pkg.name, 'dsh-copyback')
  assert.match(pkg.version, /^\d+\.\d+\.\d+$/)
  assert.equal(pkg.type, 'module')
  assert.equal(pkg.dsh.bundle.patch, './cordis.patch.yml')
  assert.deepEqual(pkg.dependencies, {})
  const lifecycle = ['preinstall', 'install', 'postinstall', 'prepare']
  for (const key of lifecycle) assert.equal(pkg.scripts?.[key], undefined, `no ${key} script`)
  assert.equal(pkg.publishConfig.access, 'public')
  assert.ok(pkg.peerDependencies['@deepseek-ai/dsh-tools'])
  assert.ok(pkg.dsh.compatibility.platforms.includes('win32'))
})

test('bundle patch: one unique host entry declaring tools injection', async () => {
  const yml = await readFile(path.join(ROOT, 'cordis.patch.yml'), 'utf8')
  assert.match(yml, /-\s+insert:/)
  assert.match(yml, /id:\s*dsh-copyback/)
  assert.match(yml, /name:\s*'dsh-copyback'/)
  assert.match(yml, /inject:\s*\[tools\]/)
  assert.equal((yml.match(/id:\s*dsh-copyback/g) || []).length, 1)
})

test('bundled daemon, provenance and docs are present', async () => {
  for (const f of ['scripts/clipboard_daemon.py', 'LICENSE', 'NOTICE.md', 'CHANGES.md', 'README.md']) {
    const buf = await readFile(path.join(ROOT, f))
    assert.ok(buf.length > 100, `${f} is not empty`)
  }
  const license = await readFile(path.join(ROOT, 'LICENSE'), 'utf8')
  assert.match(license, /MIT License/)
  assert.match(license, /Copyright \(c\) 2026 ELLA/)
  const notice = await readFile(path.join(ROOT, 'NOTICE.md'), 'utf8')
  assert.match(notice, /clipboard-history/)
  assert.match(notice, /ELLA/)
  // 上游为原创实现：包内不得出现第三方剪贴板软件的品牌残留
  for (const f of ['README.md', 'NOTICE.md', 'CHANGES.md', 'lib/index.js']) {
    const text = await readFile(path.join(ROOT, f), 'utf8')
    assert.doesNotMatch(text, /copyq/i, `${f} mentions third-party clipboard software`)
  }
})

test('apply() registers three model-callable tools', async () => {
  const { apply, name, inject } = await import('../lib/index.js')
  assert.equal(name, 'dsh-copyback')
  assert.deepEqual(inject, ['tools'])
  const registered = []
  apply({ tools: { register: (t) => registered.push(t) } })
  assert.equal(registered.length, 3)
  const names = registered.map((t) => t.name).sort()
  assert.deepEqual(names, ['copyback_copy', 'copyback_manage', 'copyback_search'])
  for (const t of registered) {
    assert.equal(typeof t.execute, 'function', `${t.name} has execute`)
    assert.ok(t.description && t.description.length > 20, `${t.name} has a description`)
    assert.ok(t.description.includes('clipboard'), `${t.name} description mentions clipboard`)
  }
})

test('date filtering keeps only the requested local day', async () => {
  const { filterWithin, dayKey } = await import('../lib/index.js')
  const now = new Date('2026-11-14T12:00:00')
  const mk = (iso) => ({ ts: new Date(`${iso}T10:00:00`).getTime() / 1000 })
  const items = [mk('2026-11-14'), mk('2026-11-13'), mk('2026-11-07'), { ts: 'bad' }]
  assert.equal(dayKey(items[0].ts, now), '2026-11-14')
  assert.equal(dayKey('bad', now), null)
  assert.equal(filterWithin(items, 'today', now).length, 1)
  assert.equal(filterWithin(items, 'yesterday', now).length, 1)
  assert.equal(filterWithin(items, '2026-11-07', now).length, 1)
  assert.equal(filterWithin(items, 'all', now).length, 4)
  assert.equal(filterWithin(items, 'nonsense', now).length, 4, 'unknown token falls back to no filter')
})

test('summarizeItems returns bounded previews with metadata', async () => {
  const { summarizeItems } = await import('../lib/index.js')
  const long = 'x'.repeat(1000)
  const rows = summarizeItems(
    [{ id: 'a1', ts: 1700000000, text: long, source: 'chrome.exe', pinned: true },
     { id: 'b2', ts: 1700000100, text: 'short' }],
    1,
  )
  assert.equal(rows.length, 1, 'respects limit')
  const row = rows[0]
  assert.equal(row.id, 'a1')
  assert.equal(row.preview.length, 400, 'preview is bounded to 400 chars')
  assert.equal(row.chars, 1000)
  assert.equal(row.source, 'chrome.exe')
  assert.equal(row.pinned, true)
  assert.ok(!row.text, 'full text is never returned in summaries')
})
