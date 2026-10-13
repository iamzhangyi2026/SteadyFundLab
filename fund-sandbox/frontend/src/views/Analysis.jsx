import { useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import EmptyState from '../components/EmptyState'
import DonutChart from '../components/DonutChart'
import Modal from '../components/Modal'
import { Icon } from '../components/Icons'
import { apiGet } from '../store/api'

// 板块资金流条。
// kind 只有 hot / risk 两种取值；缺失或为空时若不做兜底会渲染成 "bar-fill undefined"，
// 没有背景色、整条直接看不见。这里统一按数值符号兜底出 flat。
function barKind(b) {
  if (b.kind === 'hot' || b.kind === 'risk') return b.kind
  const v = Number(b.value) || 0
  if (v > 0) return 'hot'
  if (v < 0) return 'risk'
  return 'flat'
}

function BoardBars({ items }) {
  if (!items?.length) {
    return <EmptyState title="暂无板块信号" hint="板块资金流在后台抓取行情后写入，稍后再看" />
  }
  // 按资金流绝对值降序排成 TOP 排行（净流入/流出都靠前），并加排名号
  const sorted = [...items].sort((a, b) => Math.abs(b.value || 0) - Math.abs(a.value || 0))
  const max = Math.max(...sorted.map((b) => Math.abs(b.value) || 1), 1)
  return (
    <div>
      {sorted.map((b, idx) => {
        const w = (Math.abs(b.value) / max) * 100
        const kind = barKind(b)
        const tone = kind === 'hot' ? 'tone-up' : kind === 'risk' ? 'tone-down' : 'muted'
        return (
          <div className="bar-row" key={b.name}>
            <div className={`rank ${idx < 3 ? 'top' : ''}`}>{idx + 1}</div>
            <div className="name" title={b.name}>{b.name}</div>
            <div className="bar-track">
              <div className={`bar-fill ${kind}`} style={{ width: `${w}%` }} />
            </div>
            <div className="bar-val">
              <span className={tone}>
                {kind === 'hot' ? '+' : ''}{b.value} 亿
              </span>
            </div>
          </div>
        )
      })}
      {/* 图例：颜色代表什么必须写出来，不能靠猜 */}
      <div className="legend">
        <span className="li"><span className="sw" style={{ background: 'var(--up)' }} />资金净流入</span>
        <span className="li"><span className="sw" style={{ background: 'var(--down)' }} />资金净流出</span>
        <span className="li"><span className="sw" style={{ background: '#98a1b0' }} />基本持平</span>
      </div>
    </div>
  )
}

// 单基金行业暴露。
// 外层已经是 Card，这里再套 .card 会叠两层阴影和圆角，看起来发脏、层级也乱，
// 所以用扁平的 .sub-card 作子面板。
function FundExposure({ e }) {
  const alloc = e.allocation || []
  const max = Math.max(...alloc.map((a) => a.weight || 0), 1)
  return (
    <div className="sub-card" style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <strong>{e.fund_name}</strong>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {e.concentrated && <span className="tag warn">集中 {e.top_industry} {e.top_weight}%</span>}
          {e.drift?.claimed_industry ? (
            // 名实相符/不符是「好/坏」判断，不是涨跌，所以用 teal / danger，
            // 不用红绿——否则「名实相符」是绿色，会被读成「跌」。
            e.drift.drifted ? (
              <span className="tag danger">名实不符</span>
            ) : (
              <span className="tag ok">名实相符</span>
            )
          ) : null}
        </div>
      </div>
      {e.report_date && <div className="muted cell-note" style={{ marginTop: 4 }}>报告期 {e.report_date}</div>}
      {alloc.length === 0 && <div className="muted cell-note" style={{ marginTop: 8 }}>暂无行业配置数据</div>}
      {alloc.map((a) => (
        <div className="ind-row" key={a.industry}>
          <div className="top">
            <span>{a.industry}</span>
            <span className="num">{a.weight}%</span>
          </div>
          <div className="ind-track">
            <div className="ind-fill" style={{ width: `${(a.weight / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

// 人读叙述按首字符标记分流样式：⚠️ 警示 / ✅ 利好 / ℹ️ 提示 / 其余中性
function narClass(p) {
  if (p.startsWith('⚠️')) return 'nar-warn'
  if (p.startsWith('✅')) return 'nar-ok'
  if (p.startsWith('ℹ️')) return 'nar-info'
  return ''
}

export default function Analysis() {
  const analysis = useStore((s) => s.analysis.data)
  const boards = analysis?.boards
  const exposure = analysis?.exposure
  const narrative = analysis?.narrative || []
  const intelSummary = analysis?.intel_summary

  // 点击「板块资金流」卡片查看**全部**板块。
  // 后端 `/api/boards/all` 走 limit=0 全量分支。
  const [boardsOpen, setBoardsOpen] = useState(false)
  const [allBoards, setAllBoards] = useState(null)
  const [boardsLoading, setBoardsLoading] = useState(false)

  const openAllBoards = async () => {
    setBoardsOpen(true)
    if (allBoards) return // 已加载过则复用，不重复抓取
    setBoardsLoading(true)
    try {
      setAllBoards(await apiGet('/api/boards/all'))
    } catch (e) {
      setAllBoards({ items: [], error: String(e?.message || e) })
    } finally {
      setBoardsLoading(false)
    }
  }

  const perFund = exposure?.per_fund || []
  // 组合行业暴露（旧版分析页的环形图，重写时这份数据根本没渲染）
  const allocData = (exposure?.portfolio?.allocation || []).map((a) => ({
    name: a.industry,
    value: a.weight,
  }))

  return (
    <div className="stack">
      <Card className="col-7"
        title="板块资金流"
        kicker="BOARDS"
        icon={<Icon.board className="ico" />}
        hoverable
        onClick={openAllBoards}
        right={
          <span className="tag-row">
            <span className="muted cell-note">
              {boards?.as_of}
              {boards?.note ? ' · 仅资金流' : ''}
            </span>
            {/* 视觉提示（非按钮：整卡已可点，用 span 避免重复触发） */}
            <span className="link-btn" aria-hidden="true">
              查看全部 ›
            </span>
          </span>
        }
      >
        <BoardBars items={boards?.items} />
      </Card>

      <Card className="col-5"
        title="组合行业暴露"
        kicker="PORTFOLIO EXPOSURE"
        icon={<Icon.exposure className="ico" />}
        right={<span className="muted cell-note">按持仓穿透汇总</span>}
      >
        {!allocData.length ? (
          <EmptyState title="暂无持仓，无法汇总" hint="建仓后这里会显示组合在各行业的占比" />
        ) : (
          <>
            <DonutChart data={allocData} height={240} unit="%" />
            <div className="topbars-title">TOP 行业（按占比）</div>
            {allocData
              .slice()
              .sort((a, b) => b.value - a.value)
              .slice(0, 8)
              .map((a, idx) => {
                const w = (a.value / Math.max(...allocData.map((x) => x.value || 1), 1)) * 100
                return (
                  <div className="bar-row" key={a.name}>
                    <div className={`rank ${idx < 3 ? 'top' : ''}`}>{idx + 1}</div>
                    <div className="name" title={a.name}>{a.name}</div>
                    <div className="bar-track">
                      <div className="bar-fill accent" style={{ width: `${w}%` }} />
                    </div>
                    <div className="bar-val">{a.value}%</div>
                  </div>
                )
              })}
          </>
        )}
      </Card>

      {/* 
          它渲染的是同一份 `boards.items`，与上方「板块资金流」卡片数据完全重复。
          组件 BoardHeatmap.jsx 已随之移除（走回收站）。 */}

      <Card className="wide"
        title="持仓穿透 · 行业暴露"
        kicker="EXPOSURE"
        icon={<Icon.drill className="ico" />}
        right={intelSummary && <span className="tag accent">情报 {intelSummary.count} 条（落地 {intelSummary.landed}）</span>}
      >
        {!perFund.length && (
          <EmptyState title="暂无持仓暴露" hint="有持仓后，这里会按基金逐只拆出行业分布与漂移判断" />
        )}
        {perFund.map((e) => (
          <FundExposure key={e.fund_code} e={e} />
        ))}
      </Card>

      <Card className="wide" title="人读叙述" kicker="NARRATIVE" icon={<Icon.doc className="ico" />}>
        {!narrative.length && (
          <EmptyState title="暂无叙述" hint="叙述根据板块、持仓与情报自动生成，数据齐了才会出现" />
        )}
        {narrative.map((p, i) => (
          <p key={i} className={`nar ${narClass(p)}`}>{p}</p>
        ))}
      </Card>

      {/* 全部板块资金流弹窗 */}
      <Modal
        open={boardsOpen}
        title="全部板块资金流"
        onClose={() => setBoardsOpen(false)}
        width="660px"
      >
        {boardsLoading ? (
          <div className="muted cell-note">加载中…</div>
        ) : allBoards?.error ? (
          <EmptyState title="加载失败" hint={allBoards.error} />
        ) : (
          <>
            <div className="muted cell-note" style={{ marginBottom: 'var(--space-3)' }}>
              {allBoards?.as_of} · 共 {allBoards?.count ?? 0} 个板块
              {allBoards?.note ? ` · ${allBoards.note}` : ''}
            </div>
            <div className="modal-scroll">
              <BoardBars items={allBoards?.items} />
            </div>
          </>
        )}
      </Modal>
    </div>
  )
}
