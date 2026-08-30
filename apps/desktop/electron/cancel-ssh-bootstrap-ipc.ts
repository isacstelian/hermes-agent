const CANCEL_SSH_BOOTSTRAP_CHANNEL = 'hermes:connections:cancel-bootstrap'

type MaybePromise<T> = Promise<T> | T

interface CancelBootstrapPayload {
  connectionId?: string
  profile?: string
}

interface CancelBootstrapResult {
  cancelled: boolean
  ok: boolean
}

interface IpcMainLike {
  handle(
    channel: string,
    listener: (event: unknown, payload: unknown) => Promise<CancelBootstrapResult>
  ): void
}

interface IpcRendererLike {
  invoke(channel: string, payload: CancelBootstrapPayload): Promise<unknown>
}

interface CancelBootstrapDeps {
  cancelAndWait(scopes: string | string[], whileDrained?: () => MaybePromise<void>): Promise<void>
  readRegistry(): { connections: Array<{ id: string; kind: string }> }
  resolveSshScopes(connectionId: string, scope: string): string[]
  scopeKey(connectionId: string, profile: string): string
  stopPoolBackend(scope: string): MaybePromise<unknown>
  teardownSshScopes(scopes: string[]): MaybePromise<unknown>
}

interface PublishedSshRouteState {
  cancelScopes?: string[]
  primaryRegistryScope?: boolean
  registryConnectionId?: string
}

function sshTeardownScopesForRoute(
  published: Iterable<[string, PublishedSshRouteState]>,
  connectionId: string,
  requestedScope: string
): string[] {
  const scopes = new Set([requestedScope])

  for (const [scope, state] of published) {
    const cancelScopes = state.cancelScopes || []

    if (
      (scope === '' || state?.primaryRegistryScope === true) &&
      (!connectionId || String(state.registryConnectionId || '').trim() === connectionId) &&
      (scope === requestedScope || cancelScopes.includes(requestedScope))
    ) {
      scopes.add(scope)

      for (const cancelScope of cancelScopes) {
        scopes.add(cancelScope)
      }
    }
  }

  return [...scopes]
}

function invokeCancelSshBootstrap(
  ipcRenderer: IpcRendererLike,
  payload: CancelBootstrapPayload
): Promise<CancelBootstrapResult> {
  return ipcRenderer.invoke(CANCEL_SSH_BOOTSTRAP_CHANNEL, payload) as Promise<CancelBootstrapResult>
}

function registerCancelSshBootstrapIpc(ipcMain: IpcMainLike, deps: CancelBootstrapDeps): void {
  ipcMain.handle(CANCEL_SSH_BOOTSTRAP_CHANNEL, async (_event, payload) => {
    const input = payload && typeof payload === 'object' ? (payload as CancelBootstrapPayload) : {}
    const registry = deps.readRegistry()
    const connectionId = String(input.connectionId || '').trim()
    const profile = String(input.profile || '').trim() || 'default'
    const source = registry.connections.find(connection => connection.id === connectionId)

    if (!source || source.kind !== 'ssh') {
      return { cancelled: false, ok: true }
    }

    const scope = deps.scopeKey(connectionId, profile)
    const scopes = deps.resolveSshScopes(connectionId, scope)

    await deps.cancelAndWait(scopes, async () => {
      await deps.stopPoolBackend(scope)
      await deps.teardownSshScopes(scopes)
    })

    return { cancelled: true, ok: true }
  })
}

export {
  CANCEL_SSH_BOOTSTRAP_CHANNEL,
  invokeCancelSshBootstrap,
  registerCancelSshBootstrapIpc,
  sshTeardownScopesForRoute
}
