import assert from 'node:assert/strict'

import { planLookup } from '../src/lib/ticketStoreSearch.ts'

function expectPlan(input: string, expected: Record<string, unknown>) {
  const actual = planLookup(input)
  for (const [key, value] of Object.entries(expected)) {
    assert.deepEqual(actual[key as keyof typeof actual], value, `${input}: ${key}`)
  }
}

expectPlan('2008', { kind: 'explicit_store', storeId: '2008' })
expectPlan('#1048', { kind: 'ticket_ids', ids: [1048] })
expectPlan('#906', { kind: 'ticket_ids', ids: [906] })
expectPlan('950056', { kind: 'ticket_ids', ids: [950056] })
expectPlan('store 2008', { kind: 'store_from_text', storeId: '2008' })
expectPlan('950056, 950057', { kind: 'ticket_ids', ids: [950056, 950057] })
expectPlan('906', { kind: 'invalid' })

console.log('ticket/store lookup QA: 7 cases passed')
