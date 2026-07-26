/**
 * BTC Simulator console.
 *
 * Two rules run through this file:
 *
 * 1. Nothing that came from the network is ever concatenated into HTML. Peer
 *    names, mismatch reasons and security-event text arrive over P2P from
 *    other people's nodes, so building rows with innerHTML let any classmate
 *    run script in everybody else's console -- starting with the very page
 *    meant to report attacks. Every row here is built with createElement and
 *    textContent.
 * 2. A frame from the server can be malformed. Rendering is defensive
 *    throughout: one bad target string used to throw inside renderStatus and
 *    leave the whole console half-drawn.
 */

/* eslint-env browser */
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  // ---------------------------------------------------------------- state

  const state = {
    status: null,
    classroom: null,
    session: { is_admin: false, enforced: true },
    blocksOffset: 0,
    blocksLimit: 30,
    historyOffset: 0,
    historyLimit: 25,
    historyAddress: null,
    history: null,
    peers: [],
    securityEvents: [],
    logFilter: "",
    securityFilter: "",
    currentBlock: null,
    socket: null,
    reconnectDelay: 1000,
    classroomTimer: null,
    lastOrphanCount: 0,
  };

  const BLOCKS_LIMIT = 30;

  // Display labels for the machine-readable states the server now sends.
  // Keeping them here is what lets the wire protocol stay language-neutral.
  const PEER_STATUS_LABELS = {
    connected: "已连接",
    offline: "离线",
    known: "已知",
    param_mismatch: "参数不匹配",
    self_connection: "自连接",
    duplicate_address: "重复地址",
    banned: "已封禁",
    self: "本机",
  };

  const MINER_STATUS_LABELS = {
    idle: "未挖矿",
    mining: "正在挖矿",
    paused: "暂停",
  };

  const DIRECTION_LABELS = {
    in: "收入",
    out: "支出",
    mined: "挖矿",
    other: "其他",
  };

  const peerStatusLabel = (value) => PEER_STATUS_LABELS[value] || value || "--";
  const minerStatusLabel = (value) => MINER_STATUS_LABELS[value] || value || "--";

  // ------------------------------------------------------------ formatting

  function formatBTC(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "-- BTC";
    return `${number.toFixed(8).replace(/\.?0+$/, "")} BTC`;
  }

  function shortHash(value, size = 10) {
    const text = String(value ?? "");
    if (!text) return "--";
    if (text.length <= size * 2 + 3) return text;
    return `${text.slice(0, size)}...${text.slice(-size)}`;
  }

  /**
   * Human-readable form of a 256-bit target.
   * Guards every step: this used to run BigInt() on unvalidated remote data
   * and throw, aborting the render of whatever table it appeared in.
   */
  function targetPreview(value, difficulty = null) {
    if (value === null || value === undefined || value === "") {
      return difficulty === null || difficulty === undefined
        ? "--"
        : `≤ target (${difficulty} bit zero)`;
    }
    const target = String(value).toLowerCase().replace(/^0x/, "");
    if (!/^[0-9a-f]{1,64}$/.test(target)) return "--";
    let leadingBits;
    try {
      const big = BigInt(`0x${target}`);
      leadingBits = big === 0n ? 256 : Math.max(256 - big.toString(2).length, 0);
    } catch (_error) {
      return "--";
    }
    const visible = Math.min(Math.max(8, Math.ceil(leadingBits / 4) + 3), target.length);
    return `≤ ${target.slice(0, visible)}${visible < target.length ? "..." : ""} (${leadingBits} bit zero)`;
  }

  function formatHostPort(host, port) {
    const value = String(host ?? "").replace(/^\[|\]$/g, "");
    if (!value) return "";
    if (!port) return value;
    return value.includes(":") ? `[${value}]:${port}` : `${value}:${port}`;
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes)) return "--";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  }

  function formatTime(epoch) {
    const seconds = Number(epoch);
    if (!Number.isFinite(seconds) || seconds <= 0) return "--";
    return new Date(seconds * 1000).toLocaleString();
  }

  function formatDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return "--";
    if (value < 60) return `${value} 秒`;
    if (value % 60 === 0) return `${value / 60} 分钟`;
    return `${value} 秒`;
  }

  function formatCount(value, unit = "") {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    if (number >= 1e12) return `${(number / 1e12).toFixed(2)} T${unit}`;
    if (number >= 1e9) return `${(number / 1e9).toFixed(2)} G${unit}`;
    if (number >= 1e6) return `${(number / 1e6).toFixed(2)} M${unit}`;
    if (number >= 1e3) return `${(number / 1e3).toFixed(2)} k${unit}`;
    return `${number}${unit}`;
  }

  // ------------------------------------------------------------ DOM helpers

  /** Build an element. Text is always assigned via textContent, never parsed. */
  function el(tag, options = {}, children = []) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = String(options.text ?? "");
    if (options.title !== undefined) node.title = String(options.title ?? "");
    if (options.type) node.type = options.type;
    if (options.dataset) {
      for (const [key, value] of Object.entries(options.dataset)) {
        node.dataset[key] = String(value ?? "");
      }
    }
    if (options.attrs) {
      for (const [key, value] of Object.entries(options.attrs)) {
        node.setAttribute(key, String(value ?? ""));
      }
    }
    if (options.onClick) node.addEventListener("click", options.onClick);
    for (const child of children) {
      if (child) node.appendChild(child);
    }
    return node;
  }

  /** A monospace cell that shows a shortened hash but copies the whole thing. */
  function hashCell(value, size = 8) {
    const text = String(value ?? "");
    const cell = el("td", { className: "mono" });
    if (!text) {
      cell.textContent = "--";
      return cell;
    }
    const button = el("button", {
      className: "link-btn",
      text: shortHash(text, size),
      title: `${text}（点击复制）`,
      type: "button",
      onClick: () => copyText(text, "内容"),
    });
    cell.appendChild(button);
    return cell;
  }

  function textCell(value, className = "") {
    return el("td", { className, text: value === null || value === undefined || value === "" ? "--" : String(value) });
  }

  function emptyRow(body, columns, message) {
    const row = el("tr");
    const cell = el("td", { text: message });
    cell.colSpan = columns;
    row.appendChild(cell);
    body.appendChild(row);
  }

  function replaceChildren(node, children) {
    node.replaceChildren(...children.filter(Boolean));
  }

  function setText(selector, value) {
    const node = $(selector);
    if (node) node.textContent = value === null || value === undefined ? "--" : String(value);
  }

  /** Attach the full value to a copyable readout without ever parsing HTML. */
  function setCopyable(selector, value, display) {
    const node = $(selector);
    if (!node) return;
    const text = String(value ?? "");
    node.textContent = display ?? (text || "--");
    node.dataset.copy = text;
    node.title = text ? `${text}（点击复制）` : "";
  }

  // ------------------------------------------------------------ notifications

  let toastTimer = null;
  let errorTimer = null;

  function showToast(message) {
    const toast = $("#toast");
    if (!toast) return;
    toast.textContent = String(message ?? "");
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 2800);
  }

  /**
   * Errors get their own assertive region and a longer dwell.
   * Previously every message shared one polite toast, so a burst of failures
   * showed only the last one and screen readers might announce none of them.
   */
  function showError(message) {
    const toast = $("#errorToast");
    if (!toast) {
      showToast(message);
      return;
    }
    toast.textContent = String(message ?? "");
    toast.classList.add("visible");
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => toast.classList.remove("visible"), 6000);
  }

  function report(error) {
    const message = error && error.message ? error.message : String(error);
    showError(message);
    return null;
  }

  async function copyText(value, label = "内容") {
    const text = String(value ?? "");
    try {
      await navigator.clipboard.writeText(text);
      showToast(`${label}已复制`);
      return;
    } catch (_error) {
      // Clipboard API needs a secure context; classroom nodes are plain http
      // on a LAN address, so fall back to a selection-based copy.
    }
    try {
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.setAttribute("readonly", "");
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      helper.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(helper);
      showToast(ok ? `${label}已复制` : "复制失败，请手动选中");
    } catch (_error) {
      showError("复制失败，请手动选中文本");
    }
  }

  // ------------------------------------------------------------------- api

  // A token in the URL lets a teacher drive a node from another machine.
  const params = new URLSearchParams(window.location.search);
  const adminToken = params.get("token") || "";
  const REQUEST_TIMEOUT_MS = 15000;

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const headers = { "Content-Type": "application/json" };
    if (adminToken) headers["X-Admin-Token"] = adminToken;
    try {
      const response = await fetch(path, { headers, signal: controller.signal, ...options });
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const body = await response.json();
          if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : detail;
        } catch (_error) {
          /* keep the status line */
        }
        if (response.status === 403) {
          detail = `${detail}`;
        }
        throw new Error(detail);
      }
      return await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时，节点可能没有响应");
      if (error instanceof TypeError) throw new Error("连不上节点，请检查它是否还在运行");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  const post = (path, payload) =>
    api(path, { method: "POST", body: JSON.stringify(payload ?? {}) });

  // ------------------------------------------------------------------ theme

  const THEME_KEY = "btcsim-theme";
  let currentTheme = "auto";

  function applyTheme(theme) {
    currentTheme = theme;
    document.documentElement.dataset.theme = theme === "auto" ? "" : theme;
    if (theme === "auto") delete document.documentElement.dataset.theme;
    const button = $("#themeBtn");
    if (button) {
      button.textContent = theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐";
      button.title = theme === "dark" ? "深色" : theme === "light" ? "浅色" : "跟随系统";
    }
    // Redraw canvases so the QR contrast follows the theme.
    renderJoinInfo(state.status);
    renderAddressQr(state.status);
    if (state.chartData) renderCharts(state.chartData);
  }

  function setupTheme() {
    let saved = "auto";
    try {
      saved = window.localStorage.getItem(THEME_KEY) || "auto";
    } catch (_error) {
      saved = "auto";
    }
    applyTheme(saved);
    const button = $("#themeBtn");
    if (!button) return;
    button.addEventListener("click", () => {
      const order = ["auto", "light", "dark"];
      const next = order[(order.indexOf(currentTheme) + 1) % order.length];
      applyTheme(next);
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch (_error) {
        /* private mode: the choice just does not persist */
      }
    });
  }

  function themeColors() {
    const styles = getComputedStyle(document.body);
    const pick = (name, fallback) => (styles.getPropertyValue(name) || "").trim() || fallback;
    return {
      ink: pick("--ink", "#101917"),
      muted: pick("--muted", "#6b7c78"),
      accent: pick("--accent", "#1f8f6f"),
      accentSoft: pick("--accent-soft", "#8fd3bd"),
      warn: pick("--warn", "#0b7f72"),
      grid: pick("--border", "#dfe7e4"),
      surface: pick("--surface", "#ffffff"),
    };
  }

  // ------------------------------------------------------------- rendering

  function renderStatus(status) {
    if (!status || typeof status !== "object") return;
    state.status = status;

    const mining = status.mining || {};
    const peers = status.peers || {};
    const mempool = status.mempool || {};
    const wallet = status.wallet || {};
    const policy = status.difficulty_policy || {};
    const supply = status.supply || {};

    const isMining = Boolean(mining.is_mining);
    const miningState = isMining ? "running" : mining.status === "paused" ? "paused" : "idle";

    setText("#nodeName", status.node_name || "节点控制台");
    setText("#balance", formatBTC(status.balance));
    setText("#availableBalance", `可用 ${formatBTC(status.available_balance)}`);
    setText("#height", status.height ?? "--");
    setText("#tipHash", shortHash(status.tip_hash, 8));
    setText("#peerCount", `${peers.inbound ?? 0} / ${peers.outbound ?? 0}`);
    setText("#peerDetail", `入站 ${peers.inbound ?? 0}，出站 ${peers.outbound ?? 0}`);
    setText("#miningStatus", minerStatusLabel(mining.status));
    setText("#mempoolCount", `${mempool.count ?? 0} 笔`);
    setText("#mempoolBytes", formatBytes(mempool.bytes));
    setText("#orphanCount", status.orphan_count ?? 0);
    setText("#chainWork", formatCount(Number(status.chain_work) || 0, "H"));

    setText("#subsidyValue", formatBTC(supply.next_block_subsidy));
    setText(
      "#halvingHint",
      supply.blocks_to_halving === undefined
        ? "--"
        : `还有 ${supply.blocks_to_halving} 块减半 · 已发行 ${formatBTC(supply.total_issued)}`,
    );

    const currentTarget = status.target || policy.current_target;
    const preview = targetPreview(currentTarget, status.difficulty);
    const targetTimeText = policy.auto ? ` · ${formatDuration(policy.target_block_seconds)}/块` : "";
    setText("#difficulty", `${preview}${targetTimeText}`);
    const difficultyNode = $("#difficulty");
    if (difficultyNode) difficultyNode.title = String(currentTarget || "");
    setText("#miningDifficulty", preview);
    const miningDifficultyNode = $("#miningDifficulty");
    if (miningDifficultyNode) miningDifficultyNode.title = String(currentTarget || "");

    const difficultyInput = $("#difficultyInput");
    if (difficultyInput && document.activeElement !== difficultyInput) {
      difficultyInput.value = status.difficulty ?? 0;
    }

    setText("#miningBadge", minerStatusLabel(mining.status));
    const badge = $("#miningBadge");
    if (badge) badge.className = `status-badge ${miningState}`;
    setText(
      "#miningHint",
      isMining
        ? `正在寻找满足 hash ${preview} 的区块；难度自动调整每次只改变 1 个二进制前导 0 位。`
        : mining.hash
          ? `挖矿已暂停，保留最近一次 nonce/hash；当前目标 ${preview}。`
          : `点击开始挖矿后，节点会寻找满足 hash ${preview} 的候选区块。`,
    );
    setText("#nonceLabel", isMining ? "当前 Nonce" : "最近 Nonce");
    setText("#hashLabel", isMining ? "候选 Hash" : "最近尝试 Hash");
    setText("#nonce", mining.nonce ?? "--");
    setCopyable("#currentHash", mining.hash || "");
    setCopyable("#tipHashFull", status.tip_hash || "");
    setCopyable("#walletAddressShort", wallet.address || "");
    setCopyable("#walletAddress", wallet.address || "");

    setText("#walletName", wallet.name || "default");
    setText("#receiveBalance", formatBTC(status.balance));
    setText("#receiveImmature", formatBTC(status.immature_balance));
    setText("#receivePeers", `${peers.total ?? 0} 个节点`);
    setText(
      "#sendHint",
      `可用 ${formatBTC(status.available_balance)}；其中未成熟的挖矿奖励 ${formatBTC(status.immature_balance)} 还不能花（需要 ${supply.coinbase_maturity ?? 0} 个确认）。`,
    );

    const startBtn = $("#startMiningBtn");
    const stopBtn = $("#stopMiningBtn");
    if (startBtn) startBtn.disabled = isMining;
    if (stopBtn) stopBtn.disabled = !isMining;

    renderJoinInfo(status);
    renderAddressQr(status);
    if (Number(status.orphan_count || 0) !== state.lastOrphanCount) {
      state.lastOrphanCount = Number(status.orphan_count || 0);
      refreshTips().catch(() => {});
    }
    renderLogs(status.logs || []);
    renderSecurityEvents((status.security && status.security.events) || []);
  }

  function renderJoinInfo(status) {
    if (!status) return;
    const network = status.network || {};
    const addresses = Array.isArray(network.p2p_addresses) && network.p2p_addresses.length
      ? network.p2p_addresses
      : [network.p2p_address].filter(Boolean);

    setCopyable("#joinWebUrl", network.web_url || "");
    setCopyable("#joinP2pAddress", addresses.join(" / ") || "");
    setText("#joinNetworkId", network.network_id || "--");
    setCopyable("#joinParamsHash", network.chain_params_hash || "", shortHash(network.chain_params_hash, 12));

    const qrText = addresses[0] || "";
    setText("#joinQrText", qrText || "--");
    drawQr($("#joinQr"), qrText, $("#joinQrNote"));
  }

  function renderAddressQr(status) {
    if (!status) return;
    const address = (status.wallet || {}).address || "";
    // Addresses are 128 hex characters, past what version 10 at ECC M holds,
    // so the QR carries a short form the receive page can still act on.
    drawQr($("#addressQr"), address.length > 200 ? address.slice(0, 200) : address, null);
  }

  function drawQr(canvas, text, noteNode) {
    if (!canvas || !window.QRCode) return;
    const colors = themeColors();
    if (!text) {
      const context = canvas.getContext("2d");
      context.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    const ok = window.QRCode.draw(canvas, text, {
      dark: colors.ink,
      light: colors.surface,
    });
    if (noteNode) {
      noteNode.textContent = ok
        ? "用手机相机扫这张码即可拿到本节点的 P2P 地址。"
        : "内容过长，无法生成二维码，请直接复制上面的地址。";
    }
  }

  function renderLogs(logs) {
    const root = $("#logs");
    if (!root) return;
    const filter = state.logFilter.trim().toLowerCase();
    const items = (Array.isArray(logs) ? logs : []).filter(
      (item) => !filter || String(item.message ?? "").toLowerCase().includes(filter),
    );
    if (!items.length) {
      replaceChildren(root, [
        el("div", { className: "log-item" }, [
          el("span", { className: "log-time", text: "--" }),
          el("span", { text: filter ? "没有匹配的日志" : "暂无日志" }),
        ]),
      ]);
      return;
    }
    replaceChildren(
      root,
      items.map((item) =>
        el("div", { className: "log-item" }, [
          el("span", {
            className: "log-time",
            text: Number(item.time) ? new Date(item.time * 1000).toLocaleTimeString() : "--",
          }),
          el("span", { text: item.message ?? "" }),
        ]),
      ),
    );
  }

  function renderSecurityEvents(events) {
    const body = $("#securityTable");
    if (!body) return;
    state.securityEvents = Array.isArray(events) ? events : [];
    const filter = state.securityFilter.trim().toLowerCase();
    const items = state.securityEvents.filter((item) => {
      if (!filter) return true;
      return [item.type, item.peer_name, item.source, item.reason, item.message]
        .map((value) => String(value ?? "").toLowerCase())
        .some((value) => value.includes(filter));
    });

    body.replaceChildren();
    if (!items.length) {
      emptyRow(body, 8, filter ? "没有匹配的告警" : "暂无安全告警");
      return;
    }
    for (const item of items) {
      const row = el("tr", {
        className: item.severity === "warning" || item.severity === "critical" ? "warning-row" : "",
      });
      row.appendChild(textCell(formatTime(item.time)));
      row.appendChild(textCell(item.type));
      row.appendChild(textCell(item.peer_name));
      row.appendChild(textCell(formatHostPort(item.peer_ip, item.peer_port) || item.source, "mono"));
      row.appendChild(hashCell(item.wallet_address, 8));
      row.appendChild(hashCell(item.tx_id || item.block_hash, 8));
      row.appendChild(textCell(item.reason || item.message));

      const actions = el("td");
      if (item.peer_ip && item.peer_port && state.session.is_admin) {
        actions.appendChild(
          el("button", {
            className: "link-btn danger",
            text: "封禁",
            type: "button",
            onClick: () => banPeer(item.peer_ip, item.peer_port),
          }),
        );
      } else {
        actions.textContent = "--";
      }
      row.appendChild(actions);
      body.appendChild(row);
    }
  }

  // -------------------------------------------------------------- peers

  async function refreshPeers() {
    const data = await api("/api/peers");
    state.peers = Array.isArray(data.peers) ? data.peers : [];
    renderPeers();
  }

  function renderPeers() {
    const body = $("#peersTable");
    if (!body) return;
    body.replaceChildren();
    if (!state.peers.length) {
      emptyRow(body, 9, "暂无节点");
      return;
    }
    state.peers.forEach((peer, index) => {
      const row = el("tr", {
        className: peer.status === "param_mismatch" || peer.mismatch_reason ? "warning-row" : "",
      });
      row.appendChild(textCell(index + 1));
      row.appendChild(textCell(formatHostPort(peer.ip, peer.port), "mono"));
      row.appendChild(textCell(peer.name));
      row.appendChild(hashCell(peer.address, 8));
      row.appendChild(textCell(peer.height));
      row.appendChild(
        textCell(
          peer.mismatch_reason
            ? `${peerStatusLabel(peer.status)}（${peer.mismatch_reason}）`
            : peerStatusLabel(peer.status),
        ),
      );
      row.appendChild(textCell(peer.direction === "inbound" ? "入站" : peer.direction === "outbound" ? "出站" : peer.direction));
      row.appendChild(textCell(formatTime(peer.last_seen)));

      const actions = el("td", { className: "row-actions" });
      if (state.session.is_admin) {
        actions.appendChild(
          el("button", {
            className: "link-btn",
            text: "移除",
            type: "button",
            onClick: () => forgetPeer(peer.ip, peer.port),
          }),
        );
        actions.appendChild(
          el("button", {
            className: peer.status === "banned" ? "link-btn" : "link-btn danger",
            text: peer.status === "banned" ? "解封" : "封禁",
            type: "button",
            onClick: () =>
              peer.status === "banned" ? unbanPeer(peer.ip, peer.port) : banPeer(peer.ip, peer.port),
          }),
        );
      } else {
        actions.textContent = "--";
      }
      row.appendChild(actions);
      body.appendChild(row);
    });
  }

  async function forgetPeer(ip, port) {
    try {
      await api(`/api/peers?ip=${encodeURIComponent(ip)}&port=${encodeURIComponent(port)}`, {
        method: "DELETE",
      });
      showToast("节点已移除");
      await refreshPeers();
    } catch (error) {
      report(error);
    }
  }

  async function banPeer(ip, port) {
    try {
      await post("/api/peers/ban", { ip: String(ip), port: Number(port) });
      showToast(`已封禁 ${formatHostPort(ip, port)}`);
      await refreshPeers();
    } catch (error) {
      report(error);
    }
  }

  async function unbanPeer(ip, port) {
    try {
      await post("/api/peers/unban", { ip: String(ip), port: Number(port) });
      showToast(`已解封 ${formatHostPort(ip, port)}`);
      await refreshPeers();
    } catch (error) {
      report(error);
    }
  }

  // ------------------------------------------------------------- wallets

  async function refreshWallets() {
    const data = await api("/api/wallets");
    const body = $("#walletsTable");
    if (!body) return;
    body.replaceChildren();
    const wallets = Array.isArray(data.wallets) ? data.wallets : [];
    if (!wallets.length) {
      emptyRow(body, 5, "暂无钱包");
      return;
    }
    for (const wallet of wallets) {
      const row = el("tr", { className: wallet.is_default ? "active" : "" });
      row.appendChild(textCell(wallet.is_default ? `${wallet.name}（当前）` : wallet.name));
      row.appendChild(hashCell(wallet.address, 10));
      row.appendChild(textCell(formatBTC(wallet.balance)));
      row.appendChild(
        textCell(
          wallet.immature > 0
            ? `${formatBTC(wallet.available)}（未成熟 ${formatBTC(wallet.immature)}）`
            : formatBTC(wallet.available),
        ),
      );
      const actions = el("td", { className: "row-actions" });
      if (!wallet.is_default && state.session.is_admin) {
        actions.appendChild(
          el("button", {
            className: "link-btn",
            text: "设为当前",
            type: "button",
            onClick: () => selectWallet(wallet.address),
          }),
        );
      } else {
        actions.textContent = wallet.is_default ? "使用中" : "--";
      }
      row.appendChild(actions);
      body.appendChild(row);
    }
  }

  async function selectWallet(address) {
    try {
      await post("/api/wallet/select", { address });
      showToast("已切换当前钱包");
      await Promise.all([refreshStatus(), refreshWallets()]);
    } catch (error) {
      report(error);
    }
  }

  // ------------------------------------------------------- transactions

  async function refreshHistory() {
    const query = new URLSearchParams({
      limit: String(state.historyLimit),
      offset: String(state.historyOffset),
    });
    if (state.historyAddress) query.set("address", state.historyAddress);
    const data = await api(`/api/transactions?${query.toString()}`);
    state.history = data;
    renderHistory(data);
  }

  function renderHistory(data) {
    const summary = $("#historySummary");
    if (summary) {
      const info = data.summary || {};
      replaceChildren(summary, [
        summaryChip("查看地址", data.is_own_wallet ? "本机钱包" : shortHash(data.address, 10)),
        summaryChip("收到", formatBTC(info.received)),
        summaryChip("支出", formatBTC(info.sent)),
        summaryChip("手续费", formatBTC(info.fees_paid)),
        summaryChip("总笔数", String(data.total ?? 0)),
      ]);
    }

    const pendingBody = $("#pendingTable");
    if (pendingBody) {
      pendingBody.replaceChildren();
      const pending = Array.isArray(data.pending) ? data.pending : [];
      if (!pending.length) {
        emptyRow(pendingBody, 6, "内存池里没有和这个地址相关的交易");
      } else {
        for (const item of pending) {
          const row = el("tr", { className: "pending-row" });
          row.appendChild(textCell(DIRECTION_LABELS[item.direction] || item.direction));
          row.appendChild(hashCell(item.tx_id, 10));
          row.appendChild(hashCell(item.direction === "out" ? item.receiver : item.sender, 8));
          row.appendChild(textCell(formatBTC(item.amount)));
          row.appendChild(textCell(formatBTC(item.fee)));
          row.appendChild(textCell(formatTime(item.timestamp)));
          pendingBody.appendChild(row);
        }
      }
    }

    const body = $("#historyTable");
    if (!body) return;
    body.replaceChildren();
    const rows = Array.isArray(data.transactions) ? data.transactions : [];
    if (!rows.length) {
      emptyRow(body, 7, "还没有已确认的交易");
      return;
    }
    for (const item of rows) {
      const row = el("tr");
      row.appendChild(textCell(DIRECTION_LABELS[item.direction] || item.direction));
      row.appendChild(hashCell(item.tx_id, 10));
      row.appendChild(
        hashCell(item.direction === "out" ? item.receiver : item.sender || item.receiver, 8),
      );
      row.appendChild(textCell(formatBTC(item.amount)));
      row.appendChild(textCell(formatBTC(item.fee)));
      const heightCell = el("td");
      heightCell.appendChild(
        el("button", {
          className: "link-btn",
          text: String(item.block_height ?? "--"),
          type: "button",
          onClick: () => {
            activateTab("explorer");
            loadBlockDetail(item.block_height).catch(report);
          },
        }),
      );
      row.appendChild(heightCell);
      row.appendChild(textCell(item.confirmations));
      body.appendChild(row);
    }

    const prev = $("#prevHistoryBtn");
    const next = $("#nextHistoryBtn");
    if (prev) prev.disabled = state.historyOffset === 0;
    if (next) {
      next.disabled = state.historyOffset + state.historyLimit >= Number(data.total || 0);
    }
  }

  function summaryChip(label, value) {
    return el("div", { className: "summary-chip" }, [
      el("span", { text: label }),
      el("strong", { text: value }),
    ]);
  }

  // -------------------------------------------------------------- explorer

  async function refreshBlocks(selectFirst = true) {
    const data = await api(`/api/blocks?limit=${state.blocksLimit}&offset=${state.blocksOffset}`);
    const blocks = Array.isArray(data.blocks) ? data.blocks : [];
    renderBlocks(blocks);
    const prev = $("#prevBlocksBtn");
    const next = $("#nextBlocksBtn");
    if (prev) prev.disabled = state.blocksOffset === 0;
    // Compare against the reported chain height rather than "did this page come
    // back full", which left Next enabled on an exactly-full final page.
    if (next) {
      const total = Number(data.height ?? 0) + 1;
      next.disabled = state.blocksOffset + state.blocksLimit >= total;
    }
    if (selectFirst && blocks.length) {
      await loadBlockDetail(blocks[0].height);
    }
  }

  function renderBlocks(blocks) {
    const body = $("#blocksTable");
    if (!body) return;
    body.replaceChildren();
    if (!blocks.length) {
      emptyRow(body, 6, "暂无区块");
      clearBlockDetail();
      return;
    }
    for (const block of blocks) {
      const row = el("tr", {
        className: "clickable-row",
        dataset: { height: block.height },
        attrs: { tabindex: "0", role: "button", "aria-label": `区块 ${block.height}` },
      });
      row.appendChild(textCell(block.height));
      row.appendChild(hashCell(block.hash, 9));
      row.appendChild(textCell(block.tx_count));
      const target = textCell(targetPreview(block.target, block.difficulty), "mono");
      target.title = String(block.target ?? "");
      row.appendChild(target);
      row.appendChild(textCell(block.nonce));
      row.appendChild(textCell(formatTime(block.timestamp)));

      const open = () => loadBlockDetail(block.height).catch(report);
      row.addEventListener("click", (event) => {
        // let the copy buttons inside cells do their own thing
        if (event.target.closest("button.link-btn")) return;
        open();
      });
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
      body.appendChild(row);
    }
  }

  async function loadBlockDetail(identifier) {
    if (identifier === null || identifier === undefined) return;
    const detail = await api(`/api/blocks/${encodeURIComponent(identifier)}`);
    state.currentBlock = detail;
    renderBlockDetail(detail);
    $$("#blocksTable tr").forEach((row) => {
      row.classList.toggle("active", Number(row.dataset.height) === Number(detail.height));
    });
  }

  function renderBlockDetail(detail) {
    setText("#blockDetailTitle", `高度 ${detail.height}`);
    setCopyable("#detailHash", detail.hash || "");
    setCopyable("#detailPrevHash", detail.prev_hash || "");
    setCopyable("#detailMerkle", detail.merkle_root || "");
    setText("#detailTime", formatTime(detail.timestamp));
    setText("#detailTxCount", `${detail.tx_count} 笔`);
    setText("#detailDifficulty", targetPreview(detail.target, detail.difficulty));
    const difficultyNode = $("#detailDifficulty");
    if (difficultyNode) difficultyNode.title = String(detail.target ?? "");
    setText("#detailNonce", detail.nonce);

    const body = $("#blockTxTable");
    if (!body) return;
    body.replaceChildren();
    const transactions = Array.isArray(detail.transactions) ? detail.transactions : [];
    if (!transactions.length) {
      emptyRow(body, 7, "创世块无交易");
      hideProof();
      return;
    }
    for (const tx of transactions) {
      const row = el("tr");
      row.appendChild(textCell(tx.type === "coinbase" ? "coinbase" : "transfer"));
      row.appendChild(hashCell(tx.tx_id, 8));
      row.appendChild(hashCell(tx.sender, 8));
      row.appendChild(hashCell(tx.receiver, 8));
      row.appendChild(textCell(formatBTC(tx.amount)));
      row.appendChild(textCell(formatBTC(tx.fee)));
      const proofCell = el("td");
      proofCell.appendChild(
        el("button", {
          className: "link-btn",
          text: "验证",
          type: "button",
          title: "生成 Merkle 包含性证明",
          onClick: () => showProof(tx.tx_id),
        }),
      );
      row.appendChild(proofCell);
      body.appendChild(row);
    }
  }

  function clearBlockDetail() {
    setText("#blockDetailTitle", "未选择");
    ["#detailTime", "#detailTxCount", "#detailDifficulty", "#detailNonce"].forEach((selector) =>
      setText(selector, "--"),
    );
    ["#detailHash", "#detailPrevHash", "#detailMerkle"].forEach((selector) =>
      setCopyable(selector, ""),
    );
    const body = $("#blockTxTable");
    if (body) {
      body.replaceChildren();
      emptyRow(body, 7, "请选择一个区块");
    }
    hideProof();
  }

  function hideProof() {
    const box = $("#proofBox");
    if (box) box.hidden = true;
  }

  /**
   * Show the block as a genuine 80-byte Bitcoin header.
   *
   * The simulator hashes canonical JSON so the header stays readable, which
   * means students never see what a real header actually looks like — the
   * reversed hash byte order and the nBits compact target in particular.
   */
  async function showHeaderBytes() {
    const block = state.currentBlock;
    if (!block) {
      showToast("请先选择一个区块");
      return;
    }
    try {
      const described = await api(`/api/blocks/${encodeURIComponent(block.height)}/header`);
      const box = $("#headerBox");
      const body = $("#headerFields");
      if (!box || !body) return;
      box.hidden = false;
      setText("#headerNote", described.note);
      setCopyable("#headerHex", described.hex);
      body.replaceChildren();
      for (const field of described.fields || []) {
        const row = el("tr");
        row.appendChild(textCell(field.name, "mono"));
        row.appendChild(textCell(field.bytes));
        row.appendChild(textCell(field.hex, "mono"));
        row.appendChild(textCell(field.value, "mono"));
        row.appendChild(textCell(field.note));
        body.appendChild(row);
      }
      const summary = el("tr", { className: "pending-row" });
      summary.appendChild(textCell("nBits 解出的目标", "mono"));
      summary.appendChild(textCell("--"));
      summary.appendChild(textCell(described.nbits_hex, "mono"));
      summary.appendChild(textCell(shortHash(described.nbits_target, 12), "mono"));
      summary.appendChild(
        textCell("压缩编码是有损的，解出来的目标略小于模拟器用的目标"),
      );
      body.appendChild(summary);
    } catch (error) {
      report(error);
    }
  }

  /** Competing tips: the only way a fork is ever visible in the console. */
  async function refreshTips() {
    const banner = $("#forkBanner");
    if (!banner) return;
    try {
      const data = await api("/api/tips");
      if (!data.forked) {
        banner.hidden = true;
        return;
      }
      banner.hidden = false;
      const orphans = (data.tips || []).filter((tip) => tip.kind === "orphan");
      replaceChildren(banner, [
        el("strong", { text: `检测到 ${orphans.length} 个竞争区块（分叉）` }),
        el("p", {
          text:
            "这些区块没能接上当前链头，通常是有人和你同时挖到了同一高度。" +
            "节点已经向对方要了整条链，累计工作量更大的那一条会赢。",
        }),
        ...orphans.map((tip) =>
          el("div", { className: "fork-tip mono" }, [
            el("span", { text: `高度 ${tip.height ?? "?"} ` }),
            el("span", { text: shortHash(tip.hash, 10), title: tip.hash }),
            el("span", { text: ` ← ${shortHash(tip.prev_hash, 8)}` }),
          ]),
        ),
      ]);
    } catch (_error) {
      banner.hidden = true;
    }
  }

  /**
   * Show the Merkle path for one transaction.
   * This is SPV made visible: a handful of hashes stands in for the whole
   * block body.
   */
  async function showProof(txId) {
    try {
      const proof = await api(`/api/proof/${encodeURIComponent(txId)}`);
      const box = $("#proofBox");
      const steps = $("#proofSteps");
      if (!box || !steps) return;
      box.hidden = false;
      setText(
        "#proofSummary",
        `区块 ${proof.block_height} 里共 ${proof.tx_count} 笔交易。只用 ${proof.hashes_needed} 个哈希` +
          `（而不是全部 ${proof.tx_count} 笔）就能证明这笔交易在里面，结果${proof.verified ? "与区块头的 merkle_root 一致 ✓" : "不一致 ✗"}。`,
      );
      steps.replaceChildren();
      if (!proof.steps || !proof.steps.length) {
        steps.appendChild(el("li", { text: "这个区块只有一笔交易，它本身就是 merkle root。" }));
        return;
      }
      proof.steps.forEach((step, index) => {
        steps.appendChild(
          el("li", {}, [
            el("span", { className: "proof-index", text: `第 ${index + 1} 步` }),
            el("code", { className: "mono", text: `H(${shortHash(step.left, 6)} ‖ ${shortHash(step.right, 6)})` }),
            el("span", { className: "proof-arrow", text: "→" }),
            el("code", { className: "mono", text: shortHash(step.parent, 8), title: step.parent }),
          ]),
        );
      });
    } catch (error) {
      report(error);
    }
  }

  async function runSearch() {
    const input = $("#blockSearchInput");
    const box = $("#searchResult");
    if (!input) return;
    const term = input.value.trim();
    if (!term) {
      showToast("请输入区块高度、hash、交易 ID 或地址");
      return;
    }
    try {
      const result = await api(`/api/search?q=${encodeURIComponent(term)}`);
      if (box) box.hidden = false;
      if (result.kind === "block") {
        if (box) replaceChildren(box, [el("p", { text: `找到区块 ${result.block.height}` })]);
        state.currentBlock = result.block;
        renderBlockDetail(result.block);
        // jump the list to the page containing the hit
        const height = Number(result.block.height);
        const chainHeight = Number((state.status || {}).height ?? height);
        const position = Math.max(chainHeight - height, 0);
        state.blocksOffset = Math.floor(position / state.blocksLimit) * state.blocksLimit;
        await refreshBlocks(false);
        $$("#blocksTable tr").forEach((row) => {
          row.classList.toggle("active", Number(row.dataset.height) === height);
        });
      } else if (result.kind === "transaction") {
        const tx = result.transaction;
        renderSearchTransaction(box, tx);
        if (tx.block_height !== null && tx.block_height !== undefined) {
          await loadBlockDetail(tx.block_height);
        }
      } else if (result.kind === "address") {
        if (box) {
          replaceChildren(box, [
            el("p", { text: `地址 ${shortHash(result.address.address, 12)}` }),
            el("p", {
              text: `收到 ${formatBTC(result.address.received)}，支出 ${formatBTC(result.address.sent)}，共 ${result.address.received_count + result.address.sent_count} 笔`,
            }),
            el("button", {
              className: "ghost-btn",
              text: "在交易页查看全部记录",
              type: "button",
              onClick: () => {
                state.historyAddress = result.address.address;
                state.historyOffset = 0;
                const field = $("#historyAddressInput");
                if (field) field.value = result.address.address;
                activateTab("history");
                refreshHistory().catch(report);
              },
            }),
          ]);
        }
      } else if (result.kind === "suggestions") {
        if (box) {
          replaceChildren(box, [
            el("p", { text: "没有精确匹配，下面是前缀相同的地址：" }),
            ...result.addresses.map((address) =>
              el("button", {
                className: "link-btn",
                text: shortHash(address, 12),
                type: "button",
                onClick: () => {
                  input.value = address;
                  runSearch().catch(report);
                },
              }),
            ),
          ]);
        }
      } else if (box) {
        replaceChildren(box, [el("p", { text: "没有找到匹配的区块、交易或地址" })]);
      }
    } catch (error) {
      report(error);
    }
  }

  function renderSearchTransaction(box, tx) {
    if (!box) return;
    replaceChildren(box, [
      el("p", { text: `交易 ${shortHash(tx.tx_id, 12)}` }),
      el("p", {
        text:
          tx.state === "pending"
            ? "状态：待确认（还在内存池里）"
            : `状态：已确认，位于高度 ${tx.block_height}，确认数 ${tx.confirmations ?? "--"}`,
      }),
      el("p", { text: `数量 ${formatBTC(tx.amount)}，手续费 ${formatBTC(tx.fee)}` }),
    ]);
  }

  // ---------------------------------------------------------------- charts

  function drawAxes(context, width, height, colors) {
    context.strokeStyle = colors.grid;
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(38.5, 8);
    context.lineTo(38.5, height - 22.5);
    context.lineTo(width - 8, height - 22.5);
    context.stroke();
  }

  function chartFrame(canvas) {
    if (!canvas) return null;
    const context = canvas.getContext("2d");
    const colors = themeColors();
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = colors.surface;
    context.fillRect(0, 0, canvas.width, canvas.height);
    drawAxes(context, canvas.width, canvas.height, colors);
    return {
      context,
      colors,
      left: 40,
      right: canvas.width - 8,
      top: 8,
      bottom: canvas.height - 23,
    };
  }

  function labelAxis(frame, minimum, maximum, formatter) {
    const { context, colors, left, top, bottom } = frame;
    context.fillStyle = colors.muted;
    context.font = "10px system-ui, sans-serif";
    context.textAlign = "right";
    context.fillText(formatter(maximum), left - 5, top + 9);
    context.fillText(formatter(minimum), left - 5, bottom);
    context.textAlign = "left";
  }

  function lineChart(canvas, points, options = {}) {
    const frame = chartFrame(canvas);
    if (!frame) return;
    const values = points.filter((value) => Number.isFinite(value));
    if (values.length < 2) {
      frame.context.fillStyle = frame.colors.muted;
      frame.context.font = "12px system-ui, sans-serif";
      frame.context.fillText("数据不足，多挖几个区块", frame.left + 12, (frame.top + frame.bottom) / 2);
      return;
    }
    const maximum = options.max ?? Math.max(...values);
    const minimum = options.min ?? Math.min(...values, 0);
    const span = maximum - minimum || 1;
    const stepX = (frame.right - frame.left) / Math.max(points.length - 1, 1);
    const toY = (value) => frame.bottom - ((value - minimum) / span) * (frame.bottom - frame.top);

    const { context, colors } = frame;

    if (options.reference !== undefined && Number.isFinite(options.reference)) {
      context.strokeStyle = colors.accentSoft;
      context.setLineDash([4, 4]);
      context.beginPath();
      context.moveTo(frame.left, toY(options.reference));
      context.lineTo(frame.right, toY(options.reference));
      context.stroke();
      context.setLineDash([]);
    }

    context.strokeStyle = options.color || colors.accent;
    context.lineWidth = 2;
    context.beginPath();
    let started = false;
    points.forEach((value, index) => {
      if (!Number.isFinite(value)) return;
      const x = frame.left + index * stepX;
      const y = toY(value);
      if (!started) {
        context.moveTo(x, y);
        started = true;
      } else {
        context.lineTo(x, y);
      }
    });
    context.stroke();
    labelAxis(frame, minimum, maximum, options.format || ((value) => String(Math.round(value))));
  }

  function barChart(canvas, series, labels, options = {}) {
    const frame = chartFrame(canvas);
    if (!frame) return;
    const { context, colors } = frame;
    const groups = series[0] ? series[0].length : 0;
    if (!groups) {
      context.fillStyle = colors.muted;
      context.font = "12px system-ui, sans-serif";
      context.fillText("暂无数据", frame.left + 12, (frame.top + frame.bottom) / 2);
      return;
    }
    const totals = [];
    for (let i = 0; i < groups; i += 1) {
      totals.push(series.reduce((sum, row) => sum + (Number(row[i]) || 0), 0));
    }
    const maximum = options.max ?? Math.max(...totals, 1);
    const slot = (frame.right - frame.left) / groups;
    const barWidth = Math.max(Math.min(slot * 0.62, 26), 2);
    const palette = options.colors || [colors.accent, colors.warn];

    for (let i = 0; i < groups; i += 1) {
      let y = frame.bottom;
      series.forEach((row, layer) => {
        const value = Number(row[i]) || 0;
        const barHeight = (value / maximum) * (frame.bottom - frame.top);
        if (barHeight <= 0) return;
        context.fillStyle = palette[layer % palette.length];
        context.fillRect(frame.left + i * slot + (slot - barWidth) / 2, y - barHeight, barWidth, barHeight);
        y -= barHeight;
      });
    }

    if (labels && labels.length === groups) {
      context.fillStyle = colors.muted;
      context.font = "10px system-ui, sans-serif";
      context.textAlign = "center";
      labels.forEach((label, i) => {
        context.fillText(String(label), frame.left + i * slot + slot / 2, frame.bottom + 13);
      });
      context.textAlign = "left";
    }
    labelAxis(frame, 0, maximum, options.format || ((value) => value.toFixed(2)));
  }

  async function refreshCharts() {
    const data = await api("/api/stats?window=120");
    state.chartData = data;
    renderCharts(data);
  }

  function renderCharts(data) {
    if (!data) return;
    const blocks = Array.isArray(data.blocks) ? data.blocks : [];
    const supply = data.supply || {};

    const summary = $("#chartSummary");
    if (summary) {
      replaceChildren(summary, [
        summaryChip("平均出块", data.average_interval === null || data.average_interval === undefined ? "--" : `${data.average_interval} 秒`),
        summaryChip("目标出块", `${data.target_block_seconds} 秒`),
        summaryChip("估算算力", data.estimated_hashrate ? `${formatCount(data.estimated_hashrate)}H/s` : "--"),
        summaryChip("当前奖励", formatBTC(supply.next_block_subsidy)),
        summaryChip("已发行", `${formatBTC(supply.total_issued)} / ${formatBTC(supply.max_supply)}`),
        summaryChip("距下次减半", `${supply.blocks_to_halving ?? "--"} 块`),
      ]);
    }

    const intervals = blocks.map((block) => (block.interval === null ? NaN : Number(block.interval)));
    lineChart($("#chartInterval"), intervals, {
      reference: data.target_block_seconds,
      format: (value) => `${Math.round(value)}s`,
    });
    setText(
      "#noteInterval",
      data.average_interval
        ? `虚线是目标出块时间 ${data.target_block_seconds} 秒；实际平均 ${data.average_interval} 秒。持续偏快会让难度加 1 位。`
        : "还没有足够的区块来统计间隔。",
    );

    lineChart($("#chartDifficulty"), blocks.map((block) => Number(block.difficulty)), {
      format: (value) => `${Math.round(value)}`,
    });
    const difficulties = blocks.map((block) => Number(block.difficulty));
    setText(
      "#noteDifficulty",
      difficulties.length
        ? `每一级代表 hash 需要多 1 个二进制前导 0，也就是难度翻倍。当前 ${difficulties[difficulties.length - 1]} 位。`
        : "暂无数据。",
    );

    lineChart($("#chartHashrate"), blocks.map((block) => (block.hashrate === null ? NaN : Number(block.hashrate))), {
      format: (value) => formatCount(value),
    });
    setText(
      "#noteHashrate",
      "由「2^难度 次尝试 ÷ 实际出块秒数」估算，波动大是正常的：挖矿本来就是随机过程。",
    );

    lineChart($("#chartIssued"), blocks.map((block) => Number(block.issued)), {
      format: (value) => value.toFixed(2),
      color: themeColors().accent,
    });
    setText(
      "#noteIssued",
      `窗口内累计发行 ${formatBTC(blocks.length ? blocks[blocks.length - 1].issued : 0)}；全链已发行 ${formatBTC(supply.total_issued)}，上限 ${formatBTC(supply.max_supply)}。`,
    );

    barChart(
      $("#chartReward"),
      [blocks.map((block) => Number(block.subsidy)), blocks.map((block) => Number(block.fees))],
      blocks.map((block) => block.height),
      { format: (value) => value.toFixed(2) },
    );
    setText(
      "#noteReward",
      "橙色是区块奖励（会随减半下降），绿色是手续费。真实比特币里前者最终归零，矿工只剩后者。",
    );

    const buckets = Array.isArray(data.mempool_fee_buckets) ? data.mempool_fee_buckets : [];
    barChart(
      $("#chartMempool"),
      [buckets.map((bucket) => Number(bucket.count))],
      buckets.map((bucket) => bucket.label),
      { format: (value) => String(Math.round(value)) },
    );
    const pending = buckets.reduce((sum, bucket) => sum + Number(bucket.count || 0), 0);
    setText(
      "#noteMempool",
      pending
        ? `内存池里有 ${pending} 笔待打包交易，按手续费分组。矿工优先打包费率高的那些。`
        : "内存池是空的。发一笔交易再回来看。",
    );
  }

  // ------------------------------------------------------------- classroom

  async function refreshClassroom() {
    const data = await api("/api/classroom");
    state.classroom = data;
    renderClassroom(data);
  }

  function renderClassroom(data) {
    const nodes = Array.isArray(data.nodes) ? data.nodes : [];
    const heights = nodes.map((node) => Number(node.height || 0));
    const mining = nodes.filter((node) => node.mining_status === "mining");

    setText("#teacherNodeCount", nodes.length);
    setText("#teacherMaxHeight", heights.length ? Math.max(...heights) : 0);
    setText("#teacherMiningCount", mining.length);
    setText("#teacherMismatchCount", data.mismatch_count || 0);

    const alert = $("#classroomAlert");
    if (alert) {
      alert.hidden = !data.mismatch_count;
      alert.textContent = data.mismatch_count
        ? `检测到 ${data.mismatch_count} 个节点网络参数不一致，请检查 network_id 或难度规则。`
        : "";
    }

    const body = $("#classroomTable");
    if (!body) return;
    body.replaceChildren();
    if (!nodes.length) {
      emptyRow(body, 8, "暂无节点");
      return;
    }
    for (const node of nodes) {
      const row = el("tr", {
        className: node.status === "param_mismatch" || node.mismatch_reason ? "warning-row" : "",
      });
      row.appendChild(textCell(node.name || node.node_name));
      row.appendChild(textCell(formatHostPort(node.ip, node.port), "mono"));
      row.appendChild(textCell(node.height));
      const target = textCell(targetPreview(node.target, node.difficulty), "mono");
      target.title = String(node.target ?? "");
      row.appendChild(target);
      row.appendChild(textCell(minerStatusLabel(node.mining_status)));
      row.appendChild(textCell(peerStatusLabel(node.status)));
      row.appendChild(hashCell(node.chain_params_hash, 8));
      row.appendChild(textCell(node.mismatch_reason));
      body.appendChild(row);
    }
  }

  // ------------------------------------------------------------------- lab

  async function refreshLab() {
    const data = await api("/api/lab");
    const root = $("#labList");
    if (!root) return;
    const tasks = Array.isArray(data.tasks) ? data.tasks : [];
    replaceChildren(
      root,
      tasks.map((task, index) =>
        el("div", { className: `lab-item ${task.done ? "done" : ""}` }, [
          el("span", { className: "lab-index", text: String(index + 1) }),
          el("div", {}, [
            el("strong", { text: task.title ?? "" }),
            el("small", { text: task.detail ?? "" }),
          ]),
          el("span", { className: "lab-state", text: task.done ? "完成" : "待做" }),
        ]),
      ),
    );
  }

  // ------------------------------------------------------------------ tabs

  const TAB_LOADERS = {
    nodes: () => refreshPeers(),
    explorer: () => Promise.all([refreshBlocks(), refreshTips()]),
    history: () => refreshHistory(),
    charts: () => refreshCharts(),
    receive: () => refreshWallets(),
    security: () => refreshSecurityEvents(),
    lab: () => refreshLab(),
    classroom: () => refreshClassroom(),
  };

  function activateTab(name, { updateHash = true } = {}) {
    const button = $(`#tabbtn-${name}`);
    const panel = $(`#tab-${name}`);
    if (!button || !panel || button.hidden) return;

    $$(".tab").forEach((tab) => {
      const selected = tab === button;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
    });
    $$(".tab-panel").forEach((item) => {
      const selected = item === panel;
      item.classList.toggle("active", selected);
      item.hidden = !selected;
    });

    if (updateHash && window.location.hash !== `#${name}`) {
      // Deep links: refreshing used to always drop you back on the mining tab,
      // and there was no way to send someone a link to a specific page.
      history.replaceState(null, "", `#${name}${window.location.search ? "" : ""}`);
    }

    const loader = TAB_LOADERS[name];
    if (loader) loader().catch(report);

    if (name === "classroom") startClassroomTimer();
    else stopClassroomTimer();
  }

  function setupTabs() {
    const tabs = $$(".tab");
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => activateTab(tab.dataset.tab));
      tab.addEventListener("keydown", (event) => {
        const visible = $$(".tab").filter((item) => !item.hidden);
        const index = visible.indexOf(tab);
        let target = null;
        if (event.key === "ArrowRight") target = visible[(index + 1) % visible.length];
        if (event.key === "ArrowLeft") target = visible[(index - 1 + visible.length) % visible.length];
        if (event.key === "Home") target = visible[0];
        if (event.key === "End") target = visible[visible.length - 1];
        if (!target) return;
        event.preventDefault();
        target.focus();
        activateTab(target.dataset.tab);
      });
    });

    window.addEventListener("hashchange", () => {
      const name = window.location.hash.replace("#", "");
      if (name) activateTab(name, { updateHash: false });
    });
  }

  function startClassroomTimer() {
    stopClassroomTimer();
    const toggle = $("#classroomAutoRefresh");
    if (toggle && !toggle.checked) return;
    // The teacher view is the one screen that most needs to be live, and it
    // used to only update when somebody clicked the button.
    state.classroomTimer = setInterval(() => {
      if (document.hidden) return;
      refreshClassroom().catch(() => {});
    }, 4000);
  }

  function stopClassroomTimer() {
    if (state.classroomTimer) {
      clearInterval(state.classroomTimer);
      state.classroomTimer = null;
    }
  }

  // ---------------------------------------------------------------- export

  function toCsv(rows) {
    return rows
      .map((row) =>
        row
          .map((cell) => {
            const text = String(cell ?? "");
            return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
          })
          .join(","),
      )
      .join("\n");
  }

  function downloadCsv(filename, rows) {
    const blob = new Blob(["﻿", toCsv(rows)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    showToast(`已导出 ${filename}`);
  }

  // --------------------------------------------------------------- actions

  function on(selector, event, handler) {
    const node = $(selector);
    if (!node) return;
    node.addEventListener(event, handler);
  }

  function setupActions() {
    // Every listener is attached independently. The old setupActions() was one
    // unguarded sequential function: a single missing element threw and
    // silently prevented every later listener -- mining, send, peers -- from
    // ever binding.
    on("#refreshBtn", "click", () => refreshStatus().catch(report));
    on("#refreshLogsBtn", "click", () => refreshStatus().catch(report));
    on("#refreshPeersBtn", "click", () => refreshPeers().catch(report));
    on("#refreshSecurityBtn", "click", () => refreshSecurityEvents().catch(report));
    on("#refreshBlocksBtn", "click", () => refreshBlocks().catch(report));
    on("#refreshClassroomBtn", "click", () => refreshClassroom().catch(report));
    on("#refreshChartsBtn", "click", () => refreshCharts().catch(report));
    on("#refreshHistoryBtn", "click", () => refreshHistory().catch(report));
    on("#refreshWalletsBtn", "click", () => refreshWallets().catch(report));
    on("#refreshLabBtn", "click", () => refreshLab().catch(report));

    on("#syncTopBtn", "click", syncBlocks);
    on("#syncNodesBtn", "click", syncBlocks);
    on("#syncClassroomBtn", "click", syncBlocks);

    on("#logFilter", "input", (event) => {
      state.logFilter = event.target.value;
      renderLogs((state.status || {}).logs || []);
    });
    on("#securityFilter", "input", (event) => {
      state.securityFilter = event.target.value;
      renderSecurityEvents(state.securityEvents);
    });

    on("#classroomAutoRefresh", "change", (event) => {
      if (event.target.checked) startClassroomTimer();
      else stopClassroomTimer();
    });

    on("#copyP2pBtn", "click", () => {
      const network = (state.status || {}).network || {};
      copyText(
        (network.p2p_addresses || [network.p2p_address]).filter(Boolean).join("\n"),
        "P2P 地址",
      );
    });
    on("#copyWebBtn", "click", () => copyText(((state.status || {}).network || {}).web_url, "Web 地址"));
    on("#copyJoinBundleBtn", "click", () => {
      const network = (state.status || {}).network || {};
      const addresses = (network.p2p_addresses || [network.p2p_address]).filter(Boolean);
      copyText(
        [
          `Web: ${network.web_url || ""}`,
          `P2P: ${addresses.join(", ")}`,
          `Network ID: ${network.network_id || ""}`,
          `Params: ${network.chain_params_hash || ""}`,
        ].join("\n"),
        "入网信息",
      );
    });
    on("#copyAddressBtn", "click", () => copyText(((state.status || {}).wallet || {}).address, "地址"));

    // Any element marked copyable copies its full value, so a truncated hash
    // on screen is still recoverable. Keyboard users get it too.
    document.addEventListener("click", (event) => {
      const node = event.target.closest(".copyable");
      if (node && node.dataset.copy) copyText(node.dataset.copy, "内容");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const node = event.target.closest(".copyable");
      if (node && node.dataset.copy) {
        event.preventDefault();
        copyText(node.dataset.copy, "内容");
      }
    });

    on("#prevBlocksBtn", "click", () => {
      state.blocksOffset = Math.max(0, state.blocksOffset - state.blocksLimit);
      refreshBlocks().catch(report);
    });
    on("#nextBlocksBtn", "click", () => {
      state.blocksOffset += state.blocksLimit;
      refreshBlocks().catch(report);
    });
    on("#blockSearchBtn", "click", () => runSearch().catch(report));
    on("#blockSearchInput", "keydown", (event) => {
      if (event.key === "Enter") runSearch().catch(report);
    });
    on("#closeProofBtn", "click", hideProof);
    on("#showHeaderBtn", "click", () => showHeaderBytes());
    on("#closeHeaderBtn", "click", () => {
      const box = $("#headerBox");
      if (box) box.hidden = true;
    });

    on("#prevHistoryBtn", "click", () => {
      state.historyOffset = Math.max(0, state.historyOffset - state.historyLimit);
      refreshHistory().catch(report);
    });
    on("#nextHistoryBtn", "click", () => {
      state.historyOffset += state.historyLimit;
      refreshHistory().catch(report);
    });
    on("#historyAddressBtn", "click", () => {
      const field = $("#historyAddressInput");
      state.historyAddress = field && field.value.trim() ? field.value.trim() : null;
      state.historyOffset = 0;
      refreshHistory().catch(report);
    });
    on("#historyAddressInput", "keydown", (event) => {
      if (event.key === "Enter") $("#historyAddressBtn").click();
    });

    on("#exportTxBtn", "click", () => {
      const data = state.history;
      if (!data) return;
      const rows = [["方向", "tx_id", "发送方", "接收方", "数量", "手续费", "高度", "确认数", "状态"]];
      for (const item of [...(data.pending || []), ...(data.transactions || [])]) {
        rows.push([
          DIRECTION_LABELS[item.direction] || item.direction,
          item.tx_id,
          item.sender || "",
          item.receiver || "",
          item.amount,
          item.fee,
          item.block_height ?? "",
          item.confirmations ?? 0,
          item.state,
        ]);
      }
      downloadCsv("transactions.csv", rows);
    });

    on("#exportPeersBtn", "click", () => {
      const rows = [["ip", "port", "名字", "钱包地址", "高度", "状态", "方向", "最后通信"]];
      for (const peer of state.peers) {
        rows.push([
          peer.ip,
          peer.port,
          peer.name || "",
          peer.address || "",
          peer.height ?? "",
          peerStatusLabel(peer.status),
          peer.direction || "",
          formatTime(peer.last_seen),
        ]);
      }
      downloadCsv("peers.csv", rows);
    });

    on("#exportSecurityBtn", "click", () => {
      const rows = [["时间", "类型", "节点", "ip", "port", "钱包地址", "对象", "原因"]];
      for (const item of state.securityEvents) {
        rows.push([
          formatTime(item.time),
          item.type || "",
          item.peer_name || "",
          item.peer_ip || "",
          item.peer_port || "",
          item.wallet_address || "",
          item.tx_id || item.block_hash || "",
          item.reason || item.message || "",
        ]);
      }
      downloadCsv("security-events.csv", rows);
    });

    on("#difficultyForm", "submit", saveDifficulty);
    on("#resetChainBtn", "click", resetChain);

    on("#startMiningBtn", "click", async () => {
      try {
        await post("/api/mining/start");
        showToast("挖矿已启动");
        await refreshStatus();
      } catch (error) {
        report(error);
      }
    });

    on("#stopMiningBtn", "click", async () => {
      try {
        await post("/api/mining/stop");
        showToast("挖矿已暂停");
        await refreshStatus();
      } catch (error) {
        report(error);
      }
    });

    on("#autoFeeBtn", "click", () => {
      // A fee that actually reflects congestion instead of a hardcoded 0.01.
      const buckets = ((state.chartData || {}).mempool_fee_buckets) || [];
      const pending = buckets.reduce((sum, bucket) => sum + Number(bucket.count || 0), 0);
      const suggested = pending > 20 ? 0.05 : pending > 5 ? 0.02 : 0.01;
      const field = $("#feeInput");
      if (field) field.value = String(suggested);
      showToast(
        pending
          ? `内存池有 ${pending} 笔待确认，建议手续费 ${suggested} BTC`
          : `内存池是空的，${suggested} BTC 就够了`,
      );
    });

    on("#clearSendBtn", "click", () => {
      const form = $("#sendForm");
      if (form) form.reset();
      const fee = $("#feeInput");
      if (fee) fee.value = "0.01";
      const receipt = $("#sendReceipt");
      if (receipt) receipt.hidden = true;
    });

    on("#generateAddressBtn", "click", async () => {
      const confirmed = window.confirm(
        "会新建一个钱包并切换成当前钱包。原来的钱包和它的余额仍然保留，可以在下面的钱包管理里切回去。继续吗？",
      );
      if (!confirmed) return;
      try {
        await post("/api/wallet/generate", { name: `wallet-${Date.now().toString(36)}` });
        showToast("新钱包已生成");
        await Promise.all([refreshStatus(), refreshWallets()]);
      } catch (error) {
        report(error);
      }
    });

    on("#exportKeyBtn", "click", async () => {
      try {
        const data = await api("/api/wallet/export");
        const node = $("#exportedKey");
        if (node) {
          node.hidden = false;
          node.textContent = data.private_key;
          node.dataset.copy = data.private_key;
        }
        showToast("私钥已显示，请妥善保管");
      } catch (error) {
        report(error);
      }
    });

    on("#importForm", "submit", async (event) => {
      event.preventDefault();
      const field = $("#importKeyInput");
      if (!field || !field.value.trim()) return;
      try {
        const wallet = await post("/api/wallet/import", {
          private_key: field.value.trim(),
          name: "imported",
        });
        field.value = "";
        showToast(`已导入钱包 ${shortHash(wallet.address, 8)}`);
        await Promise.all([refreshStatus(), refreshWallets()]);
      } catch (error) {
        report(error);
      }
    });

    on("#sendForm", "submit", async (event) => {
      event.preventDefault();
      const amount = Number(($("#amountInput") || {}).value);
      const fee = Number(($("#feeInput") || {}).value);
      const available = Number((state.status || {}).available_balance || 0);
      if (!Number.isFinite(amount) || amount <= 0) {
        showError("数量必须大于 0");
        return;
      }
      if (amount + fee > available + 1e-8) {
        showError(
          `可用余额不够：需要 ${formatBTC(amount + fee)}，可用 ${formatBTC(available)}`,
        );
        return;
      }
      try {
        const result = await post("/api/transactions", {
          receiver: ($("#receiverInput") || {}).value.trim(),
          amount,
          fee,
          note: ($("#noteInput") || {}).value.trim() || null,
        });
        // Keep the tx_id on screen. It used to appear in a 2.8-second toast and
        // then be gone forever, with no way to look it up again.
        const receipt = $("#sendReceipt");
        const idNode = $("#receiptTxId");
        if (receipt && idNode) {
          receipt.hidden = false;
          idNode.textContent = result.tx_id;
          idNode.dataset.copy = result.tx_id;
        }
        showToast("交易已进入内存池");
        await refreshStatus();
      } catch (error) {
        report(error);
      }
    });

    on("#receiptViewBtn", "click", () => {
      state.historyAddress = null;
      state.historyOffset = 0;
      activateTab("history");
    });

    on("#peerForm", "submit", async (event) => {
      event.preventDefault();
      try {
        const result = await post("/api/peers", {
          ip: ($("#peerIpInput") || {}).value.trim(),
          port: Number(($("#peerPortInput") || {}).value),
        });
        showToast(result.message);
        await refreshPeers();
      } catch (error) {
        report(error);
      }
    });
  }

  async function syncBlocks() {
    try {
      const result = await post("/api/sync");
      showToast(`已向 ${result.requested_peers} 个节点请求同步`);
    } catch (error) {
      report(error);
    }
  }

  async function saveDifficulty(event) {
    event.preventDefault();
    const difficulty = Number(($("#difficultyInput") || {}).value);
    if (!Number.isInteger(difficulty) || difficulty < 0 || difficulty > 255) {
      showError("难度必须是 0 到 255 的整数");
      return;
    }
    try {
      const result = await post("/api/settings/difficulty", { difficulty });
      showToast(`难度已设置为 ${result.difficulty}${result.mining_stopped ? "，挖矿已暂停" : ""}`);
      await refreshStatus();
      await refreshBlocks(false);
    } catch (error) {
      report(error);
    }
  }

  async function resetChain() {
    const confirmed = window.confirm(
      "确定要重置本节点区块链吗？这会清空历史区块、交易和内存池，只保留创世块。",
    );
    if (!confirmed) return;
    try {
      const result = await post("/api/chain/reset");
      state.blocksOffset = 0;
      showToast(`已重置到创世块：${shortHash(result.tip_hash, 8)}`);
      await refreshStatus();
      await refreshBlocks();
    } catch (error) {
      report(error);
    }
  }

  async function refreshStatus() {
    renderStatus(await api("/api/status"));
  }

  async function refreshSecurityEvents() {
    const data = await api("/api/security-events?limit=200");
    renderSecurityEvents(data.events || []);
  }

  // ------------------------------------------------------------- session

  async function refreshSession() {
    try {
      state.session = await api("/api/session");
    } catch (_error) {
      state.session = { is_admin: false, enforced: true };
    }
    const isAdmin = Boolean(state.session.is_admin);
    $$(".admin-only").forEach((node) => {
      node.hidden = !isAdmin;
    });
    const hint = $("#adminHint");
    if (hint) {
      hint.hidden = isAdmin;
      hint.textContent = isAdmin
        ? ""
        : "这个页面是只读的。要修改这个节点，请在它自己的电脑上打开控制台，或在网址后加上 ?token=节点的 admin token（启动时打印在终端里）。";
    }
  }

  // ------------------------------------------------------------ live feed

  function setConnectionState(value, label) {
    const pill = $("#connPill");
    if (!pill) return;
    pill.dataset.state = value;
    pill.textContent = label;
  }

  function connectEvents() {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    let socket;
    try {
      socket = new WebSocket(`${protocol}://${window.location.host}/ws/events`);
    } catch (_error) {
      scheduleReconnect();
      return;
    }
    state.socket = socket;

    socket.addEventListener("open", () => {
      state.reconnectDelay = 1000;
      setConnectionState("online", "实时");
    });

    socket.addEventListener("message", (event) => {
      // One malformed frame used to throw out of the handler and silently
      // freeze the live console with no indication anything was wrong.
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      if (!payload || typeof payload !== "object") return;
      const { full, ...rest } = payload;
      // The socket now sends only the sections that changed, so merge.
      const merged = full ? rest : { ...(state.status || {}), ...rest };
      try {
        renderStatus(merged);
      } catch (error) {
        report(error);
      }
    });

    socket.addEventListener("error", () => setConnectionState("offline", "连接异常"));
    socket.addEventListener("close", () => {
      setConnectionState("offline", "已断开");
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    // Exponential backoff with a ceiling, instead of hammering a dead server
    // every 1.5 seconds forever with no user-visible indication.
    setConnectionState("connecting", `重连中`);
    setTimeout(connectEvents, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 30000);
  }

  // ------------------------------------------------------------------ boot

  async function boot() {
    state.blocksLimit = BLOCKS_LIMIT;
    setupTheme();
    setupTabs();
    setupActions();
    await refreshSession();

    const initial = window.location.hash.replace("#", "") || "mining";
    activateTab($(`#tabbtn-${initial}`) ? initial : "mining", { updateHash: false });

    refreshStatus().catch(report);
    refreshLab().catch(() => {});
    connectEvents();

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshStatus().catch(() => {});
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => boot().catch(report));
  } else {
    boot().catch(report);
  }
})();
