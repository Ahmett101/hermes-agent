import { useEffect, useRef } from 'react'

import { triggerHaptic } from '@/lib/haptics'

import { type ComposerTarget, getActiveComposer, getMountedComposers } from '../focus'

interface UseComposerEscCancelOptions {
  awaitingInput: boolean
  busy: boolean
  onCancel: () => unknown
  /** This composer's focus-bus key. With N composers mounted (main + tiles),
   *  only the active one's Esc cancels — otherwise every busy tile stops. */
  target: ComposerTarget
}

/**
 * Pick the composer that should receive an Esc-to-cancel press.
 *
 *  - `activeTarget` matches this composer: that's the bus's authoritative
 *    answer, take it.
 *  - `activeTarget` is stale (e.g. the focus tracker points at a composer that
 *    has since unmounted, or never updated because focus never landed on a
 *    composer input): if exactly one mounted composer is currently busy,
 *    that's almost certainly the one the user can see — fire it.
 *  - Ambiguous (multiple busy mounted composers, or none busy): no-op. Better
 *    to leave a runaway tile running than to halt the wrong one.
 *
 *  Pure: pass in the live `getMountedComposers()` snapshot so this function
 *  stays testable without a DOM.
 */
export const resolveEscTarget = (
  target: ComposerTarget,
  activeTarget: ComposerTarget,
  busyByTarget: ReadonlyMap<ComposerTarget, boolean>,
  mounted: ReadonlySet<ComposerTarget>
): ComposerTarget | null => {
  // Authoritative path: the focus bus already points at this composer. The
  // mount check guards against the tracker pointing at an unmounted tile.
  if (activeTarget === target && mounted.has(target)) {
    return target
  }

  // Stale-tracker fallback: if there's exactly one mounted busy composer, that
  // is the one the user can see — even if the focus bus disagrees.
  const busyMounted: ComposerTarget[] = []

  for (const t of mounted) {
    if (busyByTarget.get(t)) {
      busyMounted.push(t)
    }
  }

  if (busyMounted.length === 1) {
    return busyMounted[0]
  }

  return null
}

/**
 * Global Esc-to-cancel: stop the in-flight turn when the CHAT (not the composer
 * input, which has its own handler) has focus — clicking into the transcript and
 * hitting Esc stops the run, matching the Stop button. A latest-handler ref keeps
 * the window listener registered exactly once while still reading fresh
 * busy/awaitingInput/onCancel each press.
 */
export function useComposerEscCancel({ awaitingInput, busy, onCancel, target }: UseComposerEscCancelOptions) {
  // Intentional only: we bail if (a) the composer/another field already handled
  // Esc (defaultPrevented), (b) focus is in any input/textarea/contenteditable
  // (you're typing, not stopping), or (c) a dialog/popover is open — Esc must
  // close that overlay, never double as canceling the stream behind it.
  const escCancelRef = useRef<(event: globalThis.KeyboardEvent) => void>(() => {})

  escCancelRef.current = (event: globalThis.KeyboardEvent) => {
    // `awaitingInput`: the turn is parked on a clarify / approval / sudo / secret
    // prompt, which owns Esc (or is meant to persist) — never cancel the stream
    // out from under it.
    if (event.key !== 'Escape' || event.defaultPrevented || !busy || awaitingInput) {
      return
    }

    // Only the focused composer cancels — otherwise every mounted busy tile
    // stops at once (and the winner would be mount-order arbitrary).
    // `resolveEscTarget` falls back to the lone mounted busy composer when the
    // focus bus is stale (tab switch, mount race, focus never landed), so Esc
    // still works from the transcript on a single-composer chat.
    const busyByTarget = new Map<ComposerTarget, boolean>([[target, busy]])
    const mounted = getMountedComposers()

    // Co-mount any other composers the bus tracks, marked non-busy here: a
    // sibling tile can't be considered busy from this hook, so the helper
    // sees "this composer is busy, others are not". `resolveEscTarget` will
    // still prefer us when `getActiveComposer() === target`, and fall back to
    // us when we're the lone busy mounted composer.
    for (const t of mounted) {
      if (!busyByTarget.has(t)) {
        busyByTarget.set(t, false)
      }
    }

    const resolved = resolveEscTarget(target, getActiveComposer(), busyByTarget, mounted)

    if (resolved !== target) {
      return
    }

    const active = document.activeElement as HTMLElement | null

    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) {
      return
    }

    if (document.querySelector('[role="dialog"],[role="alertdialog"],[data-radix-popper-content-wrapper]')) {
      return
    }

    event.preventDefault()
    triggerHaptic('cancel')
    void Promise.resolve(onCancel())
  }

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => escCancelRef.current(event)
    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])
}
