import { useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import Modal from '../components/Modal'
import AsyncButton from '../components/AsyncButton'
import { Icon } from '../components/Icons'
import EmptyState from '../components/EmptyState'
import StatStrip from '../components/StatStrip'
import { date } from '../lib/format'

// 情报类别：下拉与列表标签都要用中文，直接显示 policy / geo 这类键名看不懂
const CATS = [
  { v: 'policy', label: '政策' },
  { v: 'geo', label: '地缘' },
  { v: 'military', label: '军事' },
  { v: 'conference', label: '会议' },
  { v: 'company', label: '公司' },
  { v: 'macro', label: '宏观' },
]
const CAT_LABEL = Object.fromEntries(CATS.map((c) => [c.v, c.label]))
// 利好/利空属「市场情绪/状态」，按项目配色准则用语义色板、不沾红绿：
// 利好=teal(ok) ✓、利空=amber(warn) ⚠，并一律配文字，色弱用户也能读。
// 与「分析·人读叙述」保持一致（利好=teal），消除红绿在多处语义互相反转。
// 取值缺失时兜底成 neutral，避免拼出 "tag undefined" 变成没有底色的裸标签。
const DIR = { positive: 'ok', negative: 'warn', neutral: 'neutral' }
const DIR_LABEL = { positive: '利好', negative: '利空', neutral: '中性' }

export default function Intel() {
  const intel = useStore((s) => s.intel.data)
  const addIntel = useStore((s) => s.addIntel)
  const collectIntel = useStore((s) => s.collectIntel)
  const notify = useStore((s) => s.notify)
  const openExternal = useStore((s) => s.openExternal)

  // 阅读全文：走 openExternal（桌面端 = 应用内轻量浏览器）
  const openLink = (url) => {
    if (!url) return
    openExternal(url).then((r) => {
      if (!r?.ok) notify('打开失败', 'error')
    })
  }
  const [open, setOpen] = useState(false)

  const items = intel?.items || []

  const landed = items.filter((i) => i.status === 'landed').length
  const disclosed = items.length - landed
  const posCount = items.filter((i) => i.impact_dir === 'positive').length
  const negCount = items.filter((i) => i.impact_dir === 'negative').length
  const neuCount = items.filter((i) => i.impact_dir === 'neutral').length

  const onCollect = async () => {
    try {
      const r = await collectIntel()
      notify(`采集完成，新增 ${r?.collected ?? 0} 条`, 'success')
    } catch (e) {
      notify('采集失败：' + e.message, 'error')
    }
  }

  return (
    <div className="stack">
      <StatStrip
        className="wide"
        items={[
          { label: '情报总数', value: items.length },
          { label: '已落地', value: landed, tone: landed > 0 ? 'ok' : undefined },
          { label: '披露中', value: disclosed, tone: disclosed > 0 ? 'warn' : undefined },
          { label: '利好', value: posCount, tone: posCount > 0 ? 'ok' : undefined },
          { label: '利空', value: negCount, tone: negCount > 0 ? 'danger' : undefined },
          { label: '中性', value: neuCount },
        ]}
      />
      <Card className="wide"
        title="情报库"
        kicker="INTEL"
        right={
          <div style={{ display: 'flex', gap: 8 }}>
            <AsyncButton className="btn btn-sm" onClick={onCollect} pendingText="采集中…" title="后台每 30 分钟自动采集一次；此按钮用于立即再采集一轮">
              <Icon.refresh className="ico" /> 立即采集
            </AsyncButton>
            <button className="btn btn-sm btn-primary" onClick={() => setOpen(true)}>
              <Icon.plus className="ico" /> 录入
            </button>
          </div>
        }
      >
        <div className="list">
          {items.map((it) => (
            <div className="list-item" key={it.id}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }}>
                <span className="tag neutral">{CAT_LABEL[it.category] || it.category}</span>
                <span className={`tag ${it.status === 'landed' ? 'accent' : 'warn'}`}>
                  {it.status === 'landed' ? '已落地' : '披露中'}
                </span>
                <span className={`tag ${DIR[it.impact_dir] || 'neutral'}`}>
                  {DIR_LABEL[it.impact_dir] || '中性'}
                </span>
                {it.signal_level && (
                  <span className={`tag ${it.signal_level === '强' ? 'accent' : 'neutral'}`}>
                    信号 {it.signal_level}
                  </span>
                )}
              </div>
              <h4>{it.title}</h4>
              {it.detail && <p>{it.detail}</p>}
              <div className="meta">
                <span>{date(it.event_date)}</span>
                {it.impact_boards && <span>影响板块：{it.impact_boards}</span>}
                {(it.source_name || it.source) && <span>来源：{it.source_name || it.source}</span>}
                {/* 后端已把 source 拆成 source_name / source_url；以前读 it.source，
                    既拖着整串 URL 显示、也没有「阅读全文」按钮。 */}
                {it.source_url && (
                  <button className="link-btn" onClick={() => openLink(it.source_url)}>
                    <Icon.external className="ico" style={{ width: 13, height: 13 }} /> 阅读全文
                  </button>
                )}
              </div>
            </div>
          ))}
          {!items.length && <EmptyState title="暂无情报" hint="市场事件由后台自动采集（每 30 分钟一轮）；也可点「录入」手动添加。" />}
        </div>
      </Card>

      <IntelModal open={open} onClose={() => setOpen(false)} onSubmit={addIntel} notify={notify} />
    </div>
  )
}

function IntelModal({ open, onClose, onSubmit, notify }) {
  const [form, setForm] = useState({
    event_date: new Date().toISOString().slice(0, 10),
    category: 'policy',
    title: '',
    detail: '',
    status: 'disclosed',
    impact_boards: '',
    impact_dir: 'neutral',
    source: '',
  })
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const submit = async () => {
    if (!form.title) {
      notify('请填写标题', 'error')
      return
    }
    try {
      await onSubmit(form)
      notify('情报已入库', 'success')
      onClose()
    } catch (e) {
      notify('录入失败：' + e.message, 'error')
    }
  }

  return (
    <Modal open={open} title="录入情报" onClose={onClose}>
      <div className="row2">
        <div className="field">
          <label>事件日期</label>
          <input type="date" value={form.event_date} onChange={set('event_date')} />
        </div>
        <div className="field">
          <label>类别</label>
          <select value={form.category} onChange={set('category')}>
            {CATS.map((c) => (
              <option key={c.v} value={c.v}>{c.label}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="field">
        <label>标题</label>
        <input
          value={form.title}
          onChange={set('title')}
          placeholder="如：某半导体产线拟扩建"
          data-autofocus
        />
      </div>
      <div className="field">
        <label>详情</label>
        <textarea rows={3} value={form.detail} onChange={set('detail')} />
      </div>
      <div className="row2">
        <div className="field">
          <label>状态</label>
          <select value={form.status} onChange={set('status')}>
            <option value="disclosed">披露中（放风）</option>
            <option value="landed">已落地</option>
          </select>
        </div>
        <div className="field">
          <label>影响方向</label>
          <select value={form.impact_dir} onChange={set('impact_dir')}>
            <option value="positive">利好</option>
            <option value="negative">利空</option>
            <option value="neutral">中性</option>
          </select>
        </div>
      </div>
      <div className="row2">
        <div className="field">
          <label>影响板块</label>
          <input value={form.impact_boards} onChange={set('impact_boards')} placeholder="如 半导体,新能源" />
        </div>
        <div className="field">
          <label>来源</label>
          <input value={form.source} onChange={set('source')} />
        </div>
      </div>
      <AsyncButton
        className="btn btn-primary"
        style={{ width: '100%' }}
        onClick={submit}
        pendingText="入库中…"
      >
        入库
      </AsyncButton>
    </Modal>
  )
}
