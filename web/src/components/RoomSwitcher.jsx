import { useState } from "react";
import { ArrowsLeftRight, CaretUpDown, Plus } from "@phosphor-icons/react";

// M3 opt-in. The single-room Dashboard does not render a room control at all.
// All operations reuse the administrator's existing same-origin authentication.
export function RoomSwitcher({ rooms, currentRoomId, onSelect, onCreate, onAction }) {
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [roomIdInput, setRoomIdInput] = useState("");
  const [source, setSource] = useState("first");
  const [reuseKeys, setReuseKeys] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const current = rooms.find((room) => room.room_id === currentRoomId);
  const invoke = async (action) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try { await action(); }
    catch (err) { setError(err.message || "操作没有完成"); }
    finally { setBusy(false); }
  };
  const close = () => { setOpen(false); setKey(""); setError(""); setAdding(false); };

  return (
    <div className="sidebar__room-control">
      {open && (
        <div className="sidebar__room-menu" role="region" aria-label="AI 房间">
          <div className="sidebar__room-menu-head">
            <strong>AI 房间</strong>
            <button type="button" className="sidebar__room-dismiss" aria-label="关闭房间列表" onClick={close}>×</button>
          </div>
          <div className="sidebar__room-list">
            {rooms.map((room) => (
              <div className="sidebar__room-choice" key={room.room_id}>
                <button type="button"
                  className={room.room_id === currentRoomId ? "is-current" : ""}
                  disabled={busy || room.state !== "ready" || room.room_id === currentRoomId}
                  title={room.state === "ready" ? room.name : "请先启动此房间"}
                  onClick={() => invoke(() => onSelect(room.room_id))}>
                  {room.room_id === currentRoomId ? "✓ " : ""}{room.display_name || room.room_slug || room.name}
                </button>
                {room.state === "stopped" && (
                  <button type="button" className="sidebar__room-small" disabled={busy}
                    onClick={() => invoke(() => onAction(room.room_id, "start"))}>启动</button>
                )}
              </div>
            ))}
            {!rooms.length && <small>暂无房间</small>}
          </div>
          {!adding ? (
            <button type="button" className="sidebar__room-add" onClick={() => setAdding(true)}>
              <Plus size={14} /> 新建房间
            </button>
          ) : (
            <form className="sidebar__room-form" onSubmit={(event) => {
              event.preventDefault();
              invoke(async () => {
                const result = await onCreate({
                  room_slug: roomIdInput.trim(), source,
                  reuse_provider_keys: reuseKeys,
                });
                setKey(result.gateway_key || "");
                setRoomIdInput("");
                setSource("first");
                setReuseKeys(false);
                setAdding(false);
              });
            }}>
              <input required minLength={3} maxLength={48}
                pattern="[a-z][a-z0-9]*(-[a-z0-9]+)*"
                value={roomIdInput} onChange={(event) => setRoomIdInput(event.target.value)}
                placeholder="房间 ID：xiaogi-main" aria-label="房间 ID" />
              <select value={source} aria-label="新房间配置来源" onChange={(event) => setSource(event.target.value)}>
                <option value="first">首房功能配置</option>
                <option value="blank">完全空白</option>
                {rooms.map((room) => <option value={room.room_id} key={room.room_id}>{room.name}</option>)}
              </select>
              <label className="sidebar__room-reuse">
                <input type="checkbox" checked={reuseKeys}
                  onChange={(event) => setReuseKeys(event.target.checked)}/> 复用模型密钥
              </label>
              <button type="submit" disabled={busy || !roomIdInput.trim()}>创建</button>
            </form>
          )}
          {key && (
            <div className="sidebar__room-secret" role="status">
              <small>新房间 Gateway Key，请立即私密保存：</small>
              <code>{key}</code>
              <button type="button" onClick={() => setKey("")}>已保存，关闭</button>
            </div>
          )}
          {error && <p className="sidebar__room-error" role="alert">{error}</p>}
          <small className="sidebar__room-hint">切换后自动刷新，无需重复登录。私人记忆不会复制。</small>
        </div>
      )}
      <button className="nav-item sidebar__room-trigger" type="button"
        aria-label="切换 AI 房间" aria-expanded={open}
        title={"当前房间：" + (current?.display_name || current?.room_slug || current?.name || "未选择")}
        onClick={() => open ? close() : setOpen(true)}>
        <ArrowsLeftRight size={20} weight="light" aria-hidden="true"/>
        <span>{current?.display_name || current?.room_slug || current?.name || "切换房间"} <CaretUpDown size={13} aria-hidden="true"/></span>
      </button>
    </div>
  );
}
