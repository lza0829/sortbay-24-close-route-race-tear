import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Overview() {
  const [data, setData] = useState(null)
  const [report, setReport] = useState(null)
  const [err, setErr] = useState('')
  const load = async () => {
    const [d, rep] = await Promise.all([api('/总览'), api('/总览/对账')])
    setData(d); setReport(rep)
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>
  const d = data.分布 || {}
  return (
    <div className="panel">
      <h2>总览 · 同刷对账</h2>
      <p className="muted">
        当前班次：{data.班次?.business_day} {data.班次?.name} · {data.班次?.sealed ? '已封班' : '未封班'}
        {' '}· 架构口径：{data.架构口径 || '闭口优先'}
      </p>
      {report && (
        <div className={report.一致 ? 'msg receipt' : 'err receipt'} data-testid="reconcile-badge">
          争用对账：{report.一致
            ? '一致（无口闭仍增在口 / 无裁决成功却无落入 / 无在途无着落）'
            : `发现 ${report.异常数} 处异常`}
        </div>
      )}
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
        <div className="stat">
          <div className="n">{data.在途路由数 ?? 0}</div>
          <div className="l">在途裁决（臂内）</div>
        </div>
      </div>
      <table>
        <thead>
          <tr><th>口门</th><th>标题</th><th>状态</th><th>在口件数</th><th>在途</th><th>压力</th></tr>
        </thead>
        <tbody>
          {(data.口门 || []).map((c) => (
            <tr key={c.id} className={c.status === '口门闭' ? 'row-closed' : ''}>
              <td>{c.code}</td>
              <td>{c.title}</td>
              <td>{c.status}</td>
              <td data-testid="overview-count"><b>{c.在口件数}</b></td>
              <td>{c.在途路由数 ?? 0}</td>
              <td>{c.压力等级} {c.建议封袋 ? '· 建议封袋' : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button type="button" className="secondary" onClick={() => load().catch(() => {})}>刷新（总览/对账同刷）</button></div>
    </div>
  )
}
