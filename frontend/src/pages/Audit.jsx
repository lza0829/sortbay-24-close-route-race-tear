import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Audit() {
  const [audits, setAudits] = useState([])
  const [runs, setRuns] = useState([])
  const [report, setReport] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [replays, setReplays] = useState({})

  const load = async () => {
    setAudits(await api('/审计'))
    setRuns(await api('/对撞?limit=50'))
    setReport(await api('/总览/对账'))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])

  const replay = async (id) => {
    setErr(''); setMsg('')
    try {
      const r = await api(`/对撞/${id}/重放`, { method: 'POST' })
      setReplays((m) => ({ ...m, [id]: r.重放一致 ? '一致' : '不一致' }))
      setMsg(`对撞 ${r.对撞号} 重放：${r.重放一致 ? '一致' : '不一致'}`)
      await load()
    } catch (e) { setErr(e.message) }
  }

  const restore = async (id) => {
    setErr(''); setMsg('')
    try {
      const r = await api(`/对撞/${id}/退回快照`, { method: 'POST' })
      setMsg(`已退回对撞前快照：${r.对撞号}`)
      await load()
    } catch (e) { setErr(e.message) }
  }

  const repair = async () => {
    setErr(''); setMsg('')
    try {
      const r = await api('/总览/对账/修复', { method: 'POST' })
      setMsg(`对账修复完成：回收在途 ${r.回收在途.length} 笔，退回快照 ${r.退回快照对撞.length} 组`)
      await load()
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className="panel">
      <h2>审计 · 对撞收口重放</h2>
      {err && <div className="err">{err}</div>}
      {msg && <div className="msg">{msg}</div>}

      <div className="row">
        <span className={report?.一致 ? 'tag ok' : 'tag danger'} data-testid="audit-reconcile">
          {report ? (report.一致 ? '全局对账一致' : `异常 ${report.异常数}`) : '对账加载中'}
        </span>
        <button type="button" className="secondary" onClick={repair}>对账修复（回收在途/退回快照）</button>
        <button type="button" className="secondary" onClick={() => load().catch((e) => setErr(e.message))}>刷新</button>
      </div>
      {report && !report.一致 && (
        <table>
          <thead><tr><th>异常类</th><th>明细</th></tr></thead>
          <tbody>
            {report.异常.map((a, i) => (
              <tr key={i}><td>{a.类}</td><td><code>{JSON.stringify(a)}</code></td></tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>对撞账本（同一争用结局可重放）</h3>
      <table>
        <thead><tr><th>编号</th><th>对撞号</th><th>结局</th><th>口门</th><th>扫描</th><th>重放</th><th>操作</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td><code>{r.code}</code></td>
              <td>{r.outcome}</td>
              <td>{r.chute_id ?? '-'}</td>
              <td>{r.scan_id ?? '-'}</td>
              <td>
                {r.replay_count > 0 ? `已重放${r.replay_count}次` : '未重放'}
                {replays[r.id] ? ` · 本次${replays[r.id]}` : ''}
              </td>
              <td>
                <button type="button" onClick={() => replay(r.id)}>重放核对</button>{' '}
                <button type="button" className="secondary" onClick={() => restore(r.id)}>退回快照</button>
              </td>
            </tr>
          ))}
          {runs.length === 0 && <tr><td colSpan={7} className="muted">暂无对撞记录</td></tr>}
        </tbody>
      </table>

      <h3>分拣线审计流</h3>
      <table>
        <thead><tr><th>编号</th><th>类别</th><th>对象</th><th>动作</th><th>时间</th><th>备注</th></tr></thead>
        <tbody>
          {audits.map((a) => (
            <tr key={a.id}>
              <td>{a.id}</td><td>{a.kind}</td><td>{a.ref_id ?? '-'}</td>
              <td>{a.action}</td><td>{a.created_at}</td><td>{a.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
