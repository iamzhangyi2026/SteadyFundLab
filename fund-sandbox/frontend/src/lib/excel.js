// Excel 导出（SheetJS / xlsx）。把已映射好中文表头的纯对象数组写成 .xlsx 并触发下载。
import * as XLSX from 'xlsx'

// rows: 形如 [{ '列A': 1, '列B': 'x' }, ...]；fileName / sheetName 不带扩展名
// 返回导出行数（0 表示无数据）
export function exportRows(rows, { fileName = 'export', sheetName = 'Sheet1' } = {}) {
  if (!rows || !rows.length) return 0
  const ws = XLSX.utils.json_to_sheet(rows)
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, String(sheetName).slice(0, 31))
  XLSX.writeFile(wb, `${fileName}.xlsx`)
  return rows.length
}
