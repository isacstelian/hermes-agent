/**
 * Duplicating a bot: clone the profile (config/skills/SOUL/memory via
 * `clone_from`) and copy the LOOK, but never the things that belong to the
 * original — its canonical-chat pointer and its creation stamp.
 *
 * The name search is the other half. Candidates are `<base>-2`, `-3`, … and
 * the BASE is truncated to fit, never the suffix (#19): slicing the joined
 * string chops the "-2" off a max-length name, so the candidate collides with
 * the base forever and the search runs out at -99.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $botMeta } from './data'
import { duplicateBot, pullServerAvatars } from './profile-ops'
import type { RosterRow } from './types'

const { ensureBotMetadataMock, hostMock, storageMock } = vi.hoisted(() => ({
  ensureBotMetadataMock: vi.fn(),
  hostMock: {
    request: vi.fn(),
    requestProfile: vi.fn(),
    state: {
      connectionId: { get: vi.fn(() => 'local') },
      connectionKey: { get: vi.fn<() => null | string>(() => 'connection:local') },
      connectionMode: { get: vi.fn<() => null | 'local' | 'remote'>(() => 'local') },
      focusedSessionOwner: null,
      profile: { get: () => 'default' }
    }
  },
  storageMock: { get: vi.fn(), set: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return {
    atom,
    forgetSessionUnread: vi.fn(),
    host: hostMock,
    queryClient: { invalidateQueries: vi.fn() },
    useQuery: vi.fn(),
    useValue: vi.fn()
  }
})

vi.mock('./shared', () => ({ getPluginCtx: () => ({ storage: storageMock }), ID: 'hermes-bots' }))
vi.mock('./avatar-image', () => ({ isBackfilledFacePng: () => false }))
vi.mock('./canonical-chat', () => ({ ensureBotMetadata: ensureBotMetadataMock }))

const calls: Array<{ method: string; params: Record<string, unknown> }> = []

beforeEach(() => {
  vi.clearAllMocks()
  calls.length = 0
  $botMeta.set({})
  storageMock.set.mockResolvedValue(undefined)
  hostMock.state.connectionId.get.mockReturnValue('local')
  hostMock.state.connectionKey.get.mockReturnValue('connection:local')
  hostMock.state.connectionMode.get.mockReturnValue('local')
  hostMock.request.mockImplementation(async (method: string, params: Record<string, unknown>) => {
    calls.push({ method, params: structuredClone(params ?? {}) })

    return { ok: true }
  })
})

afterEach(() => {
  document.body.replaceChildren()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('duplicating a bot', () => {
  it('copies the look but neither the chat pointer nor the creation stamp', async () => {
    $botMeta.set({
      researcher: {
        chat: 'sess-source-forever',
        color: '#f97316',
        created: 1_700_000_000_000,
        image: 'data:image/png;base64,xx',
        shape: 'circle',
        title: 'Researcher'
      }
    })

    const name = await duplicateBot({ description: 'finds things', name: 'researcher' } as RosterRow, [
      { name: 'researcher' } as RosterRow
    ])

    expect(name).toBe('researcher-2')

    const clone = $botMeta.get()['researcher-2']

    expect(clone).toMatchObject({
      color: '#f97316',
      image: 'data:image/png;base64,xx',
      shape: 'circle',
      title: 'Researcher (copy)'
    })
    expect(clone.chat).toBeUndefined()
    expect(clone.created).toBeUndefined()

    expect(calls.find(call => call.method === 'profiles.create')?.params).toMatchObject({
      clone_from: 'researcher',
      name: 'researcher-2'
    })

    const configure = calls.filter(call => call.method === 'profiles.configure').at(-1)
    const uiMeta = (configure?.params.ui_meta as Record<string, Record<string, unknown>>)['hermes-bots']

    expect(uiMeta.title).toBe('Researcher (copy)')
    expect(uiMeta.chat).toBeUndefined()
    expect(uiMeta.created).toBeUndefined()
  })

  it('duplicates the look of a bot that never had a pointer', async () => {
    $botMeta.set({ painter: { color: '#38bdf8', shape: 'cloud', title: 'Painter' } })

    const name = await duplicateBot({ name: 'painter' } as RosterRow, [{ name: 'painter' } as RosterRow])

    expect($botMeta.get()[name]).toMatchObject({ shape: 'cloud', title: 'Painter (copy)' })
    expect($botMeta.get()[name].chat).toBeUndefined()
  })

  it('walks past taken suffixes to the first free slot', async () => {
    const roster = ['ops', 'ops-2', 'ops-3'].map(name => ({ name }) as RosterRow)

    expect(await duplicateBot({ name: 'ops' } as RosterRow, roster)).toBe('ops-4')
  })

  it('truncates the BASE so a max-length name still gets a distinct suffix (#19)', async () => {
    const base = 'b'.repeat(64)

    const name = await duplicateBot({ name: base } as RosterRow, [{ name: base } as RosterRow])

    expect(name).toHaveLength(64)
    expect(name.endsWith('-2')).toBe(true)
    expect(name).not.toBe(base)
  })

  it('ensures the source bot has its metadata before cloning', async () => {
    // clone_from copies the profile dir; the source's Bot Chat has to exist
    // first or the clone inherits a half-built profile.
    await duplicateBot({ name: 'ops' } as RosterRow, [])

    expect(ensureBotMetadataMock).toHaveBeenCalledTimes(1)
  })

  it('only collides against rows on the SAME connection', async () => {
    // A same-named bot on another gateway is a different agent entirely.
    const bot = {
      connectionId: 'vera',
      name: 'ops',
      route: { connectionId: 'vera', mode: 'remote', profile: 'ops', targetProfile: 'ops' },
      sourceScoped: true
    } as RosterRow

    const elsewhere = {
      connectionId: 'other',
      name: 'ops-2',
      route: { connectionId: 'other', mode: 'remote', profile: 'ops-2', targetProfile: 'ops-2' },
      sourceScoped: true
    } as RosterRow

    hostMock.requestProfile.mockResolvedValue({ ok: true })

    expect(await duplicateBot(bot, [bot, elsewhere])).toBe('ops-2')
  })
})

describe('roster avatar sync routing', () => {
  beforeEach(() => {
    hostMock.state.connectionMode.get.mockReturnValue('remote')
  })

  it('keeps active-source avatar reads on the ambient gateway for a 31-profile roster', () => {
    const roster = Array.from({ length: 31 }, (_, index) => {
      const name = `agent-${index}`

      return {
        connectionId: 'imac-hermes',
        has_avatar: true,
        name,
        route: {
          connectionId: 'imac-hermes',
          mode: 'remote' as const,
          profile: name,
          targetProfile: name
        },
        sourceScoped: true
      } as RosterRow
    })

    hostMock.request.mockResolvedValue({ found: false })
    hostMock.requestProfile.mockResolvedValue({ found: false })
    hostMock.state.connectionId.get.mockReturnValue('imac-hermes')

    pullServerAvatars(roster)

    expect(hostMock.request).toHaveBeenCalledTimes(31)
    expect(hostMock.request.mock.calls.every(([method]) => method === 'profiles.get_asset')).toBe(true)
    expect(hostMock.requestProfile).not.toHaveBeenCalled()
  })

  it('keeps a different connection on its exact routed gateway without requiring sourceScoped', () => {
    const remote = {
      connectionId: 'other-host',
      has_avatar: true,
      name: 'remote-agent',
      remoteSource: true,
      route: {
        connectionId: 'other-host',
        mode: 'remote' as const,
        profile: 'remote-agent',
        targetProfile: 'remote-agent'
      },
    } as RosterRow

    hostMock.requestProfile.mockResolvedValue({ found: false })

    pullServerAvatars([remote])

    expect(hostMock.requestProfile).toHaveBeenCalledWith(
      remote.route,
      'profiles.get_asset',
      expect.objectContaining({ asset: 'avatar', name: 'remote-agent' })
    )
    expect(hostMock.request).not.toHaveBeenCalled()
  })

  it('keeps active-source avatar backfills on the ambient gateway too', () => {
    const bot = {
      connectionId: 'imac-hermes',
      has_avatar: false,
      name: 'active-agent',
      route: {
        connectionId: 'imac-hermes',
        mode: 'remote' as const,
        profile: 'active-agent',
        targetProfile: 'active-agent'
      },
      sourceScoped: true
    } as RosterRow

    $botMeta.set({ 'imac-hermes::active-agent': { image: 'data:image/png;base64,active' } })
    hostMock.request.mockResolvedValue({ ok: true })
    hostMock.state.connectionId.get.mockReturnValue('imac-hermes')

    pullServerAvatars([bot])

    expect(hostMock.request).toHaveBeenCalledWith('profiles.set_asset', {
      asset: 'avatar',
      data: 'data:image/png;base64,active',
      name: 'active-agent'
    })
    expect(hostMock.requestProfile).not.toHaveBeenCalled()
  })

  it('keeps migrated v1 remote avatars on the ambient gateway when its connection id is absent', () => {
    const bot = {
      ambientConnectionKey: 'remote:ssh:imac',
      ambientSource: true,
      connectionId: 'imac-hermes',
      has_avatar: true,
      name: 'legacy-agent',
      route: {
        connectionId: 'imac-hermes',
        mode: 'remote' as const,
        profile: 'legacy-agent',
        targetProfile: 'legacy-agent'
      },
      sourceScoped: true
    } as RosterRow

    hostMock.state.connectionId.get.mockReturnValue('')
    hostMock.state.connectionKey.get.mockReturnValue('remote:ssh:imac')
    hostMock.request.mockResolvedValue({ found: false })

    pullServerAvatars([bot])

    expect(hostMock.request).toHaveBeenCalledWith('profiles.get_asset', {
      asset: 'avatar',
      name: 'legacy-agent'
    })
    expect(hostMock.requestProfile).not.toHaveBeenCalled()
  })

  it('does not read a stale legacy remote avatar after switching to an idless local gateway', () => {
    const bot = {
      ambientConnectionKey: 'remote:ssh:imac',
      ambientSource: true,
      connectionId: 'imac-hermes',
      has_avatar: true,
      name: 'legacy-agent',
      route: {
        connectionId: 'imac-hermes',
        mode: 'remote' as const,
        profile: 'legacy-agent',
        targetProfile: 'legacy-agent'
      },
      sourceScoped: true
    } as RosterRow

    hostMock.state.connectionId.get.mockReturnValue('')
    hostMock.state.connectionKey.get.mockReturnValue('local')
    hostMock.state.connectionMode.get.mockReturnValue('local')

    pullServerAvatars([bot])

    expect(hostMock.request).not.toHaveBeenCalled()

    hostMock.state.connectionMode.get.mockReturnValue('remote')
    hostMock.state.connectionKey.get.mockReturnValue('remote:ssh:imac')
    pullServerAvatars([bot])

    expect(hostMock.request).toHaveBeenCalledWith('profiles.get_asset', {
      asset: 'avatar',
      name: 'legacy-agent'
    })
  })

  it('does not read a stale legacy avatar after switching between two idless remotes', () => {
    const bot = {
      ambientConnectionKey: 'remote:ssh:host-a',
      ambientSource: true,
      has_avatar: true,
      name: 'legacy-agent'
    } as RosterRow

    hostMock.state.connectionId.get.mockReturnValue('')
    hostMock.state.connectionMode.get.mockReturnValue('remote')
    hostMock.state.connectionKey.get.mockReturnValue('remote:ssh:host-b')

    pullServerAvatars([bot])

    expect(hostMock.request).not.toHaveBeenCalled()
  })

  it('does not push a stale local avatar onto the newly active gateway', () => {
    const bot = {
      connectionId: 'imac-hermes',
      has_avatar: false,
      name: 'stale-data-agent',
      sourceScoped: true
    } as RosterRow

    $botMeta.set({ 'imac-hermes::stale-data-agent': { image: 'data:image/png;base64,stale' } })
    hostMock.state.connectionId.get.mockReturnValue('other-host')

    pullServerAvatars([bot])

    expect(hostMock.request).not.toHaveBeenCalled()

    hostMock.state.connectionId.get.mockReturnValue('imac-hermes')
    pullServerAvatars([bot])

    expect(hostMock.request).toHaveBeenCalledWith('profiles.set_asset', {
      asset: 'avatar',
      data: 'data:image/png;base64,stale',
      name: 'stale-data-agent'
    })
    expect(hostMock.requestProfile).not.toHaveBeenCalled()
  })

  it('does not read a stale roster row from the newly active gateway', () => {
    const bot = {
      connectionId: 'imac-hermes',
      has_avatar: true,
      name: 'stale-roster-agent',
      sourceScoped: true
    } as RosterRow

    hostMock.state.connectionId.get.mockReturnValue('other-host')
    pullServerAvatars([bot])

    expect(hostMock.request).not.toHaveBeenCalled()

    hostMock.state.connectionId.get.mockReturnValue('imac-hermes')
    pullServerAvatars([bot])

    expect(hostMock.request).toHaveBeenCalledWith('profiles.get_asset', {
      asset: 'avatar',
      name: 'stale-roster-agent'
    })
    expect(hostMock.requestProfile).not.toHaveBeenCalled()
  })

  it('aborts an SVG backfill after a connection switch and permits a retry on the original gateway', async () => {
    const imageLoads: Array<() => void> = []

    class DeferredImage {
      onerror: null | (() => void) = null
      onload: null | (() => void) = null

      set src(_value: string) {
        imageLoads.push(() => this.onload?.())
      }
    }

    vi.stubGlobal('Image', DeferredImage)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      drawImage: vi.fn()
    } as unknown as CanvasRenderingContext2D)
    vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,raster')

    const face = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    face.dataset.botFace = 'raster-agent'
    document.body.append(face)

    const bot = {
      connectionId: 'imac-hermes',
      has_avatar: false,
      name: 'raster-agent',
      route: {
        connectionId: 'imac-hermes',
        mode: 'remote' as const,
        profile: 'raster-agent',
        targetProfile: 'raster-agent'
      },
      sourceScoped: true
    } as RosterRow

    hostMock.state.connectionId.get.mockReturnValue('imac-hermes')
    pullServerAvatars([bot])

    expect(imageLoads).toHaveLength(1)

    hostMock.state.connectionId.get.mockReturnValue('other-host')
    imageLoads[0]()
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(hostMock.request).not.toHaveBeenCalled()
    expect(hostMock.requestProfile).not.toHaveBeenCalled()

    hostMock.state.connectionId.get.mockReturnValue('imac-hermes')
    pullServerAvatars([bot])

    expect(imageLoads).toHaveLength(2)
    imageLoads[1]()

    await vi.waitFor(() =>
      expect(hostMock.request).toHaveBeenCalledWith('profiles.set_asset', {
        asset: 'avatar',
        data: 'data:image/png;base64,raster',
        name: 'raster-agent'
      })
    )
    expect(hostMock.requestProfile).not.toHaveBeenCalled()
  })
})
