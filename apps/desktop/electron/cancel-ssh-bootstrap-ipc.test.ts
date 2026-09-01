import { describe, expect, it, vi } from 'vitest'

import {
  CANCEL_SSH_BOOTSTRAP_CHANNEL,
  invokeCancelSshBootstrap,
  registerCancelSshBootstrapIpc,
  sshTeardownScopesForRoute
} from './cancel-ssh-bootstrap-ipc'
import { backendScopeKey } from './connection-registry'
import { createBootstrapCoordinator } from './ssh-bootstrap-coordinator'

function createIpcHarness() {
  const handlers = new Map<string, (event: unknown, payload: unknown) => Promise<unknown>>()

  return {
    ipcMain: {
      handle(channel: string, listener: (event: unknown, payload: unknown) => Promise<unknown>) {
        handlers.set(channel, listener)
      }
    },
    ipcRenderer: {
      invoke(channel: string, payload: unknown) {
        const handler = handlers.get(channel)

        if (!handler) {
          return Promise.reject(new Error(`No IPC handler for ${channel}`))
        }

        return handler({}, payload)
      }
    },
    handlers
  }
}

describe('cancel SSH bootstrap IPC', () => {
  it('runs the preload invoke through the registered handler and drains the real coordinator scope', async () => {
    const ipc = createIpcHarness()
    const coordinator = createBootstrapCoordinator()
    const scope = backendScopeKey('imac', 'default')
    const calls: string[] = []
    let markStarted!: () => void
    let finishBootstrap!: () => void

    const started = new Promise<void>(resolve => {
      markStarted = resolve
    })

    const running = coordinator.start(
      '',
      'fingerprint',
      lease => {
        return new Promise<void>(resolve => {
          finishBootstrap = resolve
          lease.onForceCleanup(() => {
            calls.push('cleanup')
            finishBootstrap()
          })
          markStarted()
        })
      },
      { cancelScopes: [scope], managedScope: 'primary' }
    )

    const cancelAndWait = vi.fn(async (targetScope: string, whileDrained?: () => Promise<void> | void) => {
      calls.push('cancel')
      await coordinator.cancelAndWait(targetScope, whileDrained)
    })

    const stopPoolBackend = vi.fn(async () => {
      calls.push('stop')
    })

    const teardownSshScopes = vi.fn(async () => {
      calls.push('teardown')
    })

    registerCancelSshBootstrapIpc(ipc.ipcMain, {
      cancelAndWait,
      readRegistry: () => ({ connections: [{ id: 'imac', kind: 'ssh' }] }),
      resolveSshScopes: () => [scope, ''],
      scopeKey: backendScopeKey,
      stopPoolBackend,
      teardownSshScopes
    })

    await started
    expect(ipc.handlers.has(CANCEL_SSH_BOOTSTRAP_CHANNEL)).toBe(true)

    await expect(
      invokeCancelSshBootstrap(ipc.ipcRenderer, { connectionId: 'imac', profile: 'default' })
    ).resolves.toEqual({ cancelled: true, ok: true })
    await running

    expect(cancelAndWait).toHaveBeenCalledWith([scope, ''], expect.any(Function))
    expect(stopPoolBackend).toHaveBeenCalledWith(scope)
    expect(teardownSshScopes).toHaveBeenCalledWith([scope, ''])
    expect(coordinator.pending.has('')).toBe(false)
    expect(calls).toEqual(['cancel', 'cleanup', 'stop', 'teardown'])
  })

  it('keeps replacement publication blocked until pool and SSH teardown finish', async () => {
    const ipc = createIpcHarness()
    const coordinator = createBootstrapCoordinator()
    const scope = backendScopeKey('imac', 'default')
    let finishRunning!: () => void
    let finishTeardown!: () => void
    let replacementStarted = false

    const teardownGate = new Promise<void>(resolve => {
      finishTeardown = resolve
    })

    const running = coordinator.start(
      '',
      'old',
      async lease => {
        await new Promise<void>(resolve => {
          finishRunning = resolve
          lease.onForceCleanup(resolve)
        })
        lease.assertCurrent()
      },
      { cancelScopes: [scope], managedScope: 'primary' }
    )

    registerCancelSshBootstrapIpc(ipc.ipcMain, {
      cancelAndWait: (target, whileDrained) => coordinator.cancelAndWait(target, whileDrained),
      readRegistry: () => ({ connections: [{ id: 'imac', kind: 'ssh' }] }),
      resolveSshScopes: () => [scope, ''],
      scopeKey: backendScopeKey,
      stopPoolBackend: vi.fn(async () => undefined),
      teardownSshScopes: vi.fn(async () => teardownGate)
    })

    await Promise.resolve()
    const cancelling = invokeCancelSshBootstrap(ipc.ipcRenderer, { connectionId: 'imac', profile: 'default' })

    const replacement = coordinator.start('', 'new', async () => {
      replacementStarted = true
    })

    await Promise.resolve()
    await Promise.resolve()
    expect(replacementStarted).toBe(false)

    finishRunning()
    await Promise.resolve()
    expect(replacementStarted).toBe(false)

    finishTeardown()
    await cancelling
    await expect(running).rejects.toMatchObject({ kind: 'superseded' })
    await replacement
    expect(replacementStarted).toBe(true)
  })

  it('holds a published primary scope through alias teardown after its bootstrap entry has settled', async () => {
    const ipc = createIpcHarness()
    const coordinator = createBootstrapCoordinator()
    const scope = backendScopeKey('imac', 'default')

    const published = new Map([
      [
        '',
        {
          cancelScopes: [scope],
          primaryRegistryScope: true,
          registryConnectionId: 'imac'
        }
      ]
    ])

    let finishTeardown!: () => void
    let markTeardownStarted!: () => void
    let replacementStarted = false

    const teardownGate = new Promise<void>(resolve => {
      finishTeardown = resolve
    })

    const teardownStarted = new Promise<void>(resolve => {
      markTeardownStarted = resolve
    })

    registerCancelSshBootstrapIpc(ipc.ipcMain, {
      cancelAndWait: (target, whileDrained) => coordinator.cancelAndWait(target, whileDrained),
      readRegistry: () => ({ connections: [{ id: 'imac', kind: 'ssh' }] }),
      resolveSshScopes: (connectionId, target) =>
        sshTeardownScopesForRoute(published, connectionId, target),
      scopeKey: backendScopeKey,
      stopPoolBackend: vi.fn(async () => undefined),
      teardownSshScopes: vi.fn(async scopes => {
        for (const target of scopes) {
          published.delete(target)
        }

        markTeardownStarted()
        await teardownGate
      })
    })

    const cancelling = invokeCancelSshBootstrap(ipc.ipcRenderer, { connectionId: 'imac', profile: 'default' })

    await teardownStarted

    const primaryDrain = coordinator.cancelAndWait('')

    const replacement = coordinator.start('', 'new', async () => {
      replacementStarted = true
    })

    await Promise.resolve()
    expect(replacementStarted).toBe(false)

    finishTeardown()
    await Promise.all([cancelling, primaryDrain])
    await replacement
    expect(replacementStarted).toBe(true)
  })

  it('refuses to cancel non-SSH registry sources', async () => {
    const ipc = createIpcHarness()
    const cancelAndWait = vi.fn(async () => undefined)

    registerCancelSshBootstrapIpc(ipc.ipcMain, {
      cancelAndWait,
      readRegistry: () => ({ connections: [{ id: 'local', kind: 'local' }] }),
      resolveSshScopes: (_connectionId, scope) => [scope],
      scopeKey: backendScopeKey,
      stopPoolBackend: vi.fn(),
      teardownSshScopes: vi.fn()
    })

    await expect(
      invokeCancelSshBootstrap(ipc.ipcRenderer, { connectionId: 'local', profile: 'default' })
    ).resolves.toEqual({ cancelled: false, ok: true })
    expect(cancelAndWait).not.toHaveBeenCalled()
  })

  it('maps a published primary alias back to its real SSH scope', () => {
    const scope = backendScopeKey('imac', 'default')

    const states = new Map<
      string,
      { cancelScopes?: string[]; primaryRegistryScope?: boolean; registryConnectionId?: string }
    >([
      ['', { cancelScopes: [scope], primaryRegistryScope: false, registryConnectionId: 'imac' }],
      [scope, { cancelScopes: [], primaryRegistryScope: false, registryConnectionId: 'imac' }],
      ['unrelated', { cancelScopes: [scope], primaryRegistryScope: true, registryConnectionId: 'other' }]
    ])

    expect(sshTeardownScopesForRoute(states, 'imac', scope)).toEqual([scope, ''])
  })

  it('maps a direct primary teardown back to its published registry alias', () => {
    const alias = backendScopeKey('imac', 'default')

    const states = new Map([
      [
        '',
        {
          cancelScopes: [alias],
          primaryRegistryScope: true,
          registryConnectionId: 'imac'
        }
      ]
    ])

    expect(sshTeardownScopesForRoute(states, 'imac', '')).toEqual(['', alias])
  })

  it('does not tear down a different primary profile on the same SSH connection', () => {
    const requested = backendScopeKey('imac', 'default')
    const research = backendScopeKey('imac', 'research')

    const states = new Map([
      [
        'research',
        {
          cancelScopes: [research],
          primaryRegistryScope: true,
          registryConnectionId: 'imac'
        }
      ]
    ])

    expect(sshTeardownScopesForRoute(states, 'imac', requested)).toEqual([requested])
  })
})
