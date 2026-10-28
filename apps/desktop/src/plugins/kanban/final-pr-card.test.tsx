import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

// Test harness registers the plugin locale, as the Desktop host does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import type * as KanbanApi from './api'
import { KanbanBoardPage } from './board'
import { en, KANBAN_LOCALES } from './i18n'

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof KanbanApi>()),
  fetchBoards: vi.fn(async () => ({ boards: [], current: '' })),
  fetchBoard: vi.fn(async () => ({
    assignees: [],
    tenants: [],
    columns: [
      {
        name: 'review',
        tasks: [
          {
            id: 'draft',
            title: 'Approved Draft',
            status: 'review',
            current_attention: { kind: 'awaiting-test', run_id: 1 }
          },
          {
            id: 'final',
            title: 'Selected final',
            status: 'review',
            current_attention: { kind: 'awaiting-test', run_id: 2, final_pr: true }
          }
        ]
      }
    ]
  })),
  fetchOrchestration: vi.fn(async () => ({ default_assignee: '' })),
  fetchProfiles: vi.fn(async () => ({ profiles: [] }))
}))

let disposeLocales: () => void
beforeEach(() => {
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})
afterEach(() => {
  cleanup()
  disposeLocales()
})

it('highlights only the selected final card without changing order or count', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const { container } = render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )

  await screen.findByText('Selected final')
  const cards = [...container.querySelectorAll('[draggable="true"]')]
  expect(cards).toHaveLength(2)
  expect(cards[0].textContent).toContain('Approved Draft')
  expect(cards[1].textContent).toContain('Selected final')
  expect(cards[0].classList.contains('border-primary/40')).toBe(false)
  expect(cards[1].classList.contains('border-primary/40')).toBe(true)
  expect(screen.getAllByText(en.attention['awaiting-test'])).toHaveLength(2)
  expect(screen.getAllByText(en.finalPr)).toHaveLength(1)
  client.clear()
})
