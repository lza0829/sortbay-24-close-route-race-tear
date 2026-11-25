import React, { useEffect, useState } from 'react'
import { api } from '../api'

function pressureClass(level) {
  if (level === '超限') return 'tag danger'
  if (level === '偏高') return 'tag warn'
  return 'tag ok'
}

export default function Chutes() {
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')
  const [receipt, setReceipt] = useState(null)
  const load = () => api('/口门').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const toggle = async (id, 状态) => {
    setErr(''); setReceipt(null)
    try {
      const r = await api(`/口门/${id}/开闭?状态=${encodeURIComponent(状态)}`, { method: 'POST' })
      if (状态 === '口门闭') {
        setReceipt({
          code: r.code,
          在口件数: r.在口件数,
          带上件编号: r.带上件编号 || [],
          在途将拒: r.在途将拒 || [],
          重复收口: r.重复收口,
        })
      }
      await load()
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className="panel">
      <h2>口门 · 闭口优先争用收口</h2>
      <p className="muted">
        口径：闭口一旦提交成功，并发在途裁决整笔失败——不改扫描口门列、不增在口件数；
        裁决先落锤的件由闭口回执显式带上。
      </p>
      {err && <div className="err">{err}</div>}
      {receipt && (
        <div className="msg receipt">
          <b>闭口回执 {receipt.code}</b>
          {receipt.重复收口 ? '（重复收口，幂等）' : ''}
          <div>在口件数：{receipt.在口件数}；带上件：{receipt.带上件编号.join('、') || '无'}</div>
          <div>在途将拒裁决：{receipt.在途将拒.join('、') || '无'}</div>
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>口门码</th><th>标题</th><th>状态</th><th>在口件数</th><th>在途裁决</th>
            <th>压力</th><th>软容量</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id} className={c.status === '口门闭' ? 'row-closed' : ''}>
              <td>{c.code}</td>
              <td>{c.title}</td>
              <td><span className={c.status === '口门闭' ? 'tag danger' : 'tag ok'}>{c.status}</span></td>
              <td data-testid="at-chute-count"><b>{c.在口件数 ?? 0}</b></td>
              <td>{c.在途路由数 ?? 0}</td>
              <td><span className={pressureClass(c.压力等级)}>{c.压力等级} {Math.round((c.压力值 || 0) * 100)}%</span></td>
              <td>{c.soft_cap}</td>
              <td>
                <button type="button" onClick={() => toggle(c.id, '口门开')}>开</button>{' '}
                <button type="button" className="secondary" onClick={() => toggle(c.id, '口门闭')}>闭</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
