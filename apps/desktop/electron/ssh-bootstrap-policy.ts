export const SSH_BOOTSTRAP_MAX_CONCURRENCY = 2
export const SSH_POOL_READY_TIMEOUT_MS = 75_000

export function sshReadyTimeoutMs(metadata: { managedScope?: unknown } | null | undefined) {
  return metadata?.managedScope === 'pool' ? SSH_POOL_READY_TIMEOUT_MS : undefined
}
