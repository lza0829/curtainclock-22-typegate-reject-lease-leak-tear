import { useEffect, useState } from "react";
import { api } from "../api";

export default function Resources() {
  const [slots, setSlots] = useState([]);
  const [executors, setExecutors] = useState([]);
  const [matrix, setMatrix] = useState([]);
  const [err, setErr] = useState("");

  async function load() {
    try {
      const [s, e, m] = await Promise.all([
        api.slots(),
        api.executors(),
        api.capabilityMatrix(),
      ]);
      setSlots(s);
      setExecutors(e);
      setMatrix(m);
    } catch (e) {
      setErr(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  if (err) return <div className="msg err">{err}</div>;

  return (
    <>
      <section className="panel">
        <h2>能力矩阵（作业类型 × 执行器）</h2>
        <table>
          <thead>
            <tr>
              <th>作业类型</th>
              <th>能力集合（启用中执行器）</th>
              <th>占用中</th>
              <th>空闲可落</th>
            </tr>
          </thead>
          <tbody>
            {matrix.map((m) => {
              const capable = m.executors.filter((e) => e.active);
              const busy = m.executors.filter((e) => e.occupied);
              const free = capable.filter((e) => !e.occupied);
              return (
                <tr key={m.subject}>
                  <td>{m.subject}</td>
                  <td>{capable.map((e) => e.code).join("、") || "—"}</td>
                  <td>
                    {busy.length ? (
                      <span className="tag live">
                        {busy
                          .map(
                            (e) =>
                              `${e.code}(${e.open_leases
                                .map((l) => l.lease_no)
                                .join("、")})`
                          )
                          .join("、")}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{free.map((e) => e.code).join("、") || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h2>算力槽</h2>
        <table>
          <thead>
            <tr>
              <th>编码</th>
              <th>名称</th>
              <th>类型</th>
              <th>优先级</th>
              <th>容量</th>
              <th>状态</th>
              <th>占用</th>
            </tr>
          </thead>
          <tbody>
            {slots.map((r) => (
              <tr key={r.code}>
                <td>{r.code}</td>
                <td>{r.name}</td>
                <td>{r.is_standby ? "候补槽" : "主槽"}</td>
                <td>{r.is_standby ? r.standby_priority : "—"}</td>
                <td>{r.capacity}</td>
                <td>{r.active ? "启用" : "停用"}</td>
                <td>
                  {r.occupied ? (
                    <span className="tag live">
                      {r.open_leases.map((l) => l.lease_no).join("、")}
                    </span>
                  ) : (
                    <span className="tag done">空闲</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h2>执行器（占用索引）</h2>
        <table>
          <thead>
            <tr>
              <th>编码</th>
              <th>名称</th>
              <th>作业类型</th>
              <th>状态</th>
              <th>占用</th>
            </tr>
          </thead>
          <tbody>
            {executors.map((t) => (
              <tr key={t.code}>
                <td>{t.code}</td>
                <td>{t.name}</td>
                <td>{t.subject}</td>
                <td>{t.active ? "启用" : "停用"}</td>
                <td>
                  {t.occupied ? (
                    <span className="tag live">
                      {t.open_leases.map((l) => l.lease_no).join("、")}
                    </span>
                  ) : (
                    <span className="tag done">空闲</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
