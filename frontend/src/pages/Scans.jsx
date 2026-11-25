import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Scans() {
  const [rows, setRows] = useState([])
  const [barcode, setBarcode] = useState('')
  const [dest, setDest] = useState('110')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const load = () => api('/扫描册').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const create = async () => {
    setErr(''); setMsg('')
    try {
      await api('/扫描', { method: 'POST', body: JSON.stringify({ 条码: barcode, 目的地码: dest }) })
      setMsg('过机装载成功'); setBarcode(''); load()
    } catch (e) { setErr(e.message) }
  }

  const routeOne = async (id) => {
    setErr(''); setMsg('')
    try {
      const r = await api(`/扫描/${id}/路由`, { method: 'POST' })
      setMsg(`裁决成功：${r.status} · 落入口门 ${r.口门码 || r.chute_id} · 对撞号 ${r.对撞号 || ''}`)
    } catch (e) {
      // 闭口优先拒绝：回执报错，扫描册仍是待路由、口门列为空
      setErr(`裁决被拒（整笔失败，未改扫描口门列）：${e.message}`)
    } finally {
      load()
    }
  }

  return (
    <div className="panel">
      <h2>过机扫描 · 扫描册</h2>
      <div className="row">
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} /></label>
        <label>目的地码<input value={dest} onChange={(e) => setDest(e.target.value)} /></label>
        <button type="button" onClick={create}>装载扫描</button>
        <button type="button" className="secondary" onClick={load}>刷新扫描册</button>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <table>
        <thead>
          <tr>
            <th>编号</th><th>条码</th><th>目的地</th><th>状态</th><th>扫描口门列</th>
            <th>回流入口</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className={r.status === '待路由' ? 'row-pending' : ''}>
              <td>{r.id}</td>
              <td>{r.barcode}</td>
              <td>{r.dest_code}</td>
              <td>{r.status}</td>
              <td data-testid="scan-chute-col">{r.口门码 || r.chute_id || '-'}</td>
              <td>
                {r.回流入口可见
                  ? <span className="tag ok">可见</span>
                  : <span className="tag muted-tag">不可见</span>}
              </td>
              <td>
                {(r.status === '待路由' || r.status === '错分回流') && (
                  <button type="button" onClick={() => routeOne(r.id)}>路由裁决</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
