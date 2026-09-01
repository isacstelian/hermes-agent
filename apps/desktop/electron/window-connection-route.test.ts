import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  activeRosterConnectionId,
  activeRosterSshState,
  normalizeWindowConnectionRoute,
  registrySshScopeForWindowRoute,
  WindowConnectionRouteRegistry
} from './window-connection-route'

test('normalizes an exact registry-scoped connection and profile', () => {
  assert.deepEqual(
    normalizeWindowConnectionRoute({
      connectionId: 'source-b',
      profile: 'research',
      registryScoped: true
    }),
    {
      connectionId: 'source-b',
      profile: 'research',
      registryScoped: true
    }
  )
})

test('keeps legacy/profile-only routes distinct from registry identities', () => {
  assert.deepEqual(normalizeWindowConnectionRoute({ profile: 'work' }), {
    connectionId: null,
    profile: 'work',
    registryScoped: false
  })
})

test('preserves a registry connection when no profile is selected', () => {
  assert.deepEqual(
    normalizeWindowConnectionRoute({
      connectionId: 'source-b',
      registryScoped: true
    }),
    {
      connectionId: 'source-b',
      profile: undefined,
      registryScoped: true
    }
  )
})

test('isolates active routes by webContents id', () => {
  const routes = new WindowConnectionRouteRegistry()

  routes.set(11, { connectionId: 'source-a', profile: 'default', registryScoped: true })
  routes.set(22, { connectionId: 'source-b', profile: 'worker', registryScoped: true })

  assert.equal(routes.get(11)?.connectionId, 'source-a')
  assert.equal(routes.get(22)?.connectionId, 'source-b')

  routes.delete(11)

  assert.equal(routes.get(11), null)
  assert.equal(routes.get(22)?.connectionId, 'source-b')
})

test('invalid publications clear only the sender route', () => {
  const routes = new WindowConnectionRouteRegistry()

  routes.set(11, { connectionId: 'source-a', profile: 'default', registryScoped: true })
  routes.set(22, { connectionId: 'source-b', profile: 'worker', registryScoped: true })

  routes.set(11, null)

  assert.equal(routes.get(11), null)
  assert.equal(routes.get(22)?.connectionId, 'source-b')
})

test('routes a non-primary SSH connection independently from another window', () => {
  const registry = {
    primary: 'source-a',
    connections: [
      { id: 'source-a', kind: 'ssh' },
      { id: 'source-b', kind: 'ssh' },
      { id: 'source-c', kind: 'remote' }
    ]
  } as never

  const routes = new WindowConnectionRouteRegistry()

  routes.set(11, {
    connectionId: 'source-b',
    profile: 'worker',
    registryScoped: true
  })
  routes.set(22, {
    connectionId: 'source-c',
    profile: 'default',
    registryScoped: true
  })

  assert.equal(registrySshScopeForWindowRoute(routes.get(11), registry), 'conn:source-b::worker')
  assert.equal(registrySshScopeForWindowRoute(routes.get(22), registry), null)
})

test('uses the canonical default profile scope when a registry SSH route has no profile', () => {
  const registry = {
    primary: 'source-a',
    connections: [
      { id: 'source-a', kind: 'ssh' },
      { id: 'source-b', kind: 'ssh' }
    ]
  } as never

  assert.equal(
    registrySshScopeForWindowRoute(
      {
        connectionId: 'source-b',
        profile: undefined,
        registryScoped: true
      },
      registry
    ),
    'conn:source-b::default'
  )
})

test('reports the exact registry-scoped owner of a window roster', () => {
  assert.equal(
    activeRosterConnectionId(
      { connectionId: 'source-b', profile: 'default', registryScoped: true },
      [{ id: 'source-a' }, { id: 'source-b' }],
      { registryConnectionId: 'source-a' }
    ),
    'source-b'
  )
})

test('keeps a live legacy SSH owner after Settings changes registry primary', () => {
  assert.equal(
    activeRosterConnectionId(
      { connectionId: null, profile: 'default', registryScoped: false },
      [{ id: 'active-old' }, { id: 'new-primary' }],
      { registryConnectionId: 'active-old' }
    ),
    'active-old'
  )
})

test('does not publish a retired route identity', () => {
  assert.equal(activeRosterConnectionId(null, [{ id: 'new-primary' }], { registryConnectionId: 'active-old' }), null)
})

test('finds a global primary SSH state published at the empty scope', () => {
  const state = { registryConnectionId: 'active-old' }
  const published = new Map([['', state]])

  assert.equal(
    activeRosterSshState(
      { connectionId: null, profile: 'default', registryScoped: false },
      'default',
      published
    ),
    state
  )
})

test('does not borrow the global primary state for a different profile', () => {
  const published = new Map([['', { registryConnectionId: 'primary' }]])

  assert.equal(
    activeRosterSshState(
      { connectionId: null, profile: 'research', registryScoped: false },
      'default',
      published
    ),
    null
  )
})
