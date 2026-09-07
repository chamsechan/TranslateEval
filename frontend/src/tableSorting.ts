import type { TableColumnsType } from 'antd'

type SortValue = string | number | null | undefined
const textOrder = new Intl.Collator('zh-CN')

export const statusOrder: Record<string, number> = {
  queued: 0, preprocessing: 1, running: 2, cancelling: 3, completed: 4,
  partial_cancelled: 5, cancelled: 6, partial_failed: 7, failed: 8, unscored: 9,
}

/** For complete in-memory datasets only; paginated API tables sort on the server. */
export function sortableColumns<T>(columns: TableColumnsType<T>, selectors: Record<string, (row: T) => SortValue>): TableColumnsType<T> {
  return columns.map(column => {
    const value = selectors[String(column.key)]
    if (!value) return column
    return {
      ...column,
      sortDirections: ['ascend', 'descend'],
      sorter: (left, right, order) => {
        const a = value(left)
        const b = value(right)
        // Ant Design reverses the comparator for descending order. Compensate
        // for empty values so they remain last instead of masquerading as zero.
        if (a == null || b == null) {
          if (a == null && b == null) return 0
          return (a == null ? 1 : -1) * (order === 'descend' ? -1 : 1)
        }
        return typeof a === 'number' && typeof b === 'number' ? a - b : textOrder.compare(String(a), String(b))
      },
    }
  })
}
