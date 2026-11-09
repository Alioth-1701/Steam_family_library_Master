"use strict";

const IMAGE_BASE_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/";
const STALE_HOURS = 36;

const REASON_EXPLANATIONS = {
  "excluded by owner":
    "游戏拥有者已关闭这款游戏的共享，因此当前成员无法通过家庭库游玩；这不代表游戏已从拥有者的库存中移除。",
  "not eligible for sharing":
    "这款游戏不支持 Steam 家庭共享，可能涉及发行商限制、第三方账号或其他共享条件。",
  "region or DRM restricted":
    "受地区或 DRM 限制，当前成员无法通过家庭共享游玩。",
};

// 所有数据、筛选、展开、弹窗状态集中在这里；渲染统一走 render()。
const state = {
  games: [],
  memberUnshared: [],
  searchIndex: [],
  total: 0,
  mainCount: 0,
  updatedAt: null,
  search: "",
  selectedOwners: new Set(),
  playableOnly: true,
  newOnly: false,
  sortBy: "recent",
  view: "grid",
  newExpanded: false,
  libraryExpanded: false,
  openProviderCards: new Set(),
  memberDrawerOpen: false,
  openMemberGroups: new Set(),
  currentGame: null,
  lastFocusedElement: null,
  loading: true,
  error: "",
  isMock: false,
  ownerSignature: "",
};

const elements = {};
let toastTimer = null;
let resizeTimer = null;

function cacheElements() {
  const ids = [
    "updated-at", "total-count", "main-count-note", "new-count",
    "unplayable-count", "member-panel-button", "member-panel-badge",
    "stale-alert", "load-error", "new-games", "new-empty",
    "new-section-count", "toggle-new", "result-count", "toggle-library",
    "search-input", "owner-filter", "owner-filter-count", "owner-options",
    "clear-owners", "playable-only", "new-only", "sort-select", "random-button",
    "games", "loading", "empty-results", "reset-filters", "game-modal",
    "modal-poster", "modal-title", "modal-owners", "modal-acquired",
    "modal-playtime", "modal-copy", "modal-reason", "store-link",
    "xiaoheihe-link", "close-modal", "reroll-button", "member-drawer-backdrop",
    "member-drawer", "close-member-drawer", "member-empty", "member-groups",
    "reason-backdrop", "reason-popover-text", "close-reason", "toast",
  ];
  ids.forEach(id => { elements[id] = document.getElementById(id); });
}

function asText(value) {
  return typeof value === "string" ? value.trim() : "";
}

function asDate(value) {
  const text = asText(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return "";
  return Number.isNaN(Date.parse(`${text}T00:00:00Z`)) ? "" : text;
}

function asAppId(value) {
  const appid = Number.parseInt(value, 10);
  return Number.isFinite(appid) && appid > 0 ? appid : 0;
}

function asCount(value, owners) {
  const count = Number.parseInt(value, 10);
  if (Number.isFinite(count) && count > 0) return count;
  return Math.max(1, new Set(owners).size || 1);
}

// 补齐缺失字段；copy_count 缺失时按去重后的 owners 回退。
function normalizeGame(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const appid = asAppId(source.appid);
  const owners = Array.isArray(source.owners)
    ? [...new Set(source.owners.map(asText).filter(Boolean))]
    : [];
  const minutes = Number(source.playtime_minutes);
  return {
    appid,
    name: asText(source.name) || (appid ? `Steam App ${appid}` : "未命名游戏"),
    owners,
    copyCount: asCount(source.copy_count, owners),
    acquired: asDate(source.acquired),
    firstSeenAt: asDate(source.first_seen_at),
    playtimeMinutes: Number.isFinite(minutes) && minutes > 0 ? minutes : 0,
    playable: source.playable === undefined ? true : Boolean(source.playable),
    excludeReason: asText(source.exclude_reason),
    isNew: Boolean(source.is_new),
  };
}

function normalizeMemberGroup(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const games = Array.isArray(source.games) ? source.games : [];
  return {
    member: asText(source.member) || "未知成员",
    updatedAt: parseUpdatedAt(source.updated_at),
    games: games.map(normalizeGame),
  };
}

function parseUpdatedAt(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function loadPayload(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const games = Array.isArray(source.games) ? source.games.map(normalizeGame) : [];
  const memberGroups = Array.isArray(source.member_unshared)
    ? source.member_unshared.map(normalizeMemberGroup)
    : [];
  const reportedMainCount = Number(source.count);
  const reportedTotalCount = Number(source.library_total_count);
  const reportedUnshareableCount = Number(source.unshareable_total_count);

  state.games = games;
  state.memberUnshared = memberGroups;
  state.mainCount = Number.isFinite(reportedMainCount) && reportedMainCount >= 0
    ? reportedMainCount
    : games.length;
  // 旧版数据没有新字段：总数回退主库 count，不可共享回退主库不可玩数量。
  state.total = Number.isFinite(reportedTotalCount) && reportedTotalCount >= 0
    ? reportedTotalCount
    : state.mainCount;
  const memberRecordCount = memberGroups.reduce((sum, group) => sum + group.games.length, 0);
  const mainUnplayableCount = games.filter(game => !game.playable).length;
  state.unshareableCount = Number.isFinite(reportedUnshareableCount) && reportedUnshareableCount >= 0
    ? reportedUnshareableCount
    : memberRecordCount || mainUnplayableCount;
  state.updatedAt = parseUpdatedAt(source.updated_at);
  state.searchIndex = buildSearchIndex();
}

// 默认读取真实公开数据；?mock=1 仅用于本地模拟预览。
async function loadData() {
  state.isMock = new URLSearchParams(window.location.search).get("mock") === "1";
  state.loading = true;
  render();
  try {
    const response = await fetch(
      state.isMock ? "data/mock_library.json" : "data/public_library.json"
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    loadPayload(await response.json());
    state.error = "";
  } catch (error) {
    loadPayload(null);
    state.error = "暂时无法读取游戏库数据。请稍后刷新，或确认 Actions 已成功生成公开数据。";
  } finally {
    state.loading = false;
    render();
  }
}

function gameKey(game) {
  return game.appid ? `app:${game.appid}` : `name:${game.name.toLowerCase()}`;
}

// 主库 + 各成员不可共享列表按 appid 去重，并聚合“成员不可用”标记。
function buildSearchIndex() {
  const index = new Map();
  const ensureEntry = game => {
    const key = gameKey(game);
    if (!index.has(key)) {
      index.set(key, {
        game,
        inMainLibrary: false,
        unavailableMembers: [],
      });
    }
    return index.get(key);
  };

  state.games.forEach(game => {
    ensureEntry(game).inMainLibrary = true;
  });
  state.memberUnshared.forEach(group => {
    group.games.forEach(game => {
      if (game.playable) return;
      const entry = ensureEntry(game);
      if (!entry.unavailableMembers.includes(group.member)) {
        entry.unavailableMembers.push(group.member);
      }
    });
  });

  return [...index.values()].map(entry => ({
    ...entry.game,
    inMainLibrary: entry.inMainLibrary,
    unavailableMembers: entry.unavailableMembers,
  }));
}

function libraryDate(game) {
  return game.acquired || game.firstSeenAt;
}

function formatPlaytime(minutes) {
  return `${(minutes / 60).toFixed(1)} 小时`;
}

function ownerText(game) {
  return game.owners.length ? game.owners.join("、") : "未知";
}

function formatLocalTime(date) {
  return date
    ? date.toLocaleString("zh-CN", {
        year: "numeric", month: "long", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      })
    : "未知";
}

function formatStaleDate(date) {
  return date.toLocaleDateString("zh-CN", {
    year: "numeric", month: "long", day: "numeric",
  });
}

function explanationFor(reason) {
  return REASON_EXPLANATIONS[reason] ||
    `Steam 返回了不可共享原因“${reason || "未知"}”。当前成员无法通过家庭共享游玩这款游戏。`;
}

function isSearchActive() {
  return state.search.trim() !== "";
}

// 主库筛选保持不变；成员不可共享数据不参与主库列表和 NEW 标记。
function getFilteredGames() {
  const keyword = state.search.trim().toLowerCase();
  const filtered = state.games.filter(game => {
    if (keyword && !game.name.toLowerCase().includes(keyword)) return false;
    if (state.selectedOwners.size &&
        !game.owners.some(owner => state.selectedOwners.has(owner))) return false;
    if (state.playableOnly && !game.playable) return false;
    if (state.newOnly && !game.isNew) return false;
    return true;
  });
  return sortGames(filtered, state.sortBy);
}

// 统一搜索：只存在于成员不可共享列表中的游戏也可命中；按 appid 去重。
function getSearchResults() {
  const keyword = state.search.trim().toLowerCase();
  const filtered = state.searchIndex.filter(game => {
    if (!game.name.toLowerCase().includes(keyword)) return false;
    if (state.selectedOwners.size &&
        !game.owners.some(owner => state.selectedOwners.has(owner))) return false;
    if (state.newOnly && !game.isNew) return false;
    // “只看可玩”在统一搜索中同样生效；想搜索不可共享游戏时需要手动关闭该开关。
    if (state.playableOnly && !game.playable) return false;
    return true;
  });
  return sortGames(filtered, state.sortBy);
}

function sortGames(games, sortBy) {
  return [...games].sort((a, b) => {
    if (sortBy === "name") {
      return a.name.localeCompare(b.name, "zh-CN", { numeric: true, sensitivity: "base" });
    }
    if (sortBy === "playtime") {
      return b.playtimeMinutes - a.playtimeMinutes ||
        a.name.localeCompare(b.name, "zh-CN", { numeric: true });
    }
    return libraryDate(b).localeCompare(libraryDate(a)) ||
      a.name.localeCompare(b.name, "zh-CN", { numeric: true });
  });
}

// 根据容器真实宽度和 CSS 变量计算第一行卡片数，不写死某宽度下的数量。
function firstRowCount(container, variableName) {
  const rootStyle = getComputedStyle(document.documentElement);
  const cardWidth = Number.parseFloat(rootStyle.getPropertyValue(variableName)) || 150;
  const gap = Number.parseFloat(rootStyle.getPropertyValue("--space-3")) || 12;
  const width = container.clientWidth || container.parentElement?.clientWidth || cardWidth;
  return Math.max(1, Math.floor((width + gap) / (cardWidth + gap)));
}

function visibleByExpansion(games, expanded, container, variableName) {
  if (expanded) return games;
  return games.slice(0, firstRowCount(container, variableName));
}

function updateExpandButton(button, hidden, expanded, totalCount) {
  button.hidden = hidden;
  button.setAttribute("aria-expanded", String(expanded));
  button.textContent = expanded ? "收起" : "展开全部";
  button.title = totalCount ? `共 ${totalCount} 款` : "";
}

// 竖版海报失败时切换横版图；两张图都失败则显示游戏名占位。
function createGameImage(game) {
  const frame = document.createElement("div");
  frame.className = "image-frame";

  const showPlaceholder = () => {
    const placeholder = document.createElement("div");
    placeholder.className = "image-placeholder";
    placeholder.textContent = game.name;
    frame.replaceChildren(placeholder);
  };

  if (!game.appid) {
    showPlaceholder();
    return frame;
  }

  const image = document.createElement("img");
  image.loading = "lazy";
  image.decoding = "async";
  image.alt = `${game.name} 海报`;
  image.src = `${IMAGE_BASE_URL}${game.appid}/library_600x900_2x.jpg`;
  image.addEventListener("error", () => {
    if (!image.dataset.triedHeader) {
      image.dataset.triedHeader = "true";
      image.src = `${IMAGE_BASE_URL}${game.appid}/header.jpg`;
    } else {
      showPlaceholder();
    }
  });
  frame.appendChild(image);
  return frame;
}

function appendNewBadge(container) {
  const badge = document.createElement("span");
  badge.className = "new-badge";
  badge.textContent = "NEW";
  container.appendChild(badge);
}

function providerStatusLines(game) {
  if (!game.owners.length) return [{ text: "未知提供者", closed: false }];
  // 只有 Steam 明确返回 excluded by owner，且该拥有者出现在成员不可用列表中，才标注“已关闭共享”。
  // 地区、DRM、发行商限制等原因不能错误归因给拥有者。
  const ownerExcluded = game.excludeReason === "excluded by owner";
  const unavailable = new Set(game.unavailableMembers || []);
  return game.owners.map(owner => {
    const closed = ownerExcluded && unavailable.has(owner);
    return {
      text: closed ? `${owner}已关闭这款游戏的共享` : `项目由${owner}提供`,
      closed,
    };
  });
}

function createProviderSummary(game) {
  const fragment = document.createDocumentFragment();
  const lines = providerStatusLines(game);
  lines.forEach(line => {
    const item = document.createElement("span");
    item.className = line.closed ? "provider-line closed" : "provider-line";
    item.textContent = line.text;
    fragment.appendChild(item);
  });
  return fragment;
}

// 海报左下角份数按钮：悬停出桌面悬浮框，点击出移动端卡片内提供者区域。
function createProviderControl(game) {
  const key = gameKey(game);
  const control = document.createElement("button");
  control.type = "button";
  control.className = "provider-control";
  control.textContent = String(game.copyCount);
  control.setAttribute("aria-label", `查看 ${game.name} 的家庭库份数和提供者`);

  const popover = document.createElement("div");
  popover.className = "provider-popover";
  popover.setAttribute("role", "tooltip");
  const title = document.createElement("strong");
  title.textContent = `家庭库共有 ${game.copyCount} 份`;
  popover.append(title, createProviderSummary(game));

  control.addEventListener("click", event => {
    event.stopPropagation();
    const card = control.closest(".game-card");
    const willOpen = !state.openProviderCards.has(key);
    if (willOpen) state.openProviderCards.add(key);
    else state.openProviderCards.delete(key);
    card?.classList.toggle("providers-open", willOpen);
    control.setAttribute("aria-expanded", String(willOpen));
  });
  control.setAttribute("aria-expanded", String(state.openProviderCards.has(key)));

  return { control, popover };
}

function createReasonHelp(reason) {
  const help = document.createElement("button");
  help.type = "button";
  help.className = "reason-help";
  help.textContent = "?";
  help.dataset.reason = reason || "未知";
  help.setAttribute("aria-label", `查看不可共享原因说明：${reason || "未知"}`);
  const tip = document.createElement("span");
  tip.className = "reason-tip";
  tip.setAttribute("role", "tooltip");
  tip.textContent = explanationFor(reason);
  help.appendChild(tip);
  help.addEventListener("click", event => {
    event.stopPropagation();
    openReasonPopover(reason);
  });
  return help;
}

function createReasonElement(reason) {
  const wrapper = document.createElement("span");
  wrapper.className = "reason-text";
  const text = document.createElement("span");
  text.textContent = reason || "当前不可玩";
  wrapper.append(text, createReasonHelp(reason));
  return wrapper;
}

function bindActivation(element, handler) {
  element.addEventListener("click", event => {
    if (event.target.closest("button, a, details, summary")) return;
    handler();
  });
  element.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (event.target.closest("button, a, details, summary")) return;
    event.preventDefault();
    handler();
  });
}

function createGameCard(game, extraClass = "") {
  const card = document.createElement("article");
  card.className = `game-card${game.playable ? "" : " unplayable"}${extraClass ? ` ${extraClass}` : ""}`;
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.setAttribute("aria-label", `${game.name}，拥有者：${ownerText(game)}`);
  if (state.openProviderCards.has(gameKey(game))) card.classList.add("providers-open");

  const poster = document.createElement("div");
  poster.className = "poster-frame";
  const image = createGameImage(game);
  if (game.isNew) appendNewBadge(image);
  const provider = createProviderControl(game);
  poster.append(image, provider.control, provider.popover);
  card.appendChild(poster);

  const providerPanel = document.createElement("div");
  providerPanel.className = "provider-panel";
  const panelTitle = document.createElement("strong");
  panelTitle.textContent = `家庭库共有 ${game.copyCount} 份`;
  providerPanel.append(panelTitle, createProviderSummary(game));
  card.appendChild(providerPanel);

  const info = document.createElement("div");
  info.className = "card-info";
  const name = document.createElement("div");
  name.className = "game-name";
  name.textContent = game.name;
  info.append(name);
  if (!game.playable) {
    const reason = document.createElement("div");
    reason.className = "game-reason";
    reason.appendChild(createReasonElement(game.excludeReason));
    info.appendChild(reason);
  }
  card.appendChild(info);
  bindActivation(card, () => openGameModal(game));
  return card;
}

function createListRow(game) {
  const row = document.createElement("div");
  row.className = `list-row${game.playable ? "" : " unplayable"}`;
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", `${game.name}，拥有者：${ownerText(game)}`);

  row.appendChild(createGameImage(game));
  const main = document.createElement("div");
  main.className = "list-main";
  const name = document.createElement("div");
  name.className = "game-name";
  name.textContent = game.name;
  const meta = document.createElement("div");
  meta.className = "list-meta";
  const values = [
    `家庭库 ${game.copyCount} 份`,
    ownerText(game),
    libraryDate(game) || "入库日期未知",
    formatPlaytime(game.playtimeMinutes),
  ];
  if (game.isNew) values.unshift("NEW");
  if (!game.playable) values.push(game.excludeReason || "当前不可玩");
  values.forEach(value => {
    const item = document.createElement("span");
    item.textContent = value;
    meta.appendChild(item);
  });
  main.append(name, meta);
  row.appendChild(main);
  bindActivation(row, () => openGameModal(game));
  return row;
}

function createSearchResultCard(game) {
  const card = document.createElement("article");
  card.className = `search-result-card${game.playable ? "" : " unplayable"}`;

  card.appendChild(createGameImage(game));
  const main = document.createElement("div");
  main.className = "search-result-main";

  const title = document.createElement("div");
  title.className = "search-result-title";
  title.textContent = game.name;
  const copy = document.createElement("div");
  copy.className = "copy-summary";
  copy.textContent = `家庭库共有 ${game.copyCount} 份`;
  const providers = document.createElement("div");
  providers.className = "provider-list";
  providers.appendChild(createProviderSummary(game));
  main.append(title, copy, providers);

  if (game.unavailableMembers.length) {
    const flags = document.createElement("div");
    flags.className = "member-flags";
    game.unavailableMembers.forEach(member => {
      const flag = document.createElement("span");
      flag.className = "member-flag";
      flag.textContent = `${member}的库存游戏暂时不可共享`;
      flags.appendChild(flag);
    });
    main.appendChild(flags);
  }
  if (!game.playable) {
    const reason = document.createElement("div");
    reason.appendChild(createReasonElement(game.excludeReason));
    main.appendChild(reason);
  }
  card.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "result-actions";
  const store = document.createElement("a");
  store.className = "secondary-button";
  store.textContent = "Steam 商店";
  store.target = "_blank";
  store.rel = "noopener noreferrer";
  const xiaoheihe = document.createElement("a");
  xiaoheihe.className = "secondary-button";
  xiaoheihe.textContent = "小黑盒";
  xiaoheihe.target = "_blank";
  xiaoheihe.rel = "noopener noreferrer";
  const details = document.createElement("button");
  details.type = "button";
  details.className = "details-button";
  details.textContent = "详情";
  details.addEventListener("click", () => openGameModal(game));
  if (game.appid) {
    store.href = `https://store.steampowered.com/app/${game.appid}`;
    xiaoheihe.href = `https://api.xiaoheihe.cn/game/share_game_detail?appid=${game.appid}&game_type=pc`;
  } else {
    store.hidden = true;
    xiaoheihe.hidden = true;
  }
  actions.append(store, xiaoheihe, details);
  card.appendChild(actions);
  return card;
}

function renderSummary() {
  elements["total-count"].textContent = state.loading ? "—" : state.total;
  elements["main-count-note"].textContent = state.loading
    ? "主库 — 款" : `主库 ${state.mainCount} 款`;
  elements["new-count"].textContent = state.loading
    ? "—" : state.games.filter(game => game.isNew).length;
  elements["unplayable-count"].textContent = state.loading ? "—" : state.unshareableCount;
  elements["member-panel-badge"].textContent = state.loading ? "0" : state.unshareableCount;

  elements["updated-at"].textContent = state.updatedAt
    ? `主库更新于 ${formatLocalTime(state.updatedAt)}${state.isMock ? " · 模拟数据" : ""}`
    : "主库更新时间未知";

  const isStale = state.updatedAt &&
    (Date.now() - state.updatedAt.getTime()) > STALE_HOURS * 60 * 60 * 1000;
  elements["stale-alert"].hidden = !isStale;
  if (isStale) {
    elements["stale-alert"].textContent =
      `数据停留在 ${formatStaleDate(state.updatedAt)}，可能尚未更新 Steam Token。`;
  }
  elements["load-error"].hidden = !state.error;
  elements["load-error"].textContent = state.error;
}

function renderNewGames() {
  const games = sortGames(state.games.filter(game => game.isNew), "recent");
  const columns = firstRowCount(elements["new-games"], "--new-card-width");
  const visible = visibleByExpansion(games, state.newExpanded, elements["new-games"], "--new-card-width");
  elements["new-section-count"].textContent = state.loading ? "" : `${games.length} 款`;
  elements["new-empty"].hidden = state.loading || games.length > 0;
  updateExpandButton(
    elements["toggle-new"],
    state.loading || state.error || games.length <= columns,
    state.newExpanded,
    games.length
  );
  const fragment = document.createDocumentFragment();
  visible.forEach(game => fragment.appendChild(createGameCard(game)));
  elements["new-games"].replaceChildren(fragment);
}

// 仅在拥有者集合变化时重建选项，避免筛选时反复打断下拉操作。
function renderOwnerFilter() {
  const owners = [...new Set(state.games.flatMap(game => game.owners))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
  const signature = owners.join("\u0000");
  elements["owner-filter-count"].textContent = state.selectedOwners.size
    ? `已选 ${state.selectedOwners.size}` : "全部";

  if (signature === state.ownerSignature) return;
  state.ownerSignature = signature;
  const fragment = document.createDocumentFragment();
  owners.forEach(owner => {
    const label = document.createElement("label");
    label.className = "owner-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedOwners.has(owner);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedOwners.add(owner);
      else state.selectedOwners.delete(owner);
      render();
    });
    label.append(checkbox, document.createTextNode(owner));
    fragment.appendChild(label);
  });
  elements["owner-options"].replaceChildren(fragment);
  elements["owner-filter"].hidden = owners.length === 0;
}

function renderViewSwitch() {
  document.querySelectorAll("[data-view]").forEach(button => {
    const active = button.dataset.view === state.view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function renderGames() {
  elements.loading.hidden = !state.loading;
  const hasGames = state.games.length > 0 || state.searchIndex.length > 0;
  elements["random-button"].disabled = state.loading || Boolean(state.error) || !hasGames;

  if (state.loading || state.error) {
    elements["result-count"].textContent = "";
    elements.games.className = "games-grid";
    elements.games.replaceChildren();
    elements["empty-results"].hidden = true;
    updateExpandButton(elements["toggle-library"], true, false, 0);
    return;
  }

  if (isSearchActive()) {
    const results = getSearchResults();
    elements["result-count"].textContent = `搜索到 ${results.length} 款`;
    elements.games.className = "search-results";
    const fragment = document.createDocumentFragment();
    results.forEach(game => fragment.appendChild(createSearchResultCard(game)));
    elements.games.replaceChildren(fragment);
    elements["empty-results"].hidden = results.length > 0;
    updateExpandButton(elements["toggle-library"], true, false, results.length);
    return;
  }

  const filtered = getFilteredGames();
  const columns = firstRowCount(elements.games, "--card-width");
  const limited = !state.libraryExpanded && state.view !== "list";
  const visible = limited ? filtered.slice(0, columns) : filtered;
  elements["result-count"].textContent = `显示 ${visible.length} / ${filtered.length} 款`;
  elements.games.className = state.view === "list" ? "games-list" : "games-grid";
  const fragment = document.createDocumentFragment();
  visible.forEach(game => {
    fragment.appendChild(state.view === "list" ? createListRow(game) : createGameCard(game));
  });
  elements.games.replaceChildren(fragment);
  elements["empty-results"].hidden = filtered.length > 0;
  updateExpandButton(
    elements["toggle-library"],
    state.view === "list" || filtered.length <= columns,
    state.libraryExpanded,
    filtered.length
  );
}

function renderMemberGroups() {
  const groups = state.memberUnshared;
  elements["member-empty"].hidden = state.loading || groups.length > 0;
  const fragment = document.createDocumentFragment();
  groups.forEach(group => {
    const details = document.createElement("details");
    details.className = "member-group";
    const key = group.member;
    if (state.openMemberGroups.has(key)) details.open = true;

    const summary = document.createElement("summary");
    const name = document.createElement("span");
    name.className = "member-name";
    name.textContent = group.member;
    const count = document.createElement("span");
    count.className = "member-count";
    count.textContent = `${group.games.length} 款`;
    const meta = document.createElement("span");
    meta.className = "member-meta";
    meta.textContent = `上次成功更新：${formatLocalTime(group.updatedAt)}`;
    summary.append(name, count, meta);
    details.appendChild(summary);

    const list = document.createElement("div");
    list.className = "member-game-list";
    group.games.forEach(game => {
      const item = document.createElement("article");
      item.className = "member-game";
      item.appendChild(createGameImage(game));
      const info = document.createElement("div");
      info.className = "member-game-info";
      const nameEl = document.createElement("div");
      nameEl.className = "member-game-name";
      nameEl.textContent = game.name;
      const owners = document.createElement("div");
      owners.className = "member-game-owners";
      owners.textContent = `实际拥有者：${ownerText(game)}`;
      const reason = document.createElement("div");
      reason.appendChild(createReasonElement(game.excludeReason));
      info.append(nameEl, owners, reason);
      item.appendChild(info);
      list.appendChild(item);
    });
    details.appendChild(list);
    details.addEventListener("toggle", () => {
      if (details.open) state.openMemberGroups.add(key);
      else state.openMemberGroups.delete(key);
    });
    fragment.appendChild(details);
  });
  elements["member-groups"].replaceChildren(fragment);
}

// 所有界面更新统一经过 render()。
function render() {
  renderSummary();
  renderNewGames();
  renderOwnerFilter();
  renderViewSwitch();
  renderGames();
  renderMemberGroups();
}

// 打开游戏详情弹窗，并锁定背景滚动。
function openGameModal(game) {
  if (elements["game-modal"].hidden) {
    state.lastFocusedElement = document.activeElement;
  }
  state.currentGame = game;
  elements["modal-poster"].replaceChildren(createGameImage(game));
  elements["modal-title"].textContent = game.name;
  elements["modal-owners"].textContent = ownerText(game);
  elements["modal-acquired"].textContent = libraryDate(game) || "未知";
  elements["modal-playtime"].textContent = formatPlaytime(game.playtimeMinutes);

  const copy = elements["modal-copy"];
  const copyTitle = document.createElement("strong");
  copyTitle.textContent = `家庭库共有 ${game.copyCount} 份`;
  copy.replaceChildren(copyTitle, createProviderSummary(game));

  const reason = game.excludeReason || (!game.playable ? "当前不可玩" : "");
  elements["modal-reason"].hidden = !reason;
  elements["modal-reason"].replaceChildren();
  if (reason) {
    elements["modal-reason"].appendChild(createReasonElement(reason));
  }

  const storeLink = elements["store-link"];
  const xiaoheiheLink = elements["xiaoheihe-link"];
  storeLink.hidden = !game.appid;
  xiaoheiheLink.hidden = !game.appid;
  if (game.appid) {
    storeLink.href = `https://store.steampowered.com/app/${game.appid}`;
    xiaoheiheLink.href =
      `https://api.xiaoheihe.cn/game/share_game_detail?appid=${game.appid}&game_type=pc`;
  }

  elements["game-modal"].hidden = false;
  document.body.classList.add("modal-open");
  elements["game-modal"].querySelector(".modal-panel").scrollTop = 0;
  elements["close-modal"].focus();
}

function closeGameModal() {
  if (elements["game-modal"].hidden) return;
  elements["game-modal"].hidden = true;
  if (!state.memberDrawerOpen) document.body.classList.remove("modal-open");
  state.currentGame = null;
  if (state.lastFocusedElement instanceof HTMLElement) {
    state.lastFocusedElement.focus();
  }
}

function openMemberDrawer() {
  renderMemberGroups();
  state.memberDrawerOpen = true;
  elements["member-drawer-backdrop"].hidden = false;
  elements["member-drawer"].hidden = false;
  document.body.classList.add("modal-open");
  elements["close-member-drawer"].focus();
}

function closeMemberDrawer() {
  if (!state.memberDrawerOpen) return;
  state.memberDrawerOpen = false;
  elements["member-drawer-backdrop"].hidden = true;
  elements["member-drawer"].hidden = true;
  if (elements["game-modal"].hidden) document.body.classList.remove("modal-open");
  elements["member-panel-button"].focus();
}

function openReasonPopover(reason) {
  elements["reason-popover-text"].textContent = explanationFor(reason);
  elements["reason-backdrop"].hidden = false;
  elements["close-reason"].focus();
}

function closeReasonPopover() {
  elements["reason-backdrop"].hidden = true;
}

function hasCustomFilters() {
  return Boolean(
    state.search.trim() || state.selectedOwners.size || state.newOnly || !state.playableOnly
  );
}

function getVisibleGamePool() {
  return isSearchActive() ? getSearchResults() : getFilteredGames();
}

// 有自定义筛选或搜索时从当前结果抽取；默认状态下只抽取可玩且未游玩的游戏。
function getRandomCandidates() {
  return hasCustomFilters()
    ? getVisibleGamePool()
    : state.games.filter(game => game.playable && game.playtimeMinutes === 0);
}

function pickRandomGame() {
  const candidates = getRandomCandidates();
  if (!candidates.length) {
    showToast("当前没有可抽取的游戏，试试调整筛选条件。");
    return;
  }
  let pool = candidates;
  if (state.currentGame && candidates.length > 1) {
    pool = candidates.filter(game => gameKey(game) !== gameKey(state.currentGame));
  }
  if (candidates.length === 1 && state.currentGame) {
    showToast("当前只有这一款候选游戏。");
    return;
  }
  const game = pool[Math.floor(Math.random() * pool.length)];
  openGameModal(game);
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 3000);
}

function resetFilters() {
  state.search = "";
  state.selectedOwners.clear();
  state.playableOnly = true;
  state.newOnly = false;
  state.sortBy = "recent";
  elements["search-input"].value = "";
  elements["playable-only"].checked = true;
  elements["new-only"].checked = false;
  elements["sort-select"].value = "recent";
  elements["owner-options"].querySelectorAll("input").forEach(input => {
    input.checked = false;
  });
  render();
}

function bindEvents() {
  elements["search-input"].addEventListener("input", event => {
    state.search = event.target.value;
    render();
  });
  elements["playable-only"].addEventListener("change", event => {
    state.playableOnly = event.target.checked;
    render();
  });
  elements["new-only"].addEventListener("change", event => {
    state.newOnly = event.target.checked;
    render();
  });
  elements["sort-select"].addEventListener("change", event => {
    state.sortBy = event.target.value;
    render();
  });
  document.querySelectorAll("[data-view]").forEach(button => {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      render();
    });
  });
  elements["toggle-new"].addEventListener("click", () => {
    state.newExpanded = !state.newExpanded;
    render();
  });
  elements["toggle-library"].addEventListener("click", () => {
    state.libraryExpanded = !state.libraryExpanded;
    render();
  });
  elements["clear-owners"].addEventListener("click", () => {
    state.selectedOwners.clear();
    elements["owner-options"].querySelectorAll("input").forEach(input => {
      input.checked = false;
    });
    render();
  });
  elements["reset-filters"].addEventListener("click", resetFilters);
  elements["random-button"].addEventListener("click", pickRandomGame);
  elements["reroll-button"].addEventListener("click", pickRandomGame);
  elements["close-modal"].addEventListener("click", closeGameModal);
  elements["game-modal"].addEventListener("click", event => {
    if (event.target === elements["game-modal"]) closeGameModal();
  });
  elements["member-panel-button"].addEventListener("click", openMemberDrawer);
  elements["close-member-drawer"].addEventListener("click", closeMemberDrawer);
  elements["member-drawer-backdrop"].addEventListener("click", event => {
    if (event.target === elements["member-drawer-backdrop"]) closeMemberDrawer();
  });
  elements["close-reason"].addEventListener("click", closeReasonPopover);
  elements["reason-backdrop"].addEventListener("click", event => {
    if (event.target === elements["reason-backdrop"]) closeReasonPopover();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (!elements["reason-backdrop"].hidden) closeReasonPopover();
    else if (state.memberDrawerOpen) closeMemberDrawer();
    else closeGameModal();
  });
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(render, 120);
  });
}

cacheElements();
bindEvents();
loadData();
