import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { useProfilePrewarm } from './use-profile-prewarm'

const { prewarmProfileBackend } = vi.hoisted(() => ({ prewarmProfileBackend: vi.fn() }))

vi.mock('@/store/profile', () => ({ prewarmProfileBackend }))

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

it('requires the full 150ms hover dwell before prewarming', () => {
  vi.useFakeTimers()
  const { result } = renderHook(() => useProfilePrewarm('junior-dev'))

  act(() => result.current.startPrewarm())
  act(() => vi.advanceTimersByTime(149))
  expect(prewarmProfileBackend).not.toHaveBeenCalled()

  act(() => vi.advanceTimersByTime(1))
  expect(prewarmProfileBackend).toHaveBeenCalledWith('junior-dev')
})

it('cancels a pending prewarm when the pointer leaves', () => {
  vi.useFakeTimers()
  const { result } = renderHook(() => useProfilePrewarm('junior-dev'))

  act(() => {
    result.current.startPrewarm()
    result.current.cancelPrewarm()
    vi.advanceTimersByTime(150)
  })

  expect(prewarmProfileBackend).not.toHaveBeenCalled()
})
