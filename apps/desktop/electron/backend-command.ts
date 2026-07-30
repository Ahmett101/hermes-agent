// Backend subcommand routing for the desktop-managed Hermes process.
//
// The desktop app launches its own headless backend via `hermes serve` — it
// must NEVER depend on or launch the browser `dashboard`. But `serve` is a
// newer subcommand: a runtime that predates it (an older managed install the
// app hasn't updated yet, or an older `hermes` resolved from PATH) only knows
// `dashboard --no-open`. To avoid bricking those users mid-upgrade we detect
// whether the resolved runtime understands `serve` and, only when it does not,
// fall back to the legacy `dashboard --no-open` invocation. Both produce the
// exact same headless gateway; `serve` is just the decoupled name.
//
// These helpers are pure so they can be unit-tested without Electron.

import { execFile } from 'node:child_process'

/**
 * Build the canonical headless backend argv (always `serve`).
 * @param {string} [profile] optional Hermes profile to pin via `--profile`.
 */
export function serveBackendArgs(profile?: string) {
  const head = profile ? ['--profile', profile] : []

  return [...head, 'serve', '--host', '127.0.0.1', '--port', '0']
}

/**
 * Rewrite a resolved backend argv from `serve` to the legacy
 * `dashboard --no-open` form, preserving every other argument (incl. a leading
 * `-m hermes_cli.main` and any `--profile <name>`). Returns a copy; if there is
 * no `serve` token the argv is returned unchanged.
 */
export function dashboardFallbackArgs(args) {
  const i = args.indexOf('serve')

  if (i === -1) {
    return args.slice()
  }

  return [...args.slice(0, i), 'dashboard', '--no-open', ...args.slice(i + 1)]
}

/**
 * True when a runtime's `hermes_cli/subcommands/dashboard.py` source registers
 * the `serve` subcommand. Matches `add_parser("serve"` / `add_parser('serve'`
 * specifically so the substring "server" (e.g. "start_server", "web server")
 * never produces a false positive.
 */
export function sourceDeclaresServe(dashboardPySource) {
  return /add_parser\(\s*["']serve["']/.test(String(dashboardPySource || ''))
}

/**
 * Probe a resolved backend runtime by spawning `serve --help` and checking the
 * exit code. Resolves to:
 *   - `true`  → the runtime understood `serve` (exit 0)
 *   - `false` → the runtime rejected `serve` (non-zero exit)
 *   - `'timeout'` → the probe exceeded `timeoutMs` (default 8s; we deliberately
 *                  cap below the cold-start 45s floor so the desktop boot path
 *                  doesn't sit on a defensive probe longer than the spawn it is
 *                  trying to qualify — the *spawn* itself still gets the full
 *                  90s deadline once it's running, but a probe here is meant to
 *                  be quick)
 *   - `'error'` → spawn itself failed (ENOENT / EACCES / etc.). Caller should
 *                  treat this like 'timeout': assume the runtime is broken and
 *                  surface the error rather than silently falling through.
 *
 * The probe passes the same `-m hermes_cli.main` prefix as a Python-based
 * backend (mirroring `backend.args`); bare `hermes` runtimes just get the
 * subcommand and rely on the runtime's argv parser.
 *
 * Why non-blocking matters: the previous synchronous version blocked the
 * renderer's boot phase for up to 15s on Windows cold-start + Defender scans,
 * which during the first-launch window could delay the splash by enough that
 * users saw it as a hang (issue #74563). Async keeps the boot moving; the
 * decision is needed *before* spawn, so callers await explicitly when ready.
 */
export type ServeProbeResult = true | false | 'timeout' | 'error'

export interface ServeProbeOptions {
  /** Wall-clock cap for the probe (default 8000ms). */
  timeoutMs?: number
  /** Extra env (merged over process.env). */
  extraEnv?: Record<string, string | undefined>
  /** Override the spawn cwd (default backend.root). */
  cwd?: string
}

export function probeServeSupport(
  backend: { command: string; args?: string[]; root?: string; env?: Record<string, string> },
  options: ServeProbeOptions = {}
): Promise<ServeProbeResult> {
  return new Promise(resolve => {
    if (!backend || !backend.command) {
      resolve('error')

      return
    }

    const timeoutMs = options.timeoutMs ?? 8000
    const prefix =
      backend.args && backend.args[0] === '-m' && backend.args[1] ? backend.args.slice(0, 2) : []
    const child = execFile(
      backend.command,
      [...prefix, 'serve', '--help'],
      {
        cwd: options.cwd ?? backend.root,
        env: { ...process.env, ...(options.extraEnv ?? {}), ...(backend.env ?? {}) },
        timeout: timeoutMs,
        windowsHide: true,
        // Don't share stdio — the desktop already owns a stream to its *real*
        // backend child; we don't want a probe's help text bleeding into that
        // log buffer and confusing the readiness watcher.
        stdio: ['ignore', 'ignore', 'ignore']
      },
      (err) => {
        if (!err) {
          resolve(true)

          return
        }

        // execFile's err.code is 'ENOENT' / 'EACCES' / etc.; the actual exit
        // code lands in err.code when present. node:child_process signals
        // timeout via err.killed && err.signal === 'SIGTERM' with err.code
        // 'ETIMEDOUT' (>= Node 16). err.message also contains the substring
        // 'timed out' in that path.
        if ((err as NodeJS.ErrnoException).code === 'ETIMEDOUT' || /timed out/i.test(err.message)) {
          resolve('timeout')

          return
        }

        // Spawn itself failed (ENOENT) vs runtime exited non-zero. The former
        // is a broken resolver candidate; the latter is "no serve subcommand"
        // and the right thing to fall back to dashboard --no-open. Callers
        // that just want a yes/no can treat both as false; callers that need
        // to surface the distinction (logging / metrics) inspect the result.
        if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
          resolve('error')

          return
        }

        resolve(false)
      }
    )

    // execFile returns a ChildProcess but we don't need a handle here — the
    // callback path always fires (timeout/error/exit). Defensive: if some
    // platform path leaves the callback dangling, kill the child so we don't
    // leak processes between probe attempts.
    if (child && typeof child.unref === 'function') {
      child.unref()
    }
  })
}
