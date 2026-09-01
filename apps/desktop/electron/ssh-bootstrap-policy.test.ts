import { describe, expect, it } from 'vitest'

import {
  SSH_BOOTSTRAP_MAX_CONCURRENCY,
  SSH_BOOTSTRAP_MAX_QUEUED_NON_PRIMARY,
  SSH_BOOTSTRAP_RESERVED_PRIMARY_SLOTS,
  SSH_POOL_READY_TIMEOUT_MS,
  sshReadyTimeoutMs
} from './ssh-bootstrap-policy'

describe('SSH bootstrap policy', () => {
  it('keeps profile-pool startups bounded while allowing parallel progress', () => {
    expect(SSH_BOOTSTRAP_MAX_CONCURRENCY).toBe(2)
    expect(SSH_BOOTSTRAP_MAX_QUEUED_NON_PRIMARY).toBe(1)
    expect(SSH_BOOTSTRAP_RESERVED_PRIMARY_SLOTS).toBe(1)
  })

  it('gives cold profile backends enough time without slowing primary reconnects', () => {
    expect(sshReadyTimeoutMs({ managedScope: 'pool' })).toBe(SSH_POOL_READY_TIMEOUT_MS)
    expect(sshReadyTimeoutMs({ managedScope: 'primary' })).toBeUndefined()
    expect(sshReadyTimeoutMs(null)).toBeUndefined()
  })
})
