export default function Card({ title, kicker, right, children, className = '', hoverable = false, icon, onClick }) {
  // 传了 onClick 就把**整卡**变成可点区域（含键盘可达：Enter / Space）。
  // 「板块资金流」卡片要求"点击整卡弹窗"，只绑一个小按钮不够直观。
  const clickable = typeof onClick === 'function'
  return (
    <section
      className={`card ${hoverable ? 'hoverable' : ''} ${clickable ? 'clickable' : ''} ${className}`}
      onClick={clickable ? onClick : undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onClick()
              }
            }
          : undefined
      }
    >
      {(title || kicker || right || icon) && (
        <header className="card-head">
          {icon && <div className="card-icon">{icon}</div>}
          <div className="card-title-group">
            {kicker && <div className="kicker">{kicker}</div>}
            {title && <h3 className="card-title">{title}</h3>}
          </div>
          {right}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  )
}
