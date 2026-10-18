import { useRef, useState } from 'react'

// 统一的异步按钮。解决三个老问题：
// 1) 有的按钮点了没反应（无 loading 反馈）；2) 有的能连点触发重复请求；
// 3) 各页面 busy 文案/禁用逻辑各写一套，样子不一致。
// 用法：<AsyncButton onClick={async () => {...}} pendingText="抓取中…">抓取</AsyncButton>
export default function AsyncButton({
  onClick,
  children,
  pendingText,
  className = 'btn',
  disabled = false,
  ...rest
}) {
  const [pending, setPending] = useState(false)
  const lockRef = useRef(false)

  const handle = async (e) => {
    if (lockRef.current || disabled) return
    lockRef.current = true
    setPending(true)
    try {
      await onClick?.(e)
    } finally {
      lockRef.current = false
      setPending(false)
    }
  }

  return (
    <button
      className={className}
      onClick={handle}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      {...rest}
    >
      {pending && <span className="btn-spin" aria-hidden="true" />}
      {pending ? pendingText || children : children}
    </button>
  )
}
