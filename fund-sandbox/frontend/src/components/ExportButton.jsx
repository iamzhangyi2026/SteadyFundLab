// 一键导出 Excel 按钮（D4）。从 store 当前数据生成 .xlsx 下载，纯点按无键盘流。
import { useState } from 'react'
import { Icon } from './Icons'
import { exportRows } from '../lib/excel'

export default function ExportButton({ rows, fileName, sheetName, disabled, notify, label = '导出 Excel' }) {
  const [busy, setBusy] = useState(false)

  const onClick = () => {
    if (!rows || !rows.length) {
      notify && notify('暂无可导出数据', 'info')
      return
    }
    setBusy(true)
    try {
      const n = exportRows(rows, { fileName, sheetName })
      notify && notify(`已导出 ${n} 行到 Excel`, 'success')
    } catch (e) {
      notify && notify('导出失败：' + (e?.message || e), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <button className="btn btn-sm" onClick={onClick} disabled={disabled || busy}>
      <Icon.download className="ico" /> {busy ? '导出中…' : label}
    </button>
  )
}
