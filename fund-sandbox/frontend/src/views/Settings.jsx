import { useEffect, useMemo, useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import EmptyState from '../components/EmptyState'
import ConfirmDialog from '../components/ConfirmDialog'
import AsyncButton from '../components/AsyncButton'
import { date, time } from '../lib/format'
import { Icon } from '../components/Icons'

// 可编辑设置项定义（与后端 /api/settings 的 schema 对应）。
// 每项带一句人话说明——设置页是给使用者看的，不是给配置文件看的，
// 每个数字「是什么、改了影响什么」必须就地讲清楚。
const GROUPS = [
  {
    title: '资金规则',
    items: [
      { key: 'monthly_quota', label: '每月新增预算（元）', type: 'number', min: 1, hint: '累积制：每月往池子里累加，当月不用不清零' },
      { key: 'per_fund_weekly', label: '单基金每周定投（元）', type: 'number', min: 1, hint: '新建定投计划的默认每期金额' },
    ],
  },
  {
    title: '运行方式',
    items: [
      {
        key: 'mode', label: '决策模式', type: 'select',
        options: [
          ['auto', '自动（推荐）：规则引擎常驻兜底，AI 在线时接管'],
          ['rule', '纯规则引擎：AI 完全不介入'],
          ['external', '只听外部 AI：AI 离线时系统停摆'],
        ],
        hint: '推荐「自动」——对话不可能 24 小时开着，纯外部模式 AI 一离线，定投该扣的款就漏了',
      },
      { key: 'heartbeat_timeout_hours', label: 'AI 心跳超时（小时）', type: 'number', min: 1, max: 168, hint: '超过这个时长没有新心跳 → 判定 AI 离线 → 规则引擎接管' },
    ],
  },
  {
    title: '风控边界',
    items: [
      { key: 'max_amount_per_order', label: '单笔金额上限（元）', type: 'number', min: 1, hint: '任何一笔下单都不能超过这个数' },
      { key: 'max_orders_per_day', label: '单日下单次数上限', type: 'number', min: 1, max: 100, hint: '防止失控连环下单' },
      { key: 'max_single_position_pct', label: '单基金仓位上限（%）', type: 'number', min: 1, max: 100, hint: '一只基金占持仓市值不能超过这个比例' },
      { key: 'max_drawdown_stop_pct', label: '账户回撤止损线（%）', type: 'number', min: 1, max: 95, hint: '账户从高点回撤触及此线 → 暂停自动交易并告警' },
    ],
  },
  {
    title: '费率',
    items: [
      { key: 'purchase_discount', label: '申购费折扣（0~1）', type: 'number', min: 0, max: 1, hint: '实际申购费 = 基金原始费率 × 折扣。支付宝是部分基金 1.25 折起，默认 0.125' },
    ],
  },
]

// 原始值 → 输入框字符串
const toStr = (v) => (v == null ? '' : String(v))

export default function Settings() {
  const account = useStore((s) => s.account.data)
  const status = useStore((s) => s.status.data)
  const settingsRes = useStore((s) => s.settings)
  const updateSettings = useStore((s) => s.updateSettings)
  const setEmergencyStop = useStore((s) => s.setEmergencyStop)
  const takeSnapshot = useStore((s) => s.takeSnapshot)
  const notify = useStore((s) => s.notify)

  const [stopConfirm, setStopConfirm] = useState(false)
  const [saveConfirm, setSaveConfirm] = useState(false)

  const fetchSettings = useStore((s) => s.fetch_settings)
  useEffect(() => {
    // settings 不在全局轮询列表（改完保存时会主动刷新），进页面时主动拉一次
    fetchSettings()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 设置表单：key -> 输入框字符串
  const [form, setForm] = useState({})
  const items = settingsRes?.data?.items || []

  useEffect(() => {
    if (!items.length) return
    setForm((prev) => {
      // 只在表单还是「空/未初始化」时整体填充；已编辑过的内容不被轮询覆盖
      if (Object.keys(prev).length) return prev
      const next = {}
      for (const it of items) next[it.key] = toStr(it.value)
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsRes?.data])

  // 相对当前生效值，找出了「真的改了」的字段
  const changes = useMemo(() => {
    const cur = {}
    for (const it of items) cur[it.key] = it.value
    const out = {}
    for (const g of GROUPS) {
      for (const f of g.items) {
        const raw = (form[f.key] ?? '').trim()
        if (!raw) continue
        const v = f.type === 'select' ? raw : Number(raw)
        if (Number.isNaN(v)) continue
        if (cur[f.key] !== v) out[f.key] = v
      }
    }
    return out
  }, [form, items])

  const rules = account?.capital_rules || {}
  const stopped = status?.emergency_stop

  const onStop = async () => {
    setStopConfirm(false)
    try {
      await setEmergencyStop(!stopped, stopped ? '恢复自动交易' : '手动紧急暂停')
      notify(stopped ? '已恢复自动交易' : '已紧急暂停', stopped ? 'success' : 'error')
    } catch (e) {
      notify('操作失败：' + e.message, 'error')
    }
  }

  const onSnapshot = async () => {
    try {
      await takeSnapshot()
      notify('已记录组合快照', 'success')
    } catch (e) {
      notify('失败：' + e.message, 'error')
    }
  }

  const confirmSave = async () => {
    setSaveConfirm(false)
    try {
      await updateSettings(changes)
      notify(`已保存 ${Object.keys(changes).length} 项设置，立即生效`, 'success')
    } catch (e) {
      notify('保存失败：' + e.message, 'error')
    }
  }

  const setField = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))
  const labelOf = (key) => {
    for (const g of GROUPS) {
      const f = g.items.find((x) => x.key === key)
      if (f) return f.label
    }
    return key
  }

  return (
    <div className="stack">
      <Card className="wide" title="参数设置" kicker="SETTINGS" icon={<Icon.sliders className="ico" />}
        right={
          <button
            className="btn btn-sm btn-primary"
            disabled={!Object.keys(changes).length}
            onClick={() => setSaveConfirm(true)}
            title={Object.keys(changes).length ? '保存并立即生效' : '没有改动'}
          >
            保存设置{Object.keys(changes).length ? `（${Object.keys(changes).length} 项改动）` : ''}
          </button>
        }
      >
        <div className="muted cell-note" style={{ lineHeight: 1.7, marginBottom: 12 }}>
          改动保存后<b>立即生效</b>，不需要重启。真正的配置写在 config.toml，这里改的就是它。
        </div>
        {!items.length && settingsRes?.loading && <div className="loading">加载设置…</div>}
        {!items.length && !settingsRes?.loading && (
          <EmptyState title="设置加载失败" hint="后端未就绪或版本过旧，重启应用后再试" />
        )}
        {items.length > 0 && (
          <div style={{ display: 'grid', gap: 16 }}>
            {GROUPS.map((g) => (
              <div key={g.title}>
                <div className="topbars-title" style={{ marginBottom: 8 }}>{g.title}</div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
                  {g.items.map((f) => {
                    const changed = f.key in changes
                    return (
                      <div className="field" key={f.key} style={{ margin: 0 }}>
                        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          {f.label}
                          {changed && <span className="tag accent" style={{ fontSize: 11 }}>已改</span>}
                        </label>
                        {f.type === 'select' ? (
                          <select value={form[f.key] ?? ''} onChange={setField(f.key)}>
                            {f.options.map(([v, t]) => (
                              <option key={v} value={v}>{t}</option>
                            ))}
                          </select>
                        ) : (
                          <input
                            type="number"
                            value={form[f.key] ?? ''}
                            min={f.min}
                            max={f.max}
                            step="any"
                            onChange={setField(f.key)}
                          />
                        )}
                        {f.hint && <div className="muted cell-note" style={{ marginTop: 4, lineHeight: 1.6 }}>{f.hint}</div>}
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card className="col-7"
        title="运行模式"
        kicker="MODE"
        icon={<Icon.settings className="ico" />}
        right={
          <button
            className={`btn btn-sm ${stopped ? 'btn-primary' : 'btn-danger'}`}
            onClick={() => setStopConfirm(true)}
          >
            {stopped ? '恢复自动交易' : '紧急暂停'}
          </button>
        }
      >
        <div className="grid-2">
          <Rule label="实际生效" v={status?.effective_mode || '—'} />
          <Rule label="AI 在线" v={status?.ai_online ? '是' : '否'} />
          <Rule label="上次活跃" v={time(status?.last_seen)} />
          <Rule label="数据日期" v={date(account?.date)} />
        </div>
        <div style={{ marginTop: 12 }}>
          <AsyncButton className="btn btn-sm" onClick={onSnapshot} pendingText="记录中…">
            记录组合快照
          </AsyncButton>
        </div>
      </Card>

      <ConfirmDialog
        open={saveConfirm}
        title="保存设置"
        message={
          Object.keys(changes).length
            ? '将修改以下参数并立即生效：\n' +
              Object.entries(changes).map(([k, v]) => `${labelOf(k)} → ${v}`).join('\n')
            : ''
        }
        confirmText="保存"
        onConfirm={confirmSave}
        onClose={() => setSaveConfirm(false)}
      />
      <ConfirmDialog
        open={stopConfirm}
        title={stopped ? '恢复自动交易？' : '紧急暂停？'}
        message={
          stopped
            ? '恢复后，定投计划与自动下单将重新按规则执行。'
            : '暂停后，所有自动交易与定投执行会立即停止，直到你手动恢复。已提交的委托不受影响。'
        }
        confirmText={stopped ? '恢复自动交易' : '确认暂停'}
        danger={!stopped}
        onConfirm={onStop}
        onClose={() => setStopConfirm(false)}
      />
    </div>
  )
}

function Rule({ label, v }) {
  return (
    <div className="tile" style={{ padding: '12px 14px' }}>
      <div className="tile-label">{label}</div>
      <div className="tile-value status-num" style={{ fontSize: 'var(--fs-tile, 18px)' }}>{v}</div>
    </div>
  )
}
