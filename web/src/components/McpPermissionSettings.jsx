import { useEffect, useState } from "react";

/**
 * Opt-in multi-room editor in the ORIGINAL Settings > Features screen.
 * Native single-room installations do not show this new control.
 * Uses the same dashboard login; never handles administrator or gateway keys.
 */
export function McpPermissionSettings() {
  const [roomId, setRoomId] = useState(null);
  const [mode, setMode] = useState("original");
  const [available, setAvailable] = useState([]);
  const [selected, setSelected] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  async function loadFor(id) {
    const response = await fetch("/__serein/rooms/" + id + "/mcp-tools", {
      credentials: "same-origin", cache: "no-store",
    });
    if (!response.ok) throw Error("MCP 工具权限暂时不可读取");
    const data = await response.json();
    if (!["original", "custom"].includes(data.mode) || !Array.isArray(data.available)) {
      throw Error("MCP 权限返回格式不正确");
    }
    setMode(data.mode);
    setAvailable(data.available);
    setSelected(data.mode === "original" ? data.available : data.tools || []);
    setLoaded(true);
  }
  useEffect(() => {
    let active = true;
    (async () => {
      const response = await fetch("/__serein/rooms/current", {
        credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) {
        return; // Original single-room Dashboard; no changes.
      }
      const data = await response.json();
      if (!active || !/^[a-f0-9]{32}$/.test(data.room_id || "")) return;
      setRoomId(data.room_id);
      try { await loadFor(data.room_id); }
      catch (error) { if (active) setStatus(error.message); }
    })().catch(() => {});
    return () => { active = false; };
  }, []);

  if (!roomId) return null;

  async function save() {
    if (busy) return;
    if (!window.confirm("保存这间房的 MCP 工具权限？若房间正在运行，将只重启这一间房使设置生效；所有记忆数据保留。")) return;
    setBusy(true); setStatus("正在更新此房间 MCP 设置……");
    try {
      const response = await fetch("/__serein/rooms/" + roomId + "/mcp-tools", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, tools: mode === "original" ? [] : selected }),
      });
      if (!response.ok) throw Error(response.status === 409
        ? "工具设置无效或房间重启失败，请查看房间运行状态"
        : "更新 MCP 工具权限失败");
      const result = await response.json();
      await loadFor(roomId);
      setStatus(result.restarted
        ? "已保存，并仅重启当前房间；其他 AI 不受影响。"
        : "已保存。启动本房间后生效。");
    } catch (error) { setStatus(error.message); }
    finally { setBusy(false); }
  }

  return (
    <details className="settings-menu">
      <summary>
        <span>MCP 工具权限<small>只控制当前 AI 能看见、调用的工具，不会删除记忆或停用原版后台功能</small></span>
      </summary>
      {!loaded ? <p role="status">{status || "正在读取当前房间的 MCP 工具……"}</p> : (
        <div className="settings-group" style={{padding:"12px 0"}}>
          <label className="settings-toggle">
            <span><strong>沿用原版 MCP 工具行为（推荐）</strong>
              <small>根据原版「功能」开关自动展示相应工具，不另加白名单限制。</small>
            </span>
            <input type="checkbox" checked={mode === "original"} disabled={busy}
              onChange={(event) => setMode(event.target.checked ? "original" : "custom")}/>
          </label>
          {mode === "custom" && (
            <>
              <p>勾选允许当前 AI 使用的工具。即使允许某工具，原版功能开关关闭时该工具仍不可用。</p>
              {Array.from(new Set([...available, ...selected])).sort().map(name => (
                <label key={name} className="settings-toggle">
                  <span><strong>{name}</strong>
                    {!available.includes(name) && <small>当前功能未开启，暂不出现在 MCP 工具列表</small>}
                  </span>
                  <input type="checkbox" disabled={busy}
                    checked={selected.includes(name)}
                    onChange={(event) => setSelected(old => event.target.checked
                      ? [...old, name] : old.filter(item => item !== name))}/>
                </label>
              ))}
            </>
          )}
          <div className="settings-actions">
            <button type="button" disabled={busy} onClick={save}>保存本房间 MCP 权限</button>
          </div>
          <p role="status">{status}</p>
        </div>
      )}
    </details>
  );
}
