import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createBootstrapCoordinator, sshConfigFingerprint } from './ssh-bootstrap-coordinator'

function deferred() {
  let resolve
  let reject

  const promise = new Promise((ok, fail) => {
    resolve = ok
    reject = fail
  })

  return { promise, reject, resolve }
}

const config = { host: 'box', user: 'alice', port: 22, keyPath: '/key', remoteHermesPath: '/hermes' }

test('sshConfigFingerprint covers scope and every connection field', () => {
  const base = sshConfigFingerprint('', config)
  assert.equal(base, sshConfigFingerprint('', { ...config }))

  for (const [field, value] of Object.entries({
    host: 'other',
    user: 'bob',
    port: 2222,
    keyPath: '/other',
    remoteHermesPath: '/other-hermes',
    remoteProfile: 'default',
    effectiveConfigFingerprint: 'changed-config'
  })) {
    assert.notEqual(base, sshConfigFingerprint('', { ...config, [field]: value }))
  }

  assert.notEqual(base, sshConfigFingerprint('profile', config))
})

test('same scope and fingerprint share one bootstrap', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  let runs = 0

  const first = coordinator.start('', 'same', async () => {
    runs++

    return gate.promise
  })

  const second = coordinator.start('', 'same', async () => {
    runs++

    return 'wrong'
  })

  assert.equal(first, second)
  gate.resolve('done')
  assert.equal(await second, 'done')
  assert.equal(runs, 1)
})

test('bounds concurrent bootstraps across different profile scopes', async () => {
  const coordinator = createBootstrapCoordinator({ maxConcurrent: 2 })
  const gates = Array.from({ length: 31 }, () => deferred())
  const started: number[] = []

  const promises = gates.map((gate, index) =>
    coordinator.start(String(index), 'same', async () => {
      started.push(index)
      await gate.promise

      return index
    })
  )

  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(started, [0, 1])

  gates[0].resolve()
  assert.equal(await promises[0], 0)
  await Promise.resolve()
  assert.deepEqual(started, [0, 1, 2])

  gates.slice(1).forEach(gate => gate.resolve())
  assert.deepEqual(await Promise.all(promises), Array.from({ length: 31 }, (_, index) => index))
})

test('a queued bootstrap cancelled before its permit never runs', async () => {
  const coordinator = createBootstrapCoordinator({ maxConcurrent: 1 })
  const gate = deferred()
  let queuedRuns = 0

  const active = coordinator.start('active', 'x', async () => gate.promise)
  await Promise.resolve()

  const queued = coordinator.start('queued', 'x', async () => {
    queuedRuns += 1
  })

  coordinator.cancel('queued')
  gate.resolve('done')
  await active
  await assert.rejects(queued, (error: any) => error.kind === 'superseded')
  assert.equal(queuedRuns, 0)
})

test('a primary reconnect jumps ahead of queued profile prewarms', async () => {
  const coordinator = createBootstrapCoordinator({ maxConcurrent: 2 })
  const gates = [deferred(), deferred()]
  const started: string[] = []

  const active = gates.map((gate, index) =>
    coordinator.start(`pool-${index}`, 'x', async () => {
      started.push(`pool-${index}`)
      await gate.promise
    }, { managedScope: 'pool' })
  )

  await Promise.resolve()

  const queuedPool = coordinator.start('pool-queued', 'x', async () => {
    started.push('pool-queued')
  }, { managedScope: 'pool' })

  const primary = coordinator.start('primary', 'x', async () => {
    started.push('primary')
  }, { managedScope: 'primary' })

  gates[0].resolve()
  await active[0]
  await primary
  await queuedPool
  assert.deepEqual(started, ['pool-0', 'pool-1', 'primary', 'pool-queued'])

  gates[1].resolve()
  await active[1]
})

test('a reserved primary permit stays available while profile prewarms are queued', async () => {
  const coordinator = createBootstrapCoordinator({ maxConcurrent: 2, reservedPrimarySlots: 1 })

  const gates = {
    firstPool: deferred(),
    primary: deferred(),
    secondPool: deferred()
  }

  const started: string[] = []
  let maxRunning = 0
  let running = 0

  const run = async (name, gate) => {
    started.push(name)
    running += 1
    maxRunning = Math.max(maxRunning, running)

    try {
      await gate.promise
    } finally {
      running -= 1
    }
  }

  const firstPool = coordinator.start('pool-first', 'x', () => run('pool-first', gates.firstPool), {
    managedScope: 'pool'
  })

  const secondPool = coordinator.start('pool-second', 'x', () => run('pool-second', gates.secondPool), {
    managedScope: 'pool'
  })

  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(started, ['pool-first'])

  const primary = coordinator.start('primary', 'x', () => run('primary', gates.primary), {
    managedScope: 'primary'
  })

  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(started, ['pool-first', 'primary'])

  gates.primary.resolve()
  await primary
  await Promise.resolve()
  assert.deepEqual(started, ['pool-first', 'primary'])

  gates.firstPool.resolve()
  await firstPool
  await Promise.resolve()
  assert.deepEqual(started, ['pool-first', 'primary', 'pool-second'])

  gates.secondPool.resolve()
  await secondPool
  assert.equal(maxRunning, 2)
})

test('caps the non-primary queue so the renderer cold timeout cannot orphan a later boot', async () => {
  const coordinator = createBootstrapCoordinator({
    maxConcurrent: 2,
    maxQueuedNonPrimary: 1,
    reservedPrimarySlots: 1
  })

  const firstGate = deferred()
  const secondGate = deferred()
  const started: string[] = []

  const first = coordinator.start(
    'pool-first',
    'x',
    async () => {
      started.push('first')
      await firstGate.promise
    },
    { managedScope: 'pool' }
  )

  const second = coordinator.start(
    'pool-second',
    'x',
    async () => {
      started.push('second')
      await secondGate.promise
    },
    { managedScope: 'pool' }
  )

  const third = coordinator.start(
    'pool-third',
    'x',
    async () => {
      started.push('third')
    },
    { managedScope: 'pool' }
  )

  await assert.rejects(third, (error: any) => error.kind === 'busy')
  assert.deepEqual(started, ['first'])

  firstGate.resolve()
  await first
  await Promise.resolve()
  assert.deepEqual(started, ['first', 'second'])

  secondGate.resolve()
  await second
  assert.deepEqual(started, ['first', 'second'])
})

test('changed fingerprint waits for old rollback before starting', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  const events: string[] = []
  let oldLease

  const oldPromise = coordinator.start('', 'old', async lease => {
    oldLease = lease
    events.push('old-start')
    await gate.promise
    events.push('old-rollback')
    lease.assertCurrent()
  })

  await Promise.resolve()

  const newPromise = coordinator.start('', 'new', async lease => {
    events.push('new-start')
    lease.assertCurrent()

    return 'new'
  })

  assert.equal(oldLease.signal.aborted, true)
  await Promise.resolve()
  assert.deepEqual(events, ['old-start'])
  gate.resolve()
  await assert.rejects(oldPromise, (error: any) => error.kind === 'superseded')
  assert.equal(await newPromise, 'new')
  assert.deepEqual(events, ['old-start', 'old-rollback', 'new-start'])
})

test('forceCleanupAll runs registered pending resource cleanup', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  let cleaned = 0

  const promise = coordinator.start('', 'x', async lease => {
    lease.onForceCleanup(async () => {
      cleaned++
    })
    await gate.promise
  })

  await Promise.resolve()
  await coordinator.forceCleanupAll()
  assert.equal(cleaned, 1)
  gate.resolve()
  await promise
})

test('shutdown cancels active bootstraps and permanently rejects respawn attempts', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()

  const active = coordinator.start('primary', 'old', async lease => {
    await gate.promise
    lease.assertCurrent()
  })

  coordinator.shutdown()
  gate.resolve()

  await assert.rejects(active, (error: any) => error.kind === 'superseded')
  let started = 0
  await assert.rejects(
    coordinator.start('primary', 'new', async () => {
      started += 1
    }),
    (error: any) => error.kind === 'superseded'
  )
  assert.equal(started, 0)
})

test('cancelAll invalidates every pending scope and exposes promises for quit', async () => {
  const coordinator = createBootstrapCoordinator()
  const gates = [deferred(), deferred()]

  const promises = gates.map((gate, index) =>
    coordinator.start(String(index), 'x', async lease => {
      await gate.promise
      lease.assertCurrent()
    })
  )

  assert.equal(coordinator.promises().length, 2)
  coordinator.cancelAll()
  gates.forEach(gate => gate.resolve())
  const results = await Promise.allSettled(promises)
  assert.ok(results.every(result => result.status === 'rejected' && (result.reason as any).kind === 'superseded'))
})

test('cancelAndWait drains only the requested scope', async () => {
  const coordinator = createBootstrapCoordinator()
  const firstGate = deferred()
  const secondGate = deferred()

  const first = coordinator.start('first', 'x', async lease => {
    await firstGate.promise
    lease.assertCurrent()
  })

  const second = coordinator.start('second', 'x', async lease => {
    await secondGate.promise
    lease.assertCurrent()

    return 'second'
  })

  await Promise.resolve()
  let drained = false

  const drain = coordinator.cancelAndWait('first').then(() => {
    drained = true
  })

  await Promise.resolve()
  assert.equal(drained, false)
  firstGate.resolve()
  await drain
  await assert.rejects(first, (error: any) => error.kind === 'superseded')
  assert.equal(coordinator.pending.has('second'), true)
  secondGate.resolve()
  assert.equal(await second, 'second')
})

test('cancelAndWait drains a primary bootstrap through its registry-scope alias', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  const alias = 'conn:imac::default'

  const primary = coordinator.start(
    '',
    'x',
    async lease => {
      await gate.promise
      lease.assertCurrent()
    },
    { cancelScopes: [alias], managedScope: 'primary' }
  )

  await Promise.resolve()
  const drain = coordinator.cancelAndWait(alias)
  let nextStarted = false

  const next = coordinator.start('', 'next', async () => {
    nextStarted = true
  })

  await Promise.resolve()
  assert.equal(nextStarted, false)

  gate.resolve()
  await drain
  await assert.rejects(primary, (error: any) => error.kind === 'superseded')
  await next
  assert.equal(nextStarted, true)
})

test('a primary join promotes a pool-first bootstrap and its cancel aliases', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  const alias = 'conn:imac::default'
  const metadata: any = { cancelScopes: [], managedScope: 'pool', primaryRegistryScope: false }
  let cleaned = 0

  const pool = coordinator.start(
    '',
    'same',
    async lease => {
      lease.onForceCleanup(() => {
        cleaned += 1
        gate.resolve()
      })
      await gate.promise
      lease.assertCurrent()
    },
    metadata
  )

  const primary = coordinator.start('', 'same', async () => assert.fail('must coalesce'), {
    cancelScopes: [alias],
    managedScope: 'primary',
    primaryRegistryScope: true,
    registryConnectionId: 'imac'
  })

  assert.equal(primary, pool)
  await Promise.resolve()
  await coordinator.cancelAndWait(alias)
  await assert.rejects(primary, (error: any) => error.kind === 'superseded')
  assert.equal(cleaned, 1)
  assert.equal(metadata.managedScope, 'primary')
  assert.equal(metadata.primaryRegistryScope, true)
  assert.deepEqual(metadata.cancelScopes, [alias])
})

test('a primary join starts a queued pool bootstrap through the reserved slot', async () => {
  const coordinator = createBootstrapCoordinator({ maxConcurrent: 2, reservedPrimarySlots: 1 })
  const otherGate = deferred()
  const targetGate = deferred()
  let targetRuns = 0

  const otherPool = coordinator.start('other', 'same', async () => otherGate.promise, { managedScope: 'pool' })

  const targetPool = coordinator.start(
    'target',
    'same',
    async () => {
      targetRuns += 1
      await targetGate.promise
    },
    { managedScope: 'pool' }
  )

  await Promise.resolve()
  assert.equal(targetRuns, 0)

  const primary = coordinator.start('target', 'same', async () => assert.fail('must coalesce'), {
    managedScope: 'primary'
  })

  assert.equal(primary, targetPool)
  await Promise.resolve()
  assert.equal(targetRuns, 1)

  targetGate.resolve()
  otherGate.resolve()
  await Promise.all([otherPool, primary])
})

test('concurrent cancellation callers share one drain and one force cleanup', async () => {
  const coordinator = createBootstrapCoordinator()
  const cleanupGate = deferred()
  const runGate = deferred()
  const alias = 'conn:imac::default'
  let cleanupCalls = 0
  let nextStarted = false

  const running = coordinator.start(
    '',
    'same',
    async lease => {
      lease.onForceCleanup(async () => {
        cleanupCalls += 1
        await cleanupGate.promise
        runGate.resolve()
      })
      await runGate.promise
      lease.assertCurrent()
    },
    { cancelScopes: [alias], managedScope: 'primary' }
  )

  await Promise.resolve()
  const first = coordinator.cancelAndWait(alias)
  const second = coordinator.cancelAndWait(alias)

  const next = coordinator.start('', 'next', async () => {
    nextStarted = true
  })

  await Promise.resolve()
  assert.equal(cleanupCalls, 1)
  assert.equal(nextStarted, false)

  cleanupGate.resolve()
  await Promise.all([first, second])
  await assert.rejects(running, (error: any) => error.kind === 'superseded')
  await next
  assert.equal(nextStarted, true)
})

test('a teardown callback joining an existing drain finishes before replacement starts', async () => {
  const coordinator = createBootstrapCoordinator()
  const cleanupGate = deferred()
  const runGate = deferred()
  const teardownGate = deferred()
  const teardownStarted = deferred()
  const alias = 'conn:imac::default'
  const events: string[] = []
  let firstDone = false
  let nextStarted = false

  const running = coordinator.start(
    '',
    'same',
    async lease => {
      lease.onForceCleanup(async () => {
        events.push('cleanup')
        runGate.resolve()
        await cleanupGate.promise
        events.push('cleanup-done')
      })
      await runGate.promise
      lease.assertCurrent()
    },
    { cancelScopes: [alias], managedScope: 'primary' }
  )

  await Promise.resolve()

  const first = coordinator.cancelAndWait(alias).then(() => {
    firstDone = true
  })

  const second = coordinator.cancelAndWait(alias, async () => {
    events.push('teardown')
    teardownStarted.resolve()
    await teardownGate.promise
    events.push('teardown-done')
  })

  const next = coordinator.start('', 'next', async () => {
    nextStarted = true
  })

  await Promise.resolve()
  assert.deepEqual(events, ['cleanup'])
  assert.equal(firstDone, false)
  assert.equal(nextStarted, false)

  cleanupGate.resolve()
  await teardownStarted.promise
  assert.deepEqual(events, ['cleanup', 'cleanup-done', 'teardown'])
  assert.equal(firstDone, false)
  assert.equal(nextStarted, false)

  teardownGate.resolve()
  await Promise.all([first, second])
  await assert.rejects(running, (error: any) => error.kind === 'superseded')
  await next
  assert.deepEqual(events, ['cleanup', 'cleanup-done', 'teardown', 'teardown-done'])
  assert.equal(nextStarted, true)
})

test('a published alias and primary scope share one drain without an active bootstrap entry', async () => {
  const coordinator = createBootstrapCoordinator()
  const teardownGate = deferred()
  const teardownStarted = deferred()
  const alias = 'conn:imac::default'
  let replacementStarted = false

  const aliasDrain = coordinator.cancelAndWait([alias, ''], async () => {
    teardownStarted.resolve()
    await teardownGate.promise
  })

  await teardownStarted.promise

  const primaryDrain = coordinator.cancelAndWait('')

  const replacement = coordinator.start('', 'next', async () => {
    replacementStarted = true
  })

  await Promise.resolve()
  assert.equal(replacementStarted, false)

  teardownGate.resolve()
  await Promise.all([aliasDrain, primaryDrain])
  await replacement
  assert.equal(replacementStarted, true)
})

test('cancelAndWait force-cleans pending resources before awaiting rollback', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  let cleaned = 0

  const pending = coordinator.start('scope', 'x', async lease => {
    lease.onForceCleanup(async () => {
      cleaned++
    })
    await gate.promise
    lease.assertCurrent()
  })

  await Promise.resolve()
  const drain = coordinator.cancelAndWait('scope')
  await Promise.resolve()
  gate.resolve()
  await drain
  await assert.rejects(pending, (error: any) => error.kind === 'superseded')
  assert.equal(cleaned, 1)
})

test('a generation started during cancelAndWait cannot run before the drain completes', async () => {
  const coordinator = createBootstrapCoordinator()
  const oldGate = deferred()
  const events: string[] = []

  const old = coordinator.start('scope', 'old', async lease => {
    events.push('old-start')
    await oldGate.promise
    lease.assertCurrent()
  })

  await Promise.resolve()
  const drain = coordinator.cancelAndWait('scope')

  const next = coordinator.start('scope', 'new', async () => {
    events.push('new-start')

    return 'new'
  })

  await Promise.resolve()
  assert.deepEqual(events, ['old-start'])
  oldGate.resolve()
  await drain
  await assert.rejects(old, (error: any) => error.kind === 'superseded')
  assert.equal(await next, 'new')
  assert.deepEqual(events, ['old-start', 'new-start'])
})
