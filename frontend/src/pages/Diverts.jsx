import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Diverts() {
  const [batches, setBatches] = useState([])
  const [scans, setScans] = useState([])
  const [entry, setEntry] = useState(null)
  const [scanId, setScanId] = useState('')
  const [err, setErr] = useState('')
  const load = async () => {
    setBatches(await api('/回流批次'))
    setEntry(await api('/回流入口'))
    const s = await api('/扫描')
    // 与后端“回流入口可见性”同口径：仅确已落入（已路由/已入袋）露出；
    // 闭口优先被拒、半截回滚的待路由件不在此列，避免“闭口后误开回流”。
    setScans(s.filter((x) => x.回流入口可见 ?? (x.status === '已路由' || x.status === '已入袋')))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const create = async () => {
    try {
      await api('/回流批次', { method: 'POST', body: JSON.stringify({ 扫描编号: Number(scanId) }) })
      load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className={surface_panel ? "panel" : "panel"}>
      <h2>错分回流</h2>
      {err && <div className="err">{err}</div>}
      <p className="muted" data-testid="divert-entry-visibility">
        回流入口当前可见可回流件：{entry ? entry.可回流扫描编号.length : scans.length} 件
        （仅已落入件；口闭被拒/待路由件不露出）
      </p>
      <div className="row">
        <label>扫描
          <select value={scanId} onChange={(e) => setScanId(e.target.value)}>
            <option value="">选择</option>
            {scans.map((s) => <option key={s.id} value={s.id}>{s.id} · {s.barcode} · {s.status}</option>)}
          </select>
        </label>
        <button type="button" onClick={create}>建回流批次</button>
      </div>
      <table>
        <thead><tr><th>编号</th><th>批次码</th><th>状态</th><th>时间</th></tr></thead>
        <tbody>
          {batches.map((b) => (
            <tr key={b.id}><td>{b.id}</td><td>{b.code}</td><td>{b.status}</td><td>{b.created_at}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
