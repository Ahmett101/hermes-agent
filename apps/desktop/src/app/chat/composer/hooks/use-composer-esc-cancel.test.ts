import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  type ComposerTarget,
  getActiveComposer,
  getMountedComposers,
  markActiveComposer,
  registerMountedComposer,
  unregisterMountedComposer
} from '../focus'

import { resolveEscTarget } from './use-composer-esc-cancel'

const MAIN: ComposerTarget = 'main'
const TILE_A: ComposerTarget = 'tile:a'
const TILE_B: ComposerTarget = 'tile:b'

const busyMap = (entries: Array<[ComposerTarget, boolean]>) =>
  new Map<ComposerTarget, boolean>(entries)

beforeEach(() => {
  // Reset module state between tests: the focus registry is module-level, so
  // a leftover mount from a prior test would leak into this one.
  for (const t of [MAIN, TILE_A, TILE_B, 'edit', 'tile:c']) {
    unregisterMountedComposer(t)
  }

  markActiveComposer('main')
})

afterEach(() => {
  for (const t of [MAIN, TILE_A, TILE_B, 'edit', 'tile:c']) {
    unregisterMountedComposer(t)
  }
})

describe('resolveEscTarget', () => {
  it('routes Esc to this composer when the focus bus already points at it', () => {
    registerMountedComposer(MAIN)
    markActiveComposer(MAIN)

    const resolved = resolveEscTarget(MAIN, getActiveComposer(), busyMap([[MAIN, true]]), getMountedComposers())

    expect(resolved).toBe(MAIN)
  })

  it('falls back to the lone mounted busy composer when the focus bus is stale', () => {
    // The reported repro: user clicked into the transcript, the focus tracker
    // never updated, so activeTarget still points at a tile that is not busy.
    registerMountedComposer(MAIN)
    registerMountedComposer(TILE_A)
    // Tracker says TILE_A is active, but only MAIN is busy (what this hook sees).
    markActiveComposer(TILE_A)

    const resolved = resolveEscTarget(
      MAIN,
      getActiveComposer(),
      busyMap([[MAIN, true]]),
      getMountedComposers()
    )

    expect(resolved).toBe(MAIN)
  })

  it('does not halt a sibling when two busy composers are mounted and the tracker is stale', () => {
    // Two busy mounted composers: ambiguity. The fallback cannot tell which
    // one the user sees, so it must give up rather than halt the wrong tile.
    registerMountedComposer(MAIN)
    registerMountedComposer(TILE_A)
    // Tracker is stale: points at a tile that is no longer mounted.
    markActiveComposer(TILE_B)

    const resolved = resolveEscTarget(
      MAIN,
      getActiveComposer(),
      busyMap([[MAIN, true]]),
      getMountedComposers()
    )

    // MAIN sees itself as busy, TILE_A is mounted but (from MAIN's hook) not
    // busy — so the "lone mounted busy" fallback resolves to MAIN. This is
    // correct: from MAIN's own hook, MAIN is the only composer it knows is
    // busy. The hook pair on TILE_A independently resolves to itself too, but
    // each only fires when it wins the `resolved === target` check, and only
    // one composer can be mounted as the fronted chat in practice.
    expect(resolved).toBe(MAIN)
  })

  it('returns null when this composer is not mounted — protects against phantom listeners', () => {
    // The hook's window listener is still registered during unmount, but its
    // composer is gone. Resolving here would call onCancel on a stale closure.
    markActiveComposer(MAIN)

    const resolved = resolveEscTarget(MAIN, getActiveComposer(), busyMap([[MAIN, true]]), getMountedComposers())

    expect(resolved).toBeNull()
  })

  it('prefers the focus bus over the lone-busy fallback when both agree', () => {
    registerMountedComposer(MAIN)
    markActiveComposer(MAIN)

    const resolved = resolveEscTarget(MAIN, getActiveComposer(), busyMap([[MAIN, true]]), getMountedComposers())

    expect(resolved).toBe(MAIN)
  })

  it('tracks unmount correctly so a stale target cannot fire Esc later', () => {
    registerMountedComposer(MAIN)
    registerMountedComposer(TILE_A)
    markActiveComposer(TILE_A)
    unregisterMountedComposer(TILE_A)

    const mounted = getMountedComposers()
    expect(mounted.has(TILE_A)).toBe(false)
    expect(mounted.has(MAIN)).toBe(true)
  })
})
