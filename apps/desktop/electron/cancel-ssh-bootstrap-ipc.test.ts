import { describe, expect, it, vi } from 'vitest'

import {
  CANCEL_SSH_BOOTSTRAP_CHANNEL,
  invokeCancelSshBootstrap,
  registerCancelSshBootstrapIpc
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

    const cancelAndWait = vi.fn(async (targetScope: string) => {
      calls.push('cancel')
      await coordinator.cancelAndWait(targetScope)
    })
    const stopPoolBackend = vi.fn(async () => {
      calls.push('stop')
    })
    const teardownSshConnection = vi.fn(async () => {
      calls.push('teardown')
    })

    registerCancelSshBootstrapIpc(ipc.ipcMain, {
      cancelAndWait,
      readRegistry: () => ({ connections: [{ id: 'imac', kind: 'ssh' }] }),
      scopeKey: backendScopeKey,
      stopPoolBackend,
      teardownSshConnection
    })

    await started
    expect(ipc.handlers.has(CANCEL_SSH_BOOTSTRAP_CHANNEL)).toBe(true)

    await expect(
      invokeCancelSshBootstrap(ipc.ipcRenderer, { connectionId: 'imac', profile: 'default' })
    ).resolves.toEqual({ cancelled: true, ok: true })
    await running

    expect(cancelAndWait).toHaveBeenCalledWith(scope)
    expect(stopPoolBackend).toHaveBeenCalledWith(scope)
    expect(teardownSshConnection).toHaveBeenCalledWith(scope)
    expect(coordinator.pending.has('')).toBe(false)
    expect(calls).toEqual(['cancel', 'cleanup', 'stop', 'teardown'])
  })

  it('refuses to cancel non-SSH registry sources', async () => {
    const ipc = createIpcHarness()
    const cancelAndWait = vi.fn(async () => undefined)

    registerCancelSshBootstrapIpc(ipc.ipcMain, {
      cancelAndWait,
      readRegistry: () => ({ connections: [{ id: 'local', kind: 'local' }] }),
      scopeKey: backendScopeKey,
      stopPoolBackend: vi.fn(),
      teardownSshConnection: vi.fn()
    })

    await expect(
      invokeCancelSshBootstrap(ipc.ipcRenderer, { connectionId: 'local', profile: 'default' })
    ).resolves.toEqual({ cancelled: false, ok: true })
    expect(cancelAndWait).not.toHaveBeenCalled()
  })
})
