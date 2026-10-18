export default function Card({ title, kicker, right, children, className = '', hoverable = false, icon, onClick }) {
  // onClick 让整卡可由鼠标或键盘打开；子按钮保留自身键盘行为，不触发卡片动作。
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
              if (e.target !== e.currentTarget) return
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
