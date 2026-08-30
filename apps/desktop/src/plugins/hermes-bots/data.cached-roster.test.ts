/**
 * `cachedUnionRoster` is the imperative roster read — the @mention popover
 * must answer per keystroke and the composer middleware runs on submit, so
 * neither can wait on the hook.
 *
 * `useRoster` keys its query on `[...ROSTER_KEY, connectionId, mode, connectionKey]`, one entry
 * per live route. Reading it back with the BARE key is an
 * exact-key match in TanStack Query and therefore matches NOTHING — the
 * regression where completions offered no handles and remote `@name-device`
 * mentions passed through unresolved. The imperative read must use the same
 * exact key and must never borrow a roster from another route.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { cache, connection } = vi.hoisted(() => ({
  cache: new Map<string, { key: unknown[]; value: unknown }>(),
  connection: {
    id: 'local' as null | string,
    key: 'connection:local' as null | string,
    mode: 'local' as null | 'local' | 'remote'
  }
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')
  const keyOf = (key: unknown[]) => JSON.stringify(key)

  return {
    atom,
    host: {
      state: {
        connectionId: { get: () => connection.id },
        connectionKey: { get: () => connection.key },
        connectionMode: { get: () => connection.mode },
        profile: { get: () => 'default' }
      }
    },
    queryClient: {
      getQueriesData: ({ queryKey }: { queryKey: unknown[] }) =>
        [...cache.values()]
          .filter(entry => queryKey.every((part, index) => entry.key[index] === part))
          .map(entry => [entry.key, entry.value]),
      getQueryData: (key: unknown[]) => cache.get(keyOf(key))?.value,
      invalidateQueries: vi.fn(),
      setQueryData: (key: unknown[], value: unknown) => cache.set(keyOf(key), { key, value })
    },
    useQuery: vi.fn(),
    useValue: vi.fn()
  }
})

vi.mock('./shared', () => ({ getPluginCtx: () => null, ID: 'hermes-bots' }))

const seed = (key: unknown[], value: unknown) => cache.set(JSON.stringify(key), { key, value })

beforeEach(() => {
  cache.clear()
  connection.id = 'local'
  connection.key = 'connection:local'
  connection.mode = 'local'
})

describe('cachedUnionRoster', () => {
  it('reads the entry useRoster wrote under the connection-suffixed key', async () => {
    const { cachedUnionRoster } = await import('./data')

    seed(['hermes-bots', 'roster', 'local', 'local', 'connection:local'], { profiles: [{ name: 'default' }] })

    expect(cachedUnionRoster()?.profiles).toHaveLength(1)
    // The bare key is what the broken read used — it must still miss, or this
    // test would pass for the wrong reason.
    expect(cache.has(JSON.stringify(['hermes-bots', 'roster']))).toBe(false)
  })

  it('does not borrow another connection\'s roster while the live cache is cold', async () => {
    const { cachedUnionRoster } = await import('./data')

    seed(['hermes-bots', 'roster', 'vera', 'remote', 'connection:vera'], {
      fetchedAt: 9_000,
      profiles: [{ connectionId: 'vera', name: 'default' }]
    })
    connection.id = 'local'
    connection.mode = 'local'

    expect(cachedUnionRoster()).toBeNull()
  })

  it('keeps local and remote caches separate even when the connection id is the same', async () => {
    const { cachedUnionRoster } = await import('./data')

    seed(['hermes-bots', 'roster', 'local', 'local', 'connection:local'], {
      profiles: [{ name: 'this-device' }]
    })
    seed(['hermes-bots', 'roster', 'local', 'remote', 'remote:ssh:local'], {
      profiles: [{ name: 'remote-primary' }]
    })

    expect(cachedUnionRoster()?.profiles?.[0]).toMatchObject({ name: 'this-device' })
  })

  it('reports nothing rather than throwing on a cold cache', async () => {
    const { cachedUnionRoster } = await import('./data')

    expect(cachedUnionRoster()).toBeNull()
  })

  it('does not borrow a different idless remote with the same mode and empty id', async () => {
    const { cachedUnionRoster } = await import('./data')

    connection.id = null
    connection.mode = 'remote'
    connection.key = 'remote:ssh:host-b'
    seed(['hermes-bots', 'roster', null, 'remote', 'remote:ssh:host-a'], {
      profiles: [{ name: 'host-a' }]
    })

    expect(cachedUnionRoster()).toBeNull()
  })
})
