import { useEffect, useState } from "react";
import { api } from "../api";

const kindLabel = {
  room: "算力槽",
  teacher: "执行器",
  type: "类型门禁",
};

const diffLabel = {
  reject_receipt_with_open_lease: "拒绝回执与占用并存",
  executor_out_of_capability: "落点执行器越出能力集合",
  window_state_torn: "批窗态与租约态撕裂",
  live_without_lease: "占用态批窗无租约",
  lease_without_window: "租约无对应批窗",
  watermark_diverged: "水位与实槽不一致",
};

export default function Conflicts() {
  const [rows, setRows] = useState([]);
  const [diffs, setDiffs] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .conflicts()
      .then(setRows)
      .catch((e) => setErr(e.message));
    api
      .differences()
      .then((d) => setDiffs(d.differences || []))
      .catch((e) => setErr(e.message));
  }, []);

  if (err) return <div className="msg err">{err}</div>;

  return (
    <>
      <section className="panel">
        <h2>差异桶（类型回执 vs 已落库占用）</h2>
        {diffs.length === 0 ? (
          <div className="msg">无差异 · 类型回执与占用索引一致</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>差异</th>
                <th>批窗</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {diffs.map((d, i) => (
                <tr key={i}>
                  <td>
                    <span className="tag bad">
                      {diffLabel[d.kind] || d.kind}
                    </span>
                  </td>
                  <td>{d.window_code}</td>
                  <td>{d.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="panel">
        <h2>冲突桶</h2>
        <table>
          <thead>
            <tr>
              <th>类型</th>
              <th>批窗</th>
              <th>资源</th>
              <th>原因</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={5}>暂无冲突</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{kindLabel[r.kind] || r.kind}</td>
                <td>{r.window_code}</td>
                <td>{r.resource_code}</td>
                <td>{r.reason}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
