import { useStore } from '../store/useStore'

// 操作反馈。内容来自 store.toast，3.2s 后自动消失，也可手动关闭。
// 用 aria-live 包裹，让读屏软件在不抢焦点的前提下播报结果。
export default function Toast() {
  const toast = useStore((s) => s.toast)
  const clearToast = useStore((s) => s.clearToast)
  return (
    <div aria-live="polite" role="status" aria-atomic="true">
      {toast && (
        <div className={`toast ${toast.type}`}>
          <span>{toast.message}</span>
          <button className="toast-x" onClick={clearToast} aria-label="关闭提示">
            ×
          </button>
        </div>
      )}
    </div>
  )
}
