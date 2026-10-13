// 定投计划周期进度算法：按频率推算「距下次定投的已过进度」与「剩余天数」。
// 因 dingtou_plan 表无累积/目标字段，进度以「本周期已流逝占比」呈现，数据自洽。

const MS_DAY = 86400000

export function daysInMonth(y, m) {
  return new Date(y, m + 1, 0).getDate()
}

// 返回 { progress: 0..1（本周期已过进度，用于环形填充）, daysLeft: 距下次执行天数 }
export function planProgress(p, now = new Date()) {
  if (!p) return { progress: 0, daysLeft: 0 }

  const dayStart = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const today = dayStart(now)

  if (p.frequency === 'weekly') {
    // WEEKDAYS 下标：0=周一..6=周日；JS getDay：0=周日..6=周六
    const wd = Number(p.weekday) || 0
    const targetJs = (wd + 1) % 7
    const todayJs = today.getDay()
    const daysSince = (todayJs - targetJs + 7) % 7 // 今天即执行日 → 0
    const progress = daysSince / 7
    const daysLeft = daysSince === 0 ? 7 : 7 - daysSince
    return { progress, daysLeft }
  }

  // monthly：day_of_month 取值 1..28
  const d = Math.min(Math.max(Number(p.day_of_month) || 1, 1), 28)
  const y = today.getFullYear()
  const m = today.getMonth()
  const occThisMonth = Math.min(d, daysInMonth(y, m))
  let lastDate = new Date(y, m, occThisMonth)
  if (lastDate > today) {
    const pm = m - 1
    const py = pm < 0 ? y - 1 : y
    const pmOcc = Math.min(d, daysInMonth(py, (pm + 12) % 12))
    lastDate = new Date(py, (pm + 12) % 12, pmOcc)
  }
  let nm = m
  let ny = y
  if (today >= new Date(y, m, occThisMonth)) {
    nm = m + 1
    ny = nm > 11 ? y + 1 : y
    nm = (nm + 12) % 12
  }
  const nextOcc = Math.min(d, daysInMonth(ny, nm))
  const nextDate = new Date(ny, nm, nextOcc)
  const period = Math.max(1, (nextDate - lastDate) / MS_DAY)
  const elapsed = (today - lastDate) / MS_DAY
  const progress = Math.min(1, Math.max(0, elapsed / period))
  const daysLeft = Math.max(0, Math.ceil((nextDate - today) / MS_DAY))
  return { progress, daysLeft }
}
