// 「深度分析」六段式版式 —— 按 SOP Step 7 的固定格式呈现。
//
// SOP（docs/项目/交易决策SOP.md Step 7）要求**禁止简写**：
//   一、核心结论（一句话定性）  二、产业端数据（硬指标）  三、催化事件
//   四、估值与业绩分化          五、后面盯 N 类信号        六、投资建议 + 风险点
// 本组件只负责**呈现**：不改内容、不补造结构；某段后端没给数据时，如实显示其说明文字
// （例如 B 区隔离拿不到资讯源，催化段就写清原因，而不是留个空壳或编事件）。
//
// 数据来源：后端结构化输出 `sections`（如 /api/real/holdings/{code}/detail）。
export default function DeepAnalysis({ sections, title = '深度分析' }) {
  if (!sections || !sections.conclusion) return null
  const { conclusion, data, catalyst, valuation, signals, advice, sources } = sections

  return (
    <div className="sub-card">
      <div
        style={{
          fontWeight: 600,
          marginBottom: 10,
        }}
      >
        {title}
      </div>

      {/* 一、核心结论 */}
      <section className="da-sec">
        <div className="da-sec-title">一、核心结论</div>
        <div className="da-verdict">
          <div>
            <b>结论：</b>
            {conclusion.verdict}
            {conclusion.trend_arrow ? <span style={{ marginLeft: 6 }}>{conclusion.trend_arrow}</span> : null}
          </div>
          <div style={{ marginTop: 5 }}>
            <b>一句话：</b>
            {conclusion.one_line}
          </div>
        </div>
      </section>

      {/* 二、硬指标 */}
      {data && (
        <section className="da-sec">
          <div className="da-sec-title">
            二、硬指标
            <span className="muted cell-note">{data.caption}</span>
          </div>
          <div className="da-table-wrap">
            <table className="da-table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>当前值</th>
                  <th>变化 / 说明</th>
                  <th>来源</th>
                </tr>
              </thead>
              <tbody>
                {(data.rows || []).map((r, i) => (
                  <tr key={i}>
                    <td className="nw">{r.indicator}</td>
                    <td className="nw">
                      <b>{r.value}</b>
                    </td>
                    <td>{r.change}</td>
                    <td className="muted">{r.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(data.facts || []).length > 0 && (
            <>
              <div className="da-sub-title">关键事实</div>
              <ul className="da-list">
                {(data.facts || []).map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      {/* 三、催化事件 */}
      {catalyst && (
        <section className="da-sec">
          <div className="da-sec-title">三、催化事件</div>
          {(catalyst.items || []).length > 0 ? (
            <ul className="da-list">
              {(catalyst.items || []).map((c, i) => (
                <li key={i}>
                  <b>{c.event}</b>
                  {c.impact ? `（${c.impact}）` : ''}
                  {c.certainty ? ` · ${c.certainty}` : ''}
                  {c.data ? ` —— ${c.data}` : ''}
                </li>
              ))}
            </ul>
          ) : (
            <div className="da-note">{catalyst.note}</div>
          )}
        </section>
      )}

      {/* 四、估值与业绩分化 */}
      {valuation && (
        <section className="da-sec">
          <div className="da-sec-title">四、估值与业绩分化（核心矛盾）</div>
          <div className="da-table-wrap">
            <table className="da-table">
              <thead>
                <tr>
                  <th>标的</th>
                  <th>指标</th>
                  <th>数值 / 备注</th>
                </tr>
              </thead>
              <tbody>
                {(valuation.rows || []).map((r, i) => (
                  <tr key={i}>
                    <td className="nw">{r.target}</td>
                    <td className="nw">{r.metric}</td>
                    <td>
                      <b>{r.value}</b>
                      {r.note ? <span className="da-inline-note">{r.note}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {valuation.conclusion && (
            <div className="da-note" style={{ marginTop: 8 }}>
              <b>结论：</b>
              {valuation.conclusion}
            </div>
          )}
        </section>
      )}

      {/* 五、后面盯 N 类信号 */}
      {(signals || []).length > 0 && (
        <section className="da-sec">
          <div className="da-sec-title">五、关注信号</div>
          <div className="da-table-wrap">
            <table className="da-table">
              <thead>
                <tr>
                  <th>优先级</th>
                  <th>信号</th>
                  <th>判断标准</th>
                  <th>当前状态</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => (
                  <tr key={i}>
                    <td className="nw" style={{ color: 'var(--warn, #b8860b)', letterSpacing: 1 }}>
                      {s.priority}
                    </td>
                    <td>{s.signal}</td>
                    <td className="muted">{s.threshold}</td>
                    <td className="nw">{s.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* 六、投资建议 + 风险点 */}
      {advice && (
        <section className="da-sec">
          <div className="da-sec-title">六、投资建议</div>
          {(advice.research || []).length > 0 && (
            <>
              <div className="da-sub-title">优先关注：</div>
              <ul className="da-list">
                {(advice.research || []).map((t, i) => (
                  <li key={i}>{t}</li>
                ))}
              </ul>
            </>
          )}
          {(advice.risks || []).length > 0 && (
            <>
              <div className="da-sub-title">风险点：</div>
              <ul className="da-list">
                {(advice.risks || []).map((t, i) => (
                  <li key={i}>{t}</li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      {(sources || []).length > 0 && (
        <div
          className="da-note"
          style={{ marginTop: 12, borderTop: '1px solid var(--line, #ececec)', paddingTop: 8 }}
        >
          <b>数据来源：</b>
          {(sources || []).join('；')}
        </div>
      )}
    </div>
  )
}
