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

function bootstrapPriority(metadata) {
  return metadata?.managedScope === 'primary' ? 1 : 0
}

function createBootstrapCoordinator({ maxConcurrent = Number.POSITIVE_INFINITY } = {}) {
  const active = new Set<any>()
  const pending = new Map<string, any>()
  const generations = new Map<string, number>()
  const drains = new Map<string, Promise<void>>()
  const concurrency = Number.isFinite(maxConcurrent) ? Math.max(1, Math.floor(maxConcurrent)) : Number.POSITIVE_INFINITY
  const waiters: any[] = []
  let waiterSequence = 0
  let running = 0
  let shutdownRequested = false

  function releasePermit() {
    running = Math.max(0, running - 1)
    pump()
  }

  function pump() {
    while (running < concurrency && waiters.length > 0) {
      waiters.sort((left, right) => right.priority - left.priority || left.sequence - right.sequence)
      const waiter = waiters.shift()

      if (waiter.cancelled || waiter.signal.aborted) {
        continue
      }

      waiter.signal.removeEventListener('abort', waiter.onAbort)
      running += 1
      waiter.launch()
    }
  }

  function schedule(signal, run, metadata) {
    if (signal.aborted) {
      return Promise.reject(supersededError())
    }

    return new Promise((resolve, reject) => {
      const waiter: any = {
        cancelled: false,
        launch: null,
        onAbort: null,
        priority: bootstrapPriority(metadata),
        sequence: waiterSequence++,
        signal
      }

      waiter.launch = () => {
        let result

        try {
          result = run()
        } catch (error) {
          releasePermit()
          reject(error)

          return
        }

        Promise.resolve(result).then(
          value => {
            releasePermit()
            resolve(value)
          },
          error => {
            releasePermit()
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

  function start(scope, fingerprint, run, metadata = null) {
    if (shutdownRequested) {
      return Promise.reject(supersededError('SSH bootstrap was cancelled because Desktop is quitting.'))
    }

    const current = pending.get(scope)

    if (current?.fingerprint === fingerprint) {
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

    const drain = drains.get(scope) || Promise.resolve()
    const predecessor = current ? Promise.allSettled([current.promise, drain]) : drain
    const entry: any = { controller, fingerprint, forceCleanups, generation, metadata, promise: null, scope }

    const promise = predecessor
      .then(() => {
        lease.assertCurrent()

        return schedule(lease.signal, () => {
          lease.assertCurrent()

          return run(lease)
        }, metadata)
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
    pending.get(scope)?.controller.abort()
  }

  async function cancelAndWait(scope) {
    let release

    const barrier = new Promise<void>(resolve => {
      release = resolve
    })

    drains.set(scope, barrier)
    const entries = [...active].filter(entry => entry.scope === scope)

    for (const entry of entries) {
      entry.controller.abort()
    }

    try {
      // Cancellation alone only invalidates the lease; it does not interrupt a
      // child process currently blocked in SSH connect/config resolution. Close
      // registered resources first so rollback can settle promptly while the
      // drain barrier still prevents stale resurrection.
      await Promise.allSettled(entries.flatMap(entry => [...entry.forceCleanups]).map(cleanup => cleanup()))
      await Promise.allSettled(entries.map(entry => entry.promise))
    } finally {
      if (drains.get(scope) === barrier) {
        drains.delete(scope)
      }

      release()
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
