/* The admin key lives in this page's memory only: no cookies or localStorage.
 * All user-supplied room names are rendered as textContent, never HTML. */
(() => {
  "use strict";
  let administratorKey = "";
  let rooms = [];
  let busy = false;
  const $ = (id) => document.getElementById(id);

  function text(id, value) { $(id).textContent = String(value || ""); }
  function message(id, value) { text(id, value); }
  function node(tag, className, textValue) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (textValue !== undefined) element.textContent = textValue;
    return element;
  }
  async function api(path, method = "GET", data = undefined) {
    if (!administratorKey) throw Error("请先登录");
    const headers = { "Authorization": "Bearer " + administratorKey };
    if (data !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: data === undefined ? undefined : JSON.stringify(data),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    });
    let body;
    try { body = await response.json(); }
    catch { body = {}; }
    if (!response.ok) {
      if (response.status === 401) throw Error("管理员密钥无效，请重新验证");
      if (response.status === 403) throw Error("请求来源被拒绝，请使用同源的安全管理入口");
      if (response.status === 409) throw Error("房间状态不允许该操作，或 Docker 启动失败");
      throw Error(body.detail || "请求失败：" + response.status);
    }
    return body;
  }

  function updateSourceChoices() {
    const source = $("room-source");
    const current = source.value;
    source.replaceChildren(
      new Option("复制第一个房间的功能配置（默认）", "first"),
      new Option("全新空白房间", "blank")
    );
    for (const room of rooms) {
      source.add(new Option("复制：" + room.name + " · " + room.room_id.slice(0, 8),
                            room.room_id));
    }
    source.value = Array.from(source.options).some(option => option.value === current) ? current : "first";
  }

  function renderRooms() {
    const list = $("room-list");
    list.replaceChildren();
    text("room-count", rooms.length + " 间");
    if (!rooms.length) {
      list.append(node("p", "muted", "还没有房间。先创建一个空白房间吧。"));
      return;
    }
    for (const room of rooms) {
      const article = node("article", "room");
      const top = node("div", "room-top");
      const title = node("h3", "", room.name);
      const badge = node("span", "pill " + (room.state === "ready" ? "ready" : room.state === "error" ? "error" : ""),
                         room.state);
      top.append(title, badge);
      article.append(top);
      article.append(node("code", "muted", room.room_slug || room.room_id));
      const docker = room.docker || {};
      article.append(node("p", "muted", "容器：" + (docker.container || "unknown") +
        " · 健康：" + (docker.health || "unknown")));
      if (room.last_error) article.append(node("p", "muted", "最近问题：" + room.last_error));
      const actions = node("div", "buttons");
      const showStart = room.state === "stopped" || room.state === "ready";
      const showStop = room.state === "ready" || room.state === "error" || room.state === "stopped";
      if (showStart) {
        const button = node("button", "", "启动");
        button.type = "button";
        button.disabled = busy || room.state === "ready";
        button.addEventListener("click", () => roomAction(room.room_id, "start"));
        actions.append(button);
      }
      if (showStop) {
        const button = node("button", "", "停止");
        button.type = "button";
        button.disabled = busy || room.state === "stopped";
        button.addEventListener("click", () => roomAction(room.room_id, "stop"));
        actions.append(button);
      }
      article.append(actions);
      list.append(article);
    }
  }

  async function refresh() {
    const result = await api("/api/rooms");
    rooms = result.rooms || [];
    updateSourceChoices();
    renderRooms();
  }
  async function roomAction(roomId, action) {
    if (busy) return;
    busy = true;
    renderRooms();
    message("rooms-message", action === "start" ? "正在启动并进行健康检查……" : "正在停止房间……");
    try {
      await api("/api/rooms/" + encodeURIComponent(roomId) + "/" + action, "POST");
      await refresh();
      message("rooms-message", action === "start" ? "房间已启动。" : "房间已停止。");
    } catch (error) {
      message("rooms-message", error.message);
    } finally {
      busy = false;
      renderRooms();
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("admin-key");
    administratorKey = input.value.trim();
    input.value = "";
    message("login-message", "");
    try {
      await refresh();
      $("login-panel").hidden = true;
      $("workspace").hidden = false;
    } catch (error) {
      administratorKey = "";
      message("login-message", error.message);
    }
  });

  $("logout-button").addEventListener("click", () => {
    administratorKey = "";
    rooms = [];
    text("new-room-key", "");
    $("secret-panel").hidden = true;
    $("workspace").hidden = true;
    $("login-panel").hidden = false;
    message("login-message", "已退出。");
  });

  $("refresh-button").addEventListener("click", async () => {
    message("rooms-message", "");
    try { await refresh(); }
    catch (error) { message("rooms-message", error.message); }
  });

  $("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    $("create-button").disabled = true;
    message("create-message", "正在初始化独立房间……");
    try {
      const result = await api("/api/rooms", "POST", {
        name: $("room-name").value.trim(),
        room_slug: $("room-id").value.trim(),
        source: $("room-source").value,
        reuse_provider_keys: $("reuse-provider").checked,
      });
      text("new-room-key", result.gateway_key);
      $("secret-panel").hidden = false;
      $("room-name").value = "";
      $("room-id").value = "";
      $("reuse-provider").checked = false;
      await refresh();
      message("create-message", "房间已创建（停止状态），请保存 Gateway Key 后启动。");
      $("secret-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      message("create-message", error.message);
    } finally {
      busy = false;
      $("create-button").disabled = false;
    }
  });

  $("dismiss-key-button").addEventListener("click", () => {
    text("new-room-key", "");
    $("secret-panel").hidden = true;
  });
  $("copy-key-button").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("new-room-key").textContent);
      message("create-message", "Key 已复制，请保存到私密位置。");
    } catch {
      message("create-message", "无法自动复制，请手动选择并复制上方 Key。");
    }
  });
})();
