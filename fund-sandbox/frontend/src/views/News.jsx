import NewsRotator from '../components/NewsRotator'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import AsyncButton from '../components/AsyncButton'
import { Icon } from '../components/Icons'
import EmptyState from '../components/EmptyState'
import { time } from '../lib/format'

export default function News() {
  const marketNews = useStore((s) => s.marketNews.data)
  const news = useStore((s) => s.news.data)
  const refreshNews = useStore((s) => s.refreshNews)
  const openExternal = useStore((s) => s.openExternal)
  const notify = useStore((s) => s.notify)

  const onRefresh = async () => {
    try {
      const r = await refreshNews()
      notify(`抓取完成，新增 ${r?.fetched ?? 0} 条`, 'success')
    } catch (e) {
      notify('抓取失败：' + e.message, 'error')
    }
  }

  const openLink = (url) => {
    if (!url) return
    openExternal(url).then((r) => {
      if (!r?.ok) notify('打开失败', 'error')
    })
  }

  const mItems = marketNews?.items || []
  const nItems = news?.items || []

  // 头条轮播源：合并财经资讯 + 基金公告，按发布时间倒序取前 10 条
  const rotatorItems = [...mItems, ...nItems]
    .filter((it) => it.title)
    .sort((a, b) => (b.pub_time || 0) - (a.pub_time || 0))
    .slice(0, 10)

  return (
    <div className="stack">
      {/* 头条是本页最该先看到的东西，占满整行，避免挤在半栏导致标题折行 */}
      <NewsRotator className="wide" items={rotatorItems} onOpen={openLink} />
      {/* 「财经资讯」卡片已移除：与总览「市场最新动态」同源（/api/market-news），
          避免同一份资讯在两处重复展示。 */}
      <Card className="wide"
        title="基金公告 / 重仓股"
        kicker="NEWS FEED"
        icon={<Icon.bell className="ico" />}
        right={
          <span className="tag-row">
            <span className="muted cell-note">{nItems.length} 条</span>
            <AsyncButton className="btn btn-sm" onClick={onRefresh} pendingText="抓取中…">
              <Icon.refresh className="ico" /> 抓取最新
            </AsyncButton>
          </span>
        }
      >
        <div className="list">
          {nItems.map((it) => (
            <div className="list-item" key={it.id}>
              <h4>{it.title}</h4>
              {it.summary && <p>{it.summary}</p>}
              <div className="meta" style={{ marginTop: 4 }}>
                {/* 公告/重仓股是内容分类，不是涨跌，所以不用绿色，避免被当成「跌」 */}
                <span className={`tag ${it.kind === 'fund_announce' ? 'info' : 'neutral'}`}>
                  {it.kind === 'fund_announce' ? '基金公告' : '重仓股'}
                </span>
                {it.fund_name && <span>基金 {it.fund_name}</span>}
                {it.related_stock && <span>相关股票 {it.related_stock}</span>}
              </div>
              <div className="meta">
                <span>{it.source}</span>
                <span>{time(it.pub_time)}</span>
                {/* 公告/重仓股表里的字段叫 url（不是 link）；以前只读 it.link，
                    永远 undefined → 这一栏从来没有「阅读全文」按钮。 */}
                {(it.link || it.url) && (
                  <button className="link-btn" onClick={() => openLink(it.link || it.url)}>
                    <Icon.external className="ico" style={{ width: 13, height: 13 }} /> 阅读全文
                  </button>
                )}
              </div>
            </div>
          ))}
          {!nItems.length && (
            <EmptyState
              title="暂无公告"
              hint="点「抓取最新」拉取交易池基金的公告与重仓股动态"
              action={
                <AsyncButton className="btn btn-primary btn-sm" onClick={onRefresh} pendingText="抓取中…">
                  抓取最新
                </AsyncButton>
              }
            />
          )}
        </div>
      </Card>
    </div>
  )
}
