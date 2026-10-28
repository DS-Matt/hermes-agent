import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { en, KANBAN_LOCALES } from './i18n'
import type { KanbanTask } from './types'
import { isFinalPr, TaskAttention } from './ui'

let disposeLocales: () => void
beforeEach(() => {
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})
afterEach(() => {
  cleanup()
  disposeLocales()
})

const task: KanbanTask = { id: 't_question', title: 'Original title', status: 'review' }

describe('current task attention', () => {
  it.each(['awaiting-answer', 'awaiting-test', 'run-failed'] as const)('shows a distinct %s badge', kind => {
    render(<TaskAttention task={{ ...task, current_attention: { kind, run_id: 9 } }} />)
    expect(screen.getByText(en.attention[kind])).toBeTruthy()
  })

  it('does not guess a question from legacy prose', () => {
    const { container } = render(<TaskAttention task={{ ...task, latest_summary: 'Please answer my question' }} />)
    expect(container.textContent).toBe('')
  })

  it('clears a question when the authoritative field disappears', () => {
    const { rerender } = render(
      <TaskAttention task={{ ...task, current_attention: { kind: 'awaiting-answer', run_id: 9 } }} />
    )

    expect(screen.getByText(en.attention['awaiting-answer'])).toBeTruthy()
    rerender(<TaskAttention task={task} />)
    expect(screen.queryByText(en.attention['awaiting-answer'])).toBeNull()
  })

  it('clears stale attention during an optimistic status transition', () => {
    const { container } = render(
      <TaskAttention task={{ ...task, status: 'running', current_attention: { kind: 'awaiting-answer', run_id: 9 } }} />
    )

    expect(container.textContent).toBe('')
  })

  it('shows the concrete detail as escaped text', () => {
    const summary = '<img src=x onerror="alert(1)"> Which option should I use?'

    const { container } = render(
      <TaskAttention detail task={{ ...task, current_attention: { kind: 'awaiting-answer', run_id: 9, summary } }} />
    )

    expect(screen.getByText(summary)).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('selected final PR attention', () => {
  it('keeps the testing label and adds Final PR in card and drawer', () => {
    const selected = { ...task, current_attention: { kind: 'awaiting-test' as const, run_id: 9, final_pr: true } }
    const { rerender } = render(<TaskAttention task={selected} />)
    expect(screen.getByText(en.attention['awaiting-test'])).toBeTruthy()
    expect(screen.getByText(en.finalPr)).toBeTruthy()
    expect(isFinalPr(selected)).toBe(true)
    rerender(<TaskAttention detail task={selected} />)
    expect(screen.getByText(en.finalPr)).toBeTruthy()
    rerender(<TaskAttention task={{ ...selected, status: 'ready' }} />)
    expect(screen.queryByText(en.finalPr)).toBeNull()
    expect(isFinalPr({ ...selected, status: 'ready' })).toBe(false)
  })

  it.each(['awaiting-answer', 'run-failed'] as const)('does not highlight %s even with a stale marker', kind => {
    const stale = { ...task, current_attention: { kind, run_id: 9, final_pr: true } }
    render(<TaskAttention task={stale} />)
    expect(screen.queryByText(en.finalPr)).toBeNull()
    expect(isFinalPr(stale)).toBe(false)
  })

  it('keeps approved Drafts and older backends unhighlighted', () => {
    const draft = { ...task, current_attention: { kind: 'awaiting-test' as const, run_id: 9 } }
    render(<TaskAttention task={draft} />)
    expect(screen.getByText(en.attention['awaiting-test'])).toBeTruthy()
    expect(screen.queryByText(en.finalPr)).toBeNull()
    expect(isFinalPr(draft)).toBe(false)
    expect(isFinalPr(task)).toBe(false)
  })
})
