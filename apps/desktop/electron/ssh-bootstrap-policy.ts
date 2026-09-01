export const SSH_BOOTSTRAP_MAX_CONCURRENCY = 2
// Non-primary SSH bootstraps may use one slot; the other stays available for primary.
export const SSH_BOOTSTRAP_RESERVED_PRIMARY_SLOTS = 1
// At most one pool bootstrap may wait behind the active pool bootstrap. This
// keeps the renderer's bounded cold-descriptor budget honest: one queued boot,
// one active boot, plus probe overhead. Further clicks fail fast and can retry
// instead of timing out while an unowned SSH process starts later.
export const SSH_BOOTSTRAP_MAX_QUEUED_NON_PRIMARY = 1
export const SSH_POOL_READY_TIMEOUT_MS = 75_000

export function sshReadyTimeoutMs(metadata: { managedScope?: unknown } | null | undefined) {
  return metadata?.managedScope === 'pool' ? SSH_POOL_READY_TIMEOUT_MS : undefined
}
