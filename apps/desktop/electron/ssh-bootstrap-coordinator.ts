import crypto from 'node:crypto'

function sshConfigFingerprint(scope, config) {
  const parts = [
    scope,
    config.host,
    config.user,
    config.port,
    config.keyPath,
    config.remoteHermesPath,
    config.remoteProfile,
    config.effectiveConfigFingerprint
  ]

  return crypto
    .createHash('sha256')
    .update(JSON.stringify(parts.map(value => value ?? '')))
    .digest('hex')
}

function supersededError(message = 'SSH bootstrap was superseded by newer connection settings.') {
  const error: any = new Error(message)
  error.kind = 'superseded'

  return error
}

function busyError() {
  const error: any = new Error('SSH bootstrap queue is busy. Retry after another agent finishes starting.')
  error.kind = 'busy'

  return error
}

function bootstrapPriority(metadata) {
  return metadata?.managedScope === 'primary' ? 1 : 0
}

function mergeBootstrapMetadata(current, incoming) {
  if (!incoming || current === incoming) {
    return current
  }

  const cancelScopes = new Set([...(current.cancelScopes || []), ...(incoming.cancelScopes || [])])

  current.cancelScopes = [...cancelScopes]

  if (bootstrapPriority(incoming) > bootstrapPriority(current)) {
    current.managedScope = incoming.managedScope
    current.managedUpdateCorrelation = incoming.managedUpdateCorrelation || current.managedUpdateCorrelation
    current.registryConnectionId = incoming.registryConnectionId || current.registryConnectionId
  }

  if (incoming.primaryRegistryScope === true) {
    current.primaryRegistryScope = true
  }

  if (!current.registryConnectionId && incoming.registryConnectionId) {
    current.registryConnectionId = incoming.registryConnectionId
  }

  return current
}

function createBootstrapCoordinator({
  maxConcurrent = Number.POSITIVE_INFINITY,
  maxQueuedNonPrimary = Number.POSITIVE_INFINITY,
  reservedPrimarySlots = 0
} = {}) {
  const active = new Set<any>()
  const pending = new Map<string, any>()
  const generations = new Map<string, number>()
  const drains = new Map<string, any>()
  const concurrency = Number.isFinite(maxConcurrent) ? Math.max(1, Math.floor(maxConcurrent)) : Number.POSITIVE_INFINITY

  const requestedPrimaryReservation = Number.isFinite(reservedPrimarySlots)
    ? Math.max(0, Math.floor(reservedPrimarySlots))
    : 0

  const primaryReservation = Number.isFinite(concurrency)
    ? Math.min(concurrency, requestedPrimaryReservation)
    : 0

  const nonPrimaryQueueLimit = Number.isFinite(maxQueuedNonPrimary)
    ? Math.max(0, Math.floor(maxQueuedNonPrimary))
    : Number.POSITIVE_INFINITY

  // Reservation gates new non-primary work only. Active bootstraps are never
  // preempted or cancelled to make room for a later primary.
  const nonPrimaryConcurrency = concurrency - primaryReservation
  const waiters: any[] = []
  let waiterSequence = 0
  let running = 0
  let runningNonPrimary = 0
  let shutdownRequested = false

  function releasePermit(primary) {
    running = Math.max(0, running - 1)

    if (!primary) {
      runningNonPrimary = Math.max(0, runningNonPrimary - 1)
    }

    pump()
  }

  function pump() {
    while (running < concurrency && waiters.length > 0) {
      waiters.sort(
        (left, right) =>
          bootstrapPriority(right.metadata) - bootstrapPriority(left.metadata) || left.sequence - right.sequence
      )

      for (let index = waiters.length - 1; index >= 0; index -= 1) {
        const waiter = waiters[index]

        if (waiter.cancelled || waiter.signal.aborted) {
          waiters.splice(index, 1)
        }
      }

      const waiterIndex = waiters.findIndex(
        waiter => waiter.metadata?.managedScope === 'primary' || runningNonPrimary < nonPrimaryConcurrency
      )

      if (waiterIndex < 0) {
        return
      }

      const [waiter] = waiters.splice(waiterIndex, 1)
      waiter.signal.removeEventListener('abort', waiter.onAbort)
      const primary = waiter.metadata?.managedScope === 'primary'
      running += 1

      if (!primary) {
        runningNonPrimary += 1
      }

      waiter.launch(primary)
    }
  }

  function schedule(signal, run, metadata) {
    if (signal.aborted) {
      return Promise.reject(supersededError())
    }

    const primary = metadata?.managedScope === 'primary'

    if (
      !primary &&
      waiters.filter(
        waiter => waiter.metadata?.managedScope !== 'primary' && !waiter.cancelled && !waiter.signal.aborted
      ).length >=
        nonPrimaryQueueLimit
    ) {
      return Promise.reject(busyError())
    }

    return new Promise((resolve, reject) => {
      const waiter: any = {
        cancelled: false,
        launch: null,
        metadata,
        onAbort: null,
        sequence: waiterSequence++,
        signal
      }

      waiter.launch = runningAsPrimary => {
        let result

        try {
          result = run()
        } catch (error) {
          releasePermit(runningAsPrimary)
          reject(error)

          return
        }

        Promise.resolve(result).then(
          value => {
            releasePermit(runningAsPrimary)
            resolve(value)
          },
          error => {
            releasePermit(runningAsPrimary)
            reject(error)
          }
        )
      }

      waiter.onAbort = () => {
        if (waiter.cancelled) {
          return
        }

        waiter.cancelled = true
        reject(supersededError())
        pump()
      }

      signal.addEventListener('abort', waiter.onAbort, { once: true })
      waiters.push(waiter)
      pump()
    })
  }

  async function waitForScopeDrain(scope, signal) {
    while (!signal.aborted) {
      const drain = drains.get(scope)

      if (!drain) {
        return
      }

      await drain.barrier
    }
  }

  function start(scope, fingerprint, run, metadata = null) {
    if (shutdownRequested) {
      return Promise.reject(supersededError('SSH bootstrap was cancelled because Desktop is quitting.'))
    }

    const current = pending.get(scope)

    if (current?.fingerprint === fingerprint) {
      mergeBootstrapMetadata(current.metadata, metadata)
      pump()

      return current.promise
    }

    current?.controller.abort()

    const generation = (generations.get(scope) || 0) + 1
    generations.set(scope, generation)
    const controller = new AbortController()
    const forceCleanups = new Set<() => any>()

    const lease = {
      signal: controller.signal,
      onForceCleanup(cleanup) {
        forceCleanups.add(cleanup)

        return () => forceCleanups.delete(cleanup)
      },
      isCurrent: () => !controller.signal.aborted && generations.get(scope) === generation,
      assertCurrent() {
        if (!this.isCurrent()) {
          throw supersededError()
        }
      }
    }

    const drainBarrier = waitForScopeDrain(scope, controller.signal)
    const predecessor = current ? Promise.allSettled([current.promise, drainBarrier]) : drainBarrier
    const entryMetadata = metadata && typeof metadata === 'object' ? metadata : {}

    const entry: any = {
      controller,
      fingerprint,
      forceCleanups,
      generation,
      metadata: entryMetadata,
      promise: null,
      scope
    }

    const promise = predecessor
      .then(() => {
        lease.assertCurrent()

        return schedule(lease.signal, () => {
          lease.assertCurrent()

          return run(lease)
        }, entryMetadata)
      })
      .finally(() => {
        forceCleanups.clear()
        active.delete(entry)

        if (pending.get(scope)?.generation === generation) {
          pending.delete(scope)
        }
      })

    entry.promise = promise
    active.add(entry)
    pending.set(scope, entry)

    return promise
  }

  function cancel(scope) {
    for (const entry of active) {
      if (entry.scope === scope || entry.metadata?.cancelScopes?.includes(scope)) {
        entry.controller.abort()
      }
    }
  }

  function enqueueDrainCallback(drain, callback): Promise<{ error: unknown | null }> {
    if (typeof callback !== 'function') {
      return Promise.resolve({ error: null })
    }

    return new Promise(resolve => {
      drain.callbacks.push({ resolve, run: callback })
    })
  }

  async function cancelAndWait(scopeOrScopes, whileDrained = null) {
    const requestedScopes = [...new Set(Array.isArray(scopeOrScopes) ? scopeOrScopes : [scopeOrScopes])]
    const requestedScopeSet = new Set(requestedScopes)

    const entries = [...active].filter(
      entry =>
        requestedScopeSet.has(entry.scope) ||
        entry.metadata?.cancelScopes?.some(cancelScope => requestedScopeSet.has(cancelScope))
    )

    const drainScopes = new Set([
      ...requestedScopes,
      ...entries.flatMap(entry => [entry.scope, ...(entry.metadata?.cancelScopes || [])])
    ])

    const existingDrains = [
      ...new Set([...drainScopes].map(drainScope => drains.get(drainScope)).filter(Boolean))
    ]

    const existingDrain = existingDrains.length === 1 ? existingDrains[0] : null

    const existingDrainOwnsRequest =
      existingDrain &&
      [...drainScopes].every(drainScope => drains.get(drainScope) === existingDrain) &&
      entries.every(entry => existingDrain.entries.has(entry))

    if (existingDrainOwnsRequest) {
      const callbackResult = enqueueDrainCallback(existingDrain, whileDrained)
      await existingDrain.barrier
      const result = await callbackResult

      if (result.error) {
        throw result.error
      }

      return
    }

    let release

    const barrier = new Promise<void>(resolve => {
      release = resolve
    })

    const drain = {
      barrier,
      callbacks: [],
      entries: new Set(entries)
    }

    const callbackResult = enqueueDrainCallback(drain, whileDrained)
    const predecessorDrains = existingDrains.map(predecessor => predecessor.barrier)

    for (const drainScope of drainScopes) {
      drains.set(drainScope, drain)
    }

    for (const entry of entries) {
      entry.controller.abort()
    }

    try {
      await Promise.allSettled(predecessorDrains)

      // Cancellation alone only invalidates the lease; it does not interrupt a
      // child process currently blocked in SSH connect/config resolution. Close
      // registered resources first so rollback can settle promptly while the
      // drain barrier still prevents stale resurrection.
      await Promise.allSettled(entries.flatMap(entry => [...entry.forceCleanups]).map(cleanup => cleanup()))
      await Promise.allSettled(entries.map(entry => entry.promise))

      while (drain.callbacks.length > 0) {
        const callback = drain.callbacks.shift()

        try {
          await callback.run()
          callback.resolve({ error: null })
        } catch (error) {
          callback.resolve({ error })
        }
      }
    } finally {
      for (const drainScope of drainScopes) {
        if (drains.get(drainScope) === drain) {
          drains.delete(drainScope)
        }
      }

      release()
    }

    const result = await callbackResult

    if (result.error) {
      throw result.error
    }
  }

  function cancelAll() {
    for (const entry of active) {
      entry.controller.abort()
    }
  }

  function shutdown() {
    // Terminal: reconnect callbacks during a prevented first quit must not
    // spawn a replacement serve --isolated for an app that is already leaving.
    shutdownRequested = true
    cancelAll()
  }

  async function forceCleanupAll() {
    const cleanups = [...active].flatMap(entry => [...entry.forceCleanups])
    await Promise.allSettled(cleanups.map(cleanup => cleanup()))
  }

  function promises() {
    return [...active].map(entry => entry.promise)
  }

  return { active, cancel, cancelAll, cancelAndWait, forceCleanupAll, pending, promises, shutdown, start }
}

export { createBootstrapCoordinator, sshConfigFingerprint }
