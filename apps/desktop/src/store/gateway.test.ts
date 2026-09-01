import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RECONNECT_ATTEMPT_TIMEOUT_MS, SECONDARY_BACKEND_BOOT_WAIT_TIMEOUT_MS } from '@/lib/with-timeout'
import { $connectionsRegistry } from '@/store/connection-registry-state'

// Connection lifecycle for registry-scoped secondary gateways:
//
//  1. Removing a connection must dispose its secondaries — remote/cloud
//     sources have no local process whose death would drop the socket, so
//     without an explicit dispose the WebSocket stays open and streams ghost
//     events until page reload.
//  2. A materially edited connection re-dials so fresh sockets target the
//     NEW endpoint.
//  3. When the Electron main reports the connection no longer exists
//     (`No connection with id`), the reconnect loop fail-stops and evicts
//     the entry instead of retrying forever.

const gatewayMocks = vi.hoisted(() => {
  const instances: { close: ReturnType<typeof vi.fn>; connectionState: string }[] = []

  return {
    connect: vi.fn(async (_wsUrl: string): Promise<void> => undefined),
    instances
  }
})

vi.mock('@/hermes', () => ({
  setApiRequestConnection: vi.fn(),
  HermesGateway: class {
    connectionState = 'closed'
    close = vi.fn(() => {
      this.connectionState = 'closed'
    })
    connect = async (wsUrl: string): Promise<void> => {
      await gatewayMocks.connect(wsUrl)
      this.connectionState = 'open'
    }
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
    constructor() {
      gatewayMocks.instances.push(this as never)
    }
  }
}))
vi.mock('@/store/session', () => ({
  setConnection: vi.fn(),
  setGatewayState: vi.fn()
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const {
  activeGateway,
  closeSecondaryGateways,
  configureGatewayRegistry,
  ensureGatewayForProfile,
  openGatewayForAgent,
  pruneSecondaryGateways,
  setPrimaryGateway
} = await import('./gateway')

function installDesktop(stub: Record<string, unknown>): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = stub
}

beforeEach(() => {
  configureGatewayRegistry({ onEvent: vi.fn() } as never)
  setPrimaryGateway({ connectionState: 'open' } as never, 'default')
})

afterEach(() => {
  closeSecondaryGateways()
  $connectionsRegistry.set(null)
  gatewayMocks.instances.length = 0
  vi.clearAllMocks()
  vi.useRealTimers()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('ensureGatewayForProfile — secondary connect failure surfaces (#81094)', () => {
  it('rethrows the dial failure instead of activating a closed socket', async () => {
    const getConnection = vi.fn(async ({ profile }: { profile: string }) => ({
      authMode: 'token',
      baseUrl: `https://${profile}.invalid`,
      mode: 'local',
      profile,
      token: 'fake-test-token',
      wsUrl: `wss://${profile}.invalid/ws`
    }))

    installDesktop({ getConnection })

    // First activation succeeds so the entry exists.
    await ensureGatewayForProfile('work')

    const live = activeGateway()

    expect(live).toBeTruthy()

    // The socket then dies (backend restart): state flips to closed, so the
    // next activation must re-dial instead of reusing the dead socket.
    ;(live as unknown as { connectionState: string }).connectionState = 'closed'
    gatewayMocks.connect.mockRejectedValue(new Error('backend unreachable'))

    await expect(ensureGatewayForProfile('work')).rejects.toThrow('backend unreachable')

    // The failed switch must NOT fall through to setActive() with a closed
    // socket: the active gateway is still the previously-live one, never the
    // dead entry that just failed to dial.
    const stillActive = activeGateway()

    expect(stillActive).toBe(live)
    expect(gatewayMocks.instances).toHaveLength(1)
  })

  it('releases the activation lease when the first dial is rejected so pruning disposes it', async () => {
    const getConnection = vi.fn(async ({ profile }: { profile: string }) => ({
      authMode: 'token',
      baseUrl: `https://${profile}.invalid`,
      mode: 'local',
      profile,
      token: 'fake-test-token',
      wsUrl: `wss://${profile}.invalid/ws`
    }))

    installDesktop({ getConnection })
    gatewayMocks.connect.mockRejectedValue(new Error('backend unreachable'))

    await expect(ensureGatewayForProfile('work')).rejects.toThrow('backend unreachable')

    pruneSecondaryGateways(new Set())

    expect(gatewayMocks.instances[0].close).toHaveBeenCalledTimes(1)
  })

  it('keeps the short descriptor budget for a recreated non-SSH secondary', async () => {
    vi.useFakeTimers()

    let redial = false
    let redialCalls = 0

    const getConnection = vi.fn((profile: string) => {
      if (redial) {
        redialCalls += 1

        if (redialCalls === 1) {
          return Promise.resolve({ sharedPrimary: false })
        }

        return new Promise(() => undefined)
      }

      return Promise.resolve({
        authMode: 'token',
        baseUrl: `https://${profile}.invalid`,
        mode: 'local',
        profile,
        token: 'fake-test-token',
        wsUrl: `wss://${profile}.invalid/ws`
      })
    })

    installDesktop({ getConnection })
    gatewayMocks.connect.mockImplementation(async () => undefined)
    await ensureGatewayForProfile('work')
    await ensureGatewayForProfile('default')
    pruneSecondaryGateways(new Set())

    redial = true
    let settled = false
    let failure: unknown = null

    const pending = ensureGatewayForProfile('work').catch(error => {
      failure = error
      settled = true
    })

    await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS - 1)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(1)
    await pending
    expect(failure).toMatchObject({ message: 'Timed out connecting to profile "work"' })
  })

  it('keeps the short timeout for a reconnect on the same secondary', async () => {
    vi.useFakeTimers()

    let reconnect = false
    let reconnectCalls = 0

    const getConnection = vi.fn((profile: string) => {
      if (reconnect) {
        reconnectCalls += 1

        if (reconnectCalls === 1) {
          return Promise.resolve({ sharedPrimary: false })
        }

        return new Promise(() => undefined)
      }

      return Promise.resolve({
        authMode: 'token',
        baseUrl: `https://${profile}.invalid`,
        mode: 'local',
        profile,
        token: 'fake-test-token',
        wsUrl: `wss://${profile}.invalid/ws`
      })
    })

    installDesktop({ getConnection })
    gatewayMocks.connect.mockImplementation(async () => undefined)
    await ensureGatewayForProfile('work')
    ;(activeGateway() as unknown as { connectionState: string }).connectionState = 'closed'

    reconnect = true
    let settled = false
    let failure: unknown = null

    const pending = ensureGatewayForProfile('work').catch(error => {
      failure = error
      settled = true
    })

    await vi.advanceTimersByTimeAsync(19_999)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(1)
    await pending
    expect(failure).toMatchObject({ message: 'Timed out connecting to profile "work"' })
  })

  it('does not globally cancel a shared SSH bootstrap when one renderer reconnect times out', async () => {
    vi.useFakeTimers()
    $connectionsRegistry.set({
      connections: [{ id: 'imac', kind: 'ssh', label: 'iMac' }],
      lastUsed: 'imac',
      primary: 'imac'
    } as never)

    let reconnect = false
    const cancelBootstrap = vi.fn(async () => ({ cancelled: true, ok: true }))

    const getConnectionFor = vi.fn(() =>
      reconnect
        ? new Promise(() => undefined)
        : Promise.resolve({
            authMode: 'token',
            baseUrl: 'https://imac.invalid',
            connectionId: 'imac',
            mode: 'remote',
            profile: 'cmo',
            token: 'fake-test-token',
            wsUrl: 'wss://imac.invalid/ws'
          })
    )

    installDesktop({ connections: { cancelBootstrap }, getConnectionFor })
    await openGatewayForAgent('imac', 'cmo')
    ;(gatewayMocks.instances[0] as { connectionState: string }).connectionState = 'closed'
    reconnect = true

    const pending = expect(openGatewayForAgent('imac', 'cmo')).rejects.toThrow(
      'Timed out connecting to profile "cmo"'
    )

    await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS)
    await pending

    expect(cancelBootstrap).not.toHaveBeenCalled()
  })

  it('keeps the reconnect schedule armed so transient failures still self-heal', async () => {
    vi.useFakeTimers()

    let failFirst = true

    const getConnection = vi.fn(async ({ profile }: { profile: string }) => ({
      authMode: 'token',
      baseUrl: `https://${profile}.invalid`,
      mode: 'local',
      profile,
      token: 'fake-test-token',
      wsUrl: `wss://${profile}.invalid/ws`
    }))

    installDesktop({ getConnection })

    gatewayMocks.connect.mockImplementation(async () => {
      if (failFirst) {
        throw new Error('backend unreachable')
      }
    })

    await expect(ensureGatewayForProfile('work')).rejects.toThrow('backend unreachable')

    // The catch kept the reconnect schedule: exactly one backoff timer is armed
    // for the failed entry (transient failures still self-heal).
    expect(vi.getTimerCount()).toBe(1)

    // Backoff fires → reconnect dials again → succeeds → socket opens.
    failFirst = false
    await vi.runAllTimersAsync()
    expect(gatewayMocks.instances[0].connectionState).toBe('open')
  })

  it('activates the secondary when connect succeeds', async () => {
    const getConnection = vi.fn(async ({ profile }: { profile: string }) => ({
      authMode: 'token',
      baseUrl: `https://${profile}.invalid`,
      mode: 'local',
      profile,
      token: 'fake-test-token',
      wsUrl: `wss://${profile}.invalid/ws`
    }))

    installDesktop({ getConnection })

    await ensureGatewayForProfile('work')

    expect(activeGateway()).toBe(gatewayMocks.instances[0])
  })
})

describe('connection-scoped dial failure identity (#95421)', () => {
  it('logs the route scope while preserving the original dial error', async () => {
    const dialError = new Error('backend unreachable')

    const getConnectionFor = vi.fn(async ({ connectionId, profile }: { connectionId: string; profile: string }) => ({
      authMode: 'token',
      connectionId,
      profile,
      wsUrl: `wss://${connectionId}.invalid/ws`
    }))

    installDesktop({ getConnectionFor })
    gatewayMocks.connect.mockRejectedValue(dialError)

    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    try {
      await expect(openGatewayForAgent('work', 'default')).rejects.toBe(dialError)
      await expect(openGatewayForAgent('homelab', 'default')).rejects.toBe(dialError)

      const messages = errorSpy.mock.calls.map(([message]) => String(message))

      expect(messages).toHaveLength(2)
      expect(messages).toEqual(
        expect.arrayContaining([
          expect.stringContaining('scope="conn:work::default"'),
          expect.stringContaining('scope="conn:homelab::default"')
        ])
      )
      expect(messages.every(message => message.includes('profile="default"'))).toBe(true)
      expect(new Set(messages).size).toBe(2)
      expect(messages.join(' ')).not.toContain('wss://')

      for (const [, error] of errorSpy.mock.calls) {
        expect(error).toBe(dialError)
      }
    } finally {
      errorSpy.mockRestore()
    }
  })
})

describe('profile switch mid-WS-handshake (#92434 close-candidate pin)', () => {
  // Reported shape: Bot ↔ Default switching killed the socket until an app
  // restart. The activation-epoch guard (applyActive) + open-socket-publish
  // rule mean a switch-back that lands while the outgoing switch's handshake
  // is still pending must win the route, and the late-completing dial must
  // neither steal the foreground nor leave its socket permanently broken.
  it('a switch-back during a pending handshake wins; the late dial neither steals the route nor breaks the socket', async () => {
    const getConnection = vi.fn(async ({ profile }: { profile: string }) => ({
      authMode: 'token',
      baseUrl: `https://${profile}.invalid`,
      mode: 'local',
      profile,
      token: 'fake-test-token',
      wsUrl: `wss://${profile}.invalid/ws`
    }))

    installDesktop({ getConnection })

    let releaseDial: () => void = () => undefined

    gatewayMocks.connect.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          releaseDial = resolve
        })
    )

    // 1. Default → Bot: the secondary's WS handshake starts and stays pending.
    const botActivation = ensureGatewayForProfile('bot')

    await vi.waitFor(() => expect(gatewayMocks.connect).toHaveBeenCalledTimes(1))

    // 2. The user switches back to Default while that handshake is mid-flight.
    await ensureGatewayForProfile('default')

    const primary = activeGateway()

    expect(primary).toBeTruthy()

    // 3. The Bot handshake completes AFTER the switch-back.
    releaseDial()
    await botActivation

    // The stale activation must not steal the foreground route (epoch guard).
    expect(activeGateway()).toBe(primary)

    // 4. No permanent break: switching to Bot again activates the (already
    // open) socket — no app restart, no duplicate socket/serve.
    await ensureGatewayForProfile('bot')

    expect(activeGateway()).toBe(gatewayMocks.instances[0])
    expect(gatewayMocks.instances[0].connectionState).toBe('open')
    expect(gatewayMocks.instances).toHaveLength(1)
  })
})

describe('secondary connection timeout (#93454)', () => {
  it("rejects instead of hanging forever when openSecondary's getConnection() wedges", async () => {
    // Repro: desktop.getConnection is an IPC round-trip into the main process
    // with no timeout of its own. A wedged main-process round-trip (e.g. a
    // stuck revalidation) hangs this await forever, latching
    // entry.connectPromise so every routed action against this secondary
    // (SSH terminal, messaging DELETE, session send, …) never settles either.
    vi.useFakeTimers()

    let callCount = 0

    const getConnection = vi.fn(({ profile }: { profile: string }) => {
      callCount += 1

      // First call is sharedPrimaryRoute's probe — resolves fast, not the
      // shared primary. Every call after (openSecondary's actual dial) wedges.
      if (callCount === 1) {
        return Promise.resolve({ sharedPrimary: false })
      }

      return new Promise(() => undefined)
    })

    installDesktop({ getConnection })

    const connection = ensureGatewayForProfile('work')
    let settled = false
    const pending = expect(connection).rejects.toThrow('Timed out connecting to profile "work"')

    void connection.then(
      () => {
        settled = true
      },
      () => {
        settled = true
      }
    )

    // Local, URL, and cloud routes retain the original short IPC bound. Only a
    // registry SSH cold boot gets the longer queue-aware budget.
    await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS - 1)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(1)
    await pending
  })

  it('keeps the longer cold descriptor budget only for registry SSH routes', async () => {
    vi.useFakeTimers()
    $connectionsRegistry.set({
      connections: [{ id: 'imac', kind: 'ssh', label: 'iMac' }],
      lastUsed: 'imac',
      primary: 'imac'
    } as never)

    const cancelBootstrap = vi.fn(async () => ({ cancelled: true, ok: true }))
    const getConnectionFor = vi.fn(() => new Promise(() => undefined))
    installDesktop({ connections: { cancelBootstrap }, getConnectionFor })

    const connection = openGatewayForAgent('imac', 'cmo')
    let settled = false
    const pending = expect(connection).rejects.toThrow('Timed out connecting to profile "cmo"')

    void connection.then(
      () => {
        settled = true
      },
      () => {
        settled = true
      }
    )

    await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(
      SECONDARY_BACKEND_BOOT_WAIT_TIMEOUT_MS - RECONNECT_ATTEMPT_TIMEOUT_MS - 1
    )
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(1)
    await pending
    expect(cancelBootstrap).toHaveBeenCalledWith({ connectionId: 'imac', profile: 'cmo' })
  })

  it('loads the registry kind before choosing the cold SSH budget', async () => {
    vi.useFakeTimers()
    $connectionsRegistry.set(null)

    const getConnectionFor = vi.fn(() => new Promise(() => undefined))

    const list = vi.fn(async () => ({
      connections: [{ id: 'imac', kind: 'ssh', label: 'iMac' }],
      primary: 'imac'
    }))

    installDesktop({ connections: { list }, getConnectionFor })

    const connection = openGatewayForAgent('imac', 'cmo')
    let settled = false
    const pending = expect(connection).rejects.toThrow('Timed out connecting to profile "cmo"')

    void connection.then(
      () => {
        settled = true
      },
      () => {
        settled = true
      }
    )

    await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(SECONDARY_BACKEND_BOOT_WAIT_TIMEOUT_MS - RECONNECT_ATTEMPT_TIMEOUT_MS)
    await pending
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('observes a fast descriptor rejection while the registry kind is loading', async () => {
    $connectionsRegistry.set(null)

    let resolveRegistry!: (registry: unknown) => void
    const descriptorFailure = new Error('SSH bootstrap failed')
    const getConnectionFor = vi.fn(() => Promise.reject(descriptorFailure))

    const list = vi.fn(
      () =>
        new Promise(resolve => {
          resolveRegistry = resolve
        })
    )

    const unhandled: unknown[] = []

    const onUnhandled = (reason: unknown): void => {
      unhandled.push(reason)
    }

    process.on('unhandledRejection', onUnhandled)
    installDesktop({ connections: { list }, getConnectionFor })

    try {
      const connection = openGatewayForAgent('imac', 'cmo')

      await new Promise(resolve => setTimeout(resolve, 0))
      expect(unhandled).toEqual([])

      resolveRegistry({
        connections: [{ id: 'imac', kind: 'ssh', label: 'iMac' }],
        primary: 'imac'
      })

      await expect(connection).rejects.toBe(descriptorFailure)
    } finally {
      process.off('unhandledRejection', onUnhandled)
    }
  })

  it.each(['cloud', 'local', 'remote'] as const)(
    'keeps the short cold budget for a %s route while the registry cache is loading',
    async kind => {
      vi.useFakeTimers()
      $connectionsRegistry.set(null)

      const getConnectionFor = vi.fn(() => new Promise(() => undefined))

      const list = vi.fn(async () => ({
        connections: [{ id: 'target', kind, label: 'Target' }],
        primary: 'target'
      }))

      installDesktop({ connections: { list }, getConnectionFor })

      const pending = expect(openGatewayForAgent('target', 'cmo')).rejects.toThrow(
        'Timed out connecting to profile "cmo"'
      )

      await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS - 1)
      expect(getConnectionFor).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(1)
      await pending
    }
  )

  it('uses the short reconnect budget after an SSH secondary was pruned and recreated', async () => {
    gatewayMocks.connect.mockImplementation(async () => undefined)
    $connectionsRegistry.set({
      connections: [
        { id: 'local', kind: 'local', label: 'This device' },
        { id: 'imac', kind: 'ssh', label: 'iMac' }
      ],
      primary: 'local'
    } as never)

    const getConnectionFor = vi.fn().mockResolvedValueOnce({
      connectionId: 'imac',
      profile: 'cmo',
      wsUrl: 'wss://imac.invalid/ws'
    })

    installDesktop({ getConnectionFor })
    await openGatewayForAgent('imac', 'cmo')

    pruneSecondaryGateways(new Set())
    expect(gatewayMocks.instances[0].close).toHaveBeenCalled()

    vi.useFakeTimers()
    getConnectionFor.mockImplementationOnce(() => new Promise(() => undefined))

    const connection = openGatewayForAgent('imac', 'cmo')
    const pending = expect(connection).rejects.toThrow('Timed out connecting to profile "cmo"')

    await vi.advanceTimersByTimeAsync(RECONNECT_ATTEMPT_TIMEOUT_MS)
    await pending
  })

  it('does not let a wedged shared-primary-route probe block the secondary dial forever', async () => {
    // Same unbounded-IPC hazard as above, but for sharedPrimaryRoute's own
    // getConnection() probe, which runs BEFORE openSecondary on every route —
    // a wedge there must resolve to "not the shared primary" and fall through
    // to the ordinary secondary dial instead of hanging the whole route
    // decision forever.
    vi.useFakeTimers()

    let callCount = 0

    const getConnection = vi.fn(({ profile }: { profile: string }) => {
      callCount += 1

      if (callCount === 1) {
        return new Promise(() => undefined)
      }

      return Promise.resolve({
        authMode: 'token',
        baseUrl: `https://${profile}.invalid`,
        mode: 'local',
        profile,
        token: 'fake-test-token',
        wsUrl: `wss://${profile}.invalid/ws`
      })
    })

    installDesktop({ getConnection })

    // The #92434 pin above leaves gatewayMocks.connect latched on a
    // never-resolving mockImplementation (vi.clearAllMocks() clears calls, not
    // implementations). Restore the default resolving dial for this test.
    gatewayMocks.connect.mockImplementation(async () => undefined)

    const pending = ensureGatewayForProfile('work')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending

    // The probe's own bound (not just openSecondary's) is what let this
    // resolve after a single 20s timeout instead of two stacked ones.
    expect(callCount).toBe(2)
    expect(activeGateway()).toBe(gatewayMocks.instances[0])
  })
})
