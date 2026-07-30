import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  dashboardFallbackArgs,
  probeServeSupport,
  serveBackendArgs,
  sourceDeclaresServe
} from './backend-command'

test('serveBackendArgs builds a headless serve invocation', () => {
  assert.deepEqual(serveBackendArgs(), ['serve', '--host', '127.0.0.1', '--port', '0'])
})

test('serveBackendArgs pins a profile when provided', () => {
  assert.deepEqual(serveBackendArgs('worker'), ['--profile', 'worker', 'serve', '--host', '127.0.0.1', '--port', '0'])
})

test('dashboardFallbackArgs rewrites serve -> dashboard --no-open, keeping the -m prefix', () => {
  const serve = ['-m', 'hermes_cli.main', 'serve', '--host', '127.0.0.1', '--port', '0']
  assert.deepEqual(dashboardFallbackArgs(serve), [
    '-m',
    'hermes_cli.main',
    'dashboard',
    '--no-open',
    '--host',
    '127.0.0.1',
    '--port',
    '0'
  ])
})

test('dashboardFallbackArgs preserves a --profile flag ahead of serve', () => {
  const serve = ['-m', 'hermes_cli.main', '--profile', 'worker', 'serve', '--host', '127.0.0.1', '--port', '0']
  assert.deepEqual(dashboardFallbackArgs(serve), [
    '-m',
    'hermes_cli.main',
    '--profile',
    'worker',
    'dashboard',
    '--no-open',
    '--host',
    '127.0.0.1',
    '--port',
    '0'
  ])
})

test('dashboardFallbackArgs is a no-op (copy) when there is no serve token', () => {
  const args = ['-m', 'hermes_cli.main', 'dashboard', '--no-open']
  const out = dashboardFallbackArgs(args)
  assert.deepEqual(out, args)
  assert.notEqual(out, args, 'should return a copy, not the same reference')
})

test('sourceDeclaresServe detects the serve subparser registration', () => {
  assert.equal(sourceDeclaresServe('subparsers.add_parser("serve", help="...")'), true)
  assert.equal(sourceDeclaresServe("subparsers.add_parser('serve')"), true)
  assert.equal(sourceDeclaresServe('subparsers.add_parser(\n        "serve",\n)'), true)
})

test('sourceDeclaresServe does not false-positive on the substring "server"', () => {
  const oldSource = `
    dashboard_parser = subparsers.add_parser("dashboard", help="Start the web UI dashboard")
    from hermes_cli.web_server import start_server  # web server
  `

  assert.equal(sourceDeclaresServe(oldSource), false)
})

// ---------------------------------------------------------------------------
// probeServeSupport — exercises the execFile-based runtime probe. We spawn
// node directly (always present on the dev box and CI) so the tests don't
// depend on a real Hermes runtime being installed. Two paths matter:
//   - exit 0  → the runtime understood `serve --help` (here: we fake it by
//               passing a script that just exits cleanly)
//   - non-0   → the runtime rejected `serve` (fallback to dashboard --no-open)
//   - timeout → the probe exceeded `timeoutMs`
//   - error   → spawn itself failed (ENOENT)
// ---------------------------------------------------------------------------

import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

function writeHelperScript(body: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'hermes-probe-test-'))
  const file = join(dir, process.platform === 'win32' ? 'helper.cmd' : 'helper.sh')

  if (process.platform === 'win32') {
    // .cmd shim — process.exec path on Windows rejects a `.sh` extension.
    writeFileSync(file, `@echo off\r\n${body}\rnexit /b ${body.includes('exit 99') ? 99 : 0}\r\n`)
  } else {
    writeFileSync(file, `#!/bin/sh\n${body}\n`)
  }

  return file
}

test('probeServeSupport resolves true when the runtime exits 0', async () => {
  const helper = writeHelperScript('exit 0')
  const result = await probeServeSupport({ command: helper }, { timeoutMs: 5000 })

  assert.equal(result, true)
})

test('probeServeSupport resolves false when the runtime exits non-zero', async () => {
  const helper = writeHelperScript('exit 99')
  const result = await probeServeSupport({ command: helper }, { timeoutMs: 5000 })

  assert.equal(result, false)
})

test('probeServeSupport resolves "timeout" when the runtime exceeds timeoutMs', async () => {
  // sleep > the timeout we hand in
  const body = process.platform === 'win32' ? 'timeout /t 5 /nobreak >NUL' : 'sleep 2'
  const helper = writeHelperScript(body)
  const result = await probeServeSupport({ command: helper }, { timeoutMs: 200 })

  assert.equal(result, 'timeout')
})

test('probeServeSupport resolves "error" when the command does not exist', async () => {
  const result = await probeServeSupport(
    { command: '/this/path/definitely/does/not/exist/hermes-probe-test' },
    { timeoutMs: 1000 }
  )

  assert.equal(result, 'error')
})

test('probeServeSupport resolves "error" when the backend has no command', async () => {
  // The signature accepts an empty backend as "no resolved runtime".
  const result = await probeServeSupport({ command: '' }, { timeoutMs: 1000 })

  assert.equal(result, 'error')
})

test('probeServeSupport includes a -m prefix when the backend resolves a Python module', async () => {
  // The helper script ignores its arguments; we just check that argv[0] got
  // the `-m hermes_cli.main` prefix when the backend uses -m invocation.
  // This is a structural guard — we read it back by writing a helper that
  // echoes $@ to a side file and reading that file after the probe completes.
  const dir = mkdtempSync(join(tmpdir(), 'hermes-probe-args-'))
  const argsFile = join(dir, 'argv.txt')
  const helper = process.platform === 'win32' ? join(dir, 'helper.cmd') : join(dir, 'helper.sh')

  if (process.platform === 'win32') {
    writeFileSync(helper, `@echo off\r\necho %* > "${argsFile}"\r\nexit /b 0\r\n`)
  } else {
    writeFileSync(helper, `#!/bin/sh\necho "$@" > "${argsFile}"\nexit 0\n`)
  }

  await probeServeSupport(
    { command: helper, args: ['-m', 'hermes_cli.main', 'serve', '--host', '127.0.0.1', '--port', '0'] },
    { timeoutMs: 5000 }
  )

  // The argv file is written by the helper; give it a tick to flush on
  // platforms where fs sync is lazy (Windows Defender-real-time, WSL2 bridge).
  await new Promise(r => setTimeout(r, 100))

  const fs = await import('node:fs')
  const echoed = fs.existsSync(argsFile) ? fs.readFileSync(argsFile, 'utf8').trim() : ''

  // The Python -m prefix should have been included so the runtime can resolve
  // the hermes_cli module even when the desktop spawns a bare `python.exe`.
  assert.ok(
    echoed.includes('-m'),
    `expected the probe argv to include the -m prefix, got: ${JSON.stringify(echoed)}`
  )
})
