import { backendScopeKey, type ConnectionRegistry } from './connection-registry'

export interface WindowConnectionRoute {
  connectionId: null | string
  profile: string | undefined
  registryScoped: boolean
}

interface PublishedSshRouteState {
  registryConnectionId?: string
}

interface PublishedSshRouteRegistry {
  get(scope: string): PublishedSshRouteState | null | undefined
}

interface RegistryConnectionIdentity {
  id: string
}

export function normalizeWindowConnectionRoute(value: unknown): WindowConnectionRoute | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const input = value as Record<string, unknown>
  const connectionId = typeof input.connectionId === 'string' ? input.connectionId.trim() : ''

  const profile = typeof input.profile === 'string' && input.profile.trim() ? input.profile.trim() : undefined

  return {
    connectionId: connectionId || null,
    profile,
    registryScoped: input.registryScoped === true
  }
}

export function registrySshScopeForWindowRoute(
  route: WindowConnectionRoute | null | undefined,
  registry: ConnectionRegistry
): null | string {
  if (!route?.registryScoped || !route.connectionId) {
    return null
  }

  const source = registry.connections.find(connection => connection.id === route.connectionId)

  if (!source || source.kind !== 'ssh') {
    return null
  }

  return backendScopeKey(route.connectionId, route.profile)
}

/** Exact registry owner of the backend serving a window's ambient requests.
 * Registry-scoped routes name themselves. Legacy SSH routes use the identity
 * captured when that live tunnel was published. The mutable registry primary
 * is never evidence about an already-running idless descriptor. */
export function activeRosterConnectionId(
  route: WindowConnectionRoute | null | undefined,
  connections: RegistryConnectionIdentity[],
  sshState: PublishedSshRouteState | null | undefined
): null | string {
  const known = new Set(connections.map(connection => String(connection?.id || '').trim()).filter(Boolean))
  const routed = route?.registryScoped ? String(route.connectionId || '').trim() : ''

  if (routed) {
    return known.has(routed) ? routed : null
  }

  const published = String(sshState?.registryConnectionId || '').trim()

  return published && known.has(published) ? published : null
}

/** Locate the published SSH state serving an idless window route. Profile
 * overrides publish under their profile key; a global primary publishes at
 * the empty legacy scope even though the renderer names its active profile. */
export function activeRosterSshState(
  route: WindowConnectionRoute | null | undefined,
  primaryProfile: string,
  published: PublishedSshRouteRegistry
): PublishedSshRouteState | null {
  if (route?.registryScoped) {
    return null
  }

  const primary = String(primaryProfile || '').trim() || 'default'
  const profile = String(route?.profile || '').trim() || primary
  const profileState = published.get(profile)

  if (profileState) {
    return profileState
  }

  return profile === primary ? published.get('') || null : null
}

export class WindowConnectionRouteRegistry {
  private readonly routes = new Map<number, WindowConnectionRoute>()

  set(webContentsId: number, value: unknown): WindowConnectionRoute | null {
    const route = normalizeWindowConnectionRoute(value)

    if (!route) {
      this.routes.delete(webContentsId)

      return null
    }

    this.routes.set(webContentsId, route)

    return route
  }

  get(webContentsId: number): WindowConnectionRoute | null {
    return this.routes.get(webContentsId) ?? null
  }

  delete(webContentsId: number): void {
    this.routes.delete(webContentsId)
  }
}
