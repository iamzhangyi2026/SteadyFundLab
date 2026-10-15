import { useStore } from '../store/useStore'
import Card from '../components/Card'
import EmptyState from '../components/EmptyState'
import AsyncButton from '../components/AsyncButton'
import StatStrip from '../components/StatStrip'
import { time } from '../lib/format'

// 数据健康度三张汉化表。缺了它们界面会直接显示 em_fund_info / purchase_rule
// 这类英文键名，可读性很差——旧版有，重写时漏掉了。
const SRC_MAP = {
  cache: '缓存 / 交易日历',
  em_fund_info: '东方财富基金信息',
  em_purchase: '东方财富申购规则',
  sina: '新浪财经',
  ths_industry_summary: '同花顺行业汇总',
}
const TYPE_MAP = {
  board: '板块',
  calendar: '交易日历',
  nav: '基金净值',
  purchase_rule: '申购规则',
}
const STATUS_MAP = { ok: '正常', failed: '失败', fail: '失败' }
// 状态用语义色：正常=teal，失败=danger。红绿只留给涨跌数值，
// 否则「正常」显示成绿色、「失败」显示成红色，会和净值涨跌的红绿撞含义。
const STATUS_TONE = { ok: 'ok', failed: 'danger', fail: 'danger' }

// 未收录的取值原样显示，不做“假装翻译”
const zh = (map, v) => map[v] || v || '—'

export default function Health() {
  const health = useStore((s) => s.health.data)
  const refreshFunds = useStore((s) => s.refreshFunds)
  const notify = useStore((s) => s.notify)
  const items = health?.items || []

  const okCount = items.filter((i) => i.status === 'ok').length
  const failCount = items.length - okCount
  const totalRecords = items.reduce((s, i) => s + (i.record_count || 0), 0)

  const onRefreshSource = async () => {
    try {
      await refreshFunds()
      notify('已触发数据源刷新，稍后回到本页查看记录', 'success')
    } catch (e) {
      notify('刷新失败：' + e.message, 'error')
    }
  }

  return (
    <div className="stack">
      <StatStrip
        className="wide"
        items={[
          { label: '数据源', value: items.length },
          { label: '正常', value: okCount, tone: okCount > 0 ? 'ok' : undefined },
          { label: '失败', value: failCount, tone: failCount > 0 ? 'danger' : undefined },
          { label: '记录总数', value: totalRecords },
        ]}
      />
      <Card
        className="wide"
        title="数据健康度"
        kicker="DATA HEALTH"
        right={
          <span className="tag-row">
            <span className={`tag ${failCount ? 'warn' : 'ok'}`}>
              {okCount}/{items.length} 正常
            </span>
            <AsyncButton className="btn btn-sm" onClick={onRefreshSource} pendingText="刷新中…">
              刷新数据源
            </AsyncButton>
          </span>
        }
      >
        {!items.length && (
          <EmptyState
            title="暂无健康记录"
            hint="刷新数据或新增基金时自动写入；也可手动触发一次。"
            action={
              <AsyncButton className="btn btn-primary btn-sm" onClick={onRefreshSource} pendingText="刷新中…">
                刷新数据源
              </AsyncButton>
            }
          />
        )}
        {items.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>数据源</th>
                <th>类型</th>
                <th>状态</th>
                <th className="right">记录数</th>
                <th>说明</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i, idx) => (
                <tr key={idx}>
                  <td>
                    <strong>{zh(SRC_MAP, i.source)}</strong>
                  </td>
                  <td className="muted">{zh(TYPE_MAP, i.data_type)}</td>
                  <td>
                    <span className={`tag ${STATUS_TONE[i.status] || 'info'}`}>
                      {zh(STATUS_MAP, i.status)}
                    </span>
                  </td>
                  <td className="num">{i.record_count}</td>
                  <td className="muted cell-note">{i.message}</td>
                  <td className="muted cell-note">{time(i.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
