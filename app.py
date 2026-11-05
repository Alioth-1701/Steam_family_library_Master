#!/usr/bin/env python3
"""
Steam 家庭游戏库本地预览看板 -- 后端。

基于 steam_family_library.py（MIT）改造：
- fetch_library() 复用原脚本的纯 urllib 抓取逻辑
- 抓取结果写入 SQLite（games upsert + snapshots + meta）
- FastAPI 提供 /api/library /api/refresh /api/token /api/health
- static/index.html 单页看板由 FastAPI 托管

Requires Python 3.8+. Dependencies: fastapi, uvicorn.
"""

import base64
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "library.db"
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"

API = "https://api.steampowered.com"

NEW_WINDOW_DAYS = 30

# Values Steam returns in the `exclude_reason` field. 0 means the game is
# shareable and playable by you right now.
EXCLUDE_REASONS = {
    0: "",
    1: "excluded by owner",
    2: "not eligible for sharing",
    3: "region or DRM restricted",
}

TOKEN_HELP_URL = "store.steampowered.com/pointssummary/ajaxgetasyncconfig"

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_config():
    if not CONFIG_PATH.exists():
        return {"token": "", "members": {}, "include_free": False,
                "include_non_games": False}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("token", "")
    cfg.setdefault("members", {})
    cfg.setdefault("include_free", False)
    cfg.setdefault("include_non_games", False)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    tmp.replace(CONFIG_PATH)


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """抓取失败（网络/服务端），数据未变更。"""

    def __init__(self, type_, message):
        super().__init__(message)
        self.type = type_
        self.message = message


class TokenExpiredError(FetchError):
    def __init__(self, message="token 已过期"):
        super().__init__("token_expired", message)


# ---------------------------------------------------------------------------
# JWT 解析
# ---------------------------------------------------------------------------


def parse_jwt(token):
    """返回 (steamid, exp, status)。status: ok / expired / invalid。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None, None, "invalid"
    try:
        claims_b64 = parts[1] + "=" * (-len(parts[1]) % 4)  # restore padding
        claims = json.loads(base64.urlsafe_b64decode(claims_b64))
    except Exception:
        return None, None, "invalid"
    steamid = claims.get("sub")
    exp = claims.get("exp")
    if exp and datetime.now(tz=timezone.utc).timestamp() > exp:
        return steamid, exp, "expired"
    if not steamid:
        return None, exp, "invalid"
    return steamid, exp, "ok"


def token_status(token):
    if not token:
        return "empty"
    _, _, status = parse_jwt(token)
    return status


# ---------------------------------------------------------------------------
# 抓取（复用原脚本逻辑，去掉 sys.exit）
# ---------------------------------------------------------------------------


def api_get(iface_method, params):
    """Call a Steam web API GET endpoint and return its `response` object."""
    url = f"{API}/{iface_method}/?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "steam-family-dashboard/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        if e.code in (401, 403):
            raise TokenExpiredError(
                f"Steam API 返回 HTTP {e.code}，token 已过期或无效。"
                f"请到 {TOKEN_HELP_URL} 重新获取 webapi_token。"
            )
        raise FetchError("http_error", f"{iface_method} 返回 HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise FetchError("network_error", f"无法连接 Steam API: {e.reason}")
    except (TimeoutError, OSError) as e:
        # urllib 低层读超时等不一定包装成 URLError
        raise FetchError("network_error", f"Steam API 请求异常: {e}")
    return payload.get("response", {})


def day(epoch):
    """Steam's rt_* fields are unix seconds. 0 means never."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_library(token, include_free, include_non_games):
    """抓取全家庭合并游戏库，返回与原脚本相同的 dict 结构。

    失败时抛 FetchError / TokenExpiredError，不产生任何副作用。
    """
    steamid, _, status = parse_jwt(token)
    if status == "invalid":
        raise FetchError("invalid_token", "token 不是有效的 JWT，请检查是否复制完整。")
    if status == "expired":
        raise TokenExpiredError("token 已过期")

    fam = api_get(
        "IFamilyGroupsService/GetFamilyGroupForUser/v1",
        {
            "access_token": token,
            "steamid": steamid,
            "include_family_group_response": "true",
        },
    )
    group_id = fam.get("family_groupid")
    if not group_id or fam.get("is_not_member_of_any_group"):
        raise FetchError("no_family", "该账号不在任何 Steam 家庭中，没有共享库可读。")

    group = fam.get("family_group", {})
    members = [m.get("steamid") for m in group.get("members", [])]

    lib = api_get(
        "IFamilyGroupsService/GetSharedLibraryApps/v1",
        {
            "access_token": token,
            "family_groupid": group_id,
            "include_own": "true",
            "include_excluded": "true",
            "include_free": str(bool(include_free)).lower(),
            "include_non_games": str(bool(include_non_games)).lower(),
            "language": "english",
        },
    )
    apps = lib.get("apps", [])
    if not apps:
        raise FetchError("empty_library", "API 返回 0 个应用，token 可能属于错误的账号。")

    me = str(steamid)
    games = []
    for a in apps:
        owners = [str(o) for o in a.get("owner_steamids", [])]
        reason = a.get("exclude_reason", 0)
        games.append({
            "appid": a.get("appid"),
            "name": a.get("name", ""),
            "source": "own" if me in owners else "family",
            "owned_by_me": me in owners,
            "owner_steamids": owners,
            "playable": reason == 0,
            "exclude_reason": EXCLUDE_REASONS.get(reason, f"unknown ({reason})"),
            "playtime_minutes": a.get("rt_playtime", 0),
            "acquired": day(a.get("rt_time_acquired")),
            "last_played": day(a.get("rt_last_played")),
            "store_url": f"https://store.steampowered.com/app/{a.get('appid')}",
        })

    games.sort(key=lambda g: g["name"].lower())
    return {
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "steamid": steamid,
        "family_groupid": group_id,
        "family_name": group.get("name", ""),
        "members": members,
        "count": len(games),
        "games": games,
    }


# ---------------------------------------------------------------------------
# SQLite 数据层
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    appid            INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    source           TEXT,
    owner_steamids   TEXT NOT NULL DEFAULT '[]',
    playable         INTEGER NOT NULL DEFAULT 0,
    exclude_reason   TEXT NOT NULL DEFAULT '',
    playtime_minutes INTEGER NOT NULL DEFAULT 0,
    acquired         TEXT NOT NULL DEFAULT '',
    last_played      TEXT NOT NULL DEFAULT '',
    first_seen_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def save_snapshot(conn, data):
    """写入一次快照：snapshots 行 + games upsert + meta。first_seen_at 只在首次插入。"""
    now = data["fetched_at"]
    cur = conn.execute("INSERT INTO snapshots(created_at) VALUES(?)", (now,))
    snapshot_id = cur.lastrowid
    for g in data["games"]:
        conn.execute(
            """INSERT INTO games(
                   appid, name, source, owner_steamids, playable,
                   exclude_reason, playtime_minutes, acquired, last_played,
                   first_seen_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(appid) DO UPDATE SET
                   name             = excluded.name,
                   source           = excluded.source,
                   owner_steamids   = excluded.owner_steamids,
                   playable         = excluded.playable,
                   exclude_reason   = excluded.exclude_reason,
                   playtime_minutes = excluded.playtime_minutes,
                   acquired         = excluded.acquired,
                   last_played      = excluded.last_played""",
            (
                g["appid"], g["name"], g["source"],
                json.dumps(g["owner_steamids"], ensure_ascii=False),
                1 if g["playable"] else 0,
                g["exclude_reason"],
                g["playtime_minutes"],
                g["acquired"],
                g["last_played"],
                now,  # first_seen_at，仅 INSERT 时生效
            ),
        )
    meta_set(conn, "fetched_at", now)
    meta_set(conn, "steamid", data["steamid"])
    meta_set(conn, "family_groupid", data["family_groupid"])
    meta_set(conn, "family_name", data["family_name"])
    meta_set(conn, "members", json.dumps(data["members"], ensure_ascii=False))
    meta_set(conn, "last_snapshot_id", snapshot_id)
    conn.commit()
    return snapshot_id


def load_library():
    """读取当前库全量 + meta，计算 is_new / owner_nicknames。"""
    cfg = load_config()
    nicknames = cfg.get("members", {})
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=NEW_WINDOW_DAYS)).date()

    conn = db_connect()
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        rows = conn.execute("SELECT * FROM games ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()

    games = []
    for r in rows:
        owners = json.loads(r["owner_steamids"] or "[]")
        acquired = r["acquired"] or ""
        first_seen = (r["first_seen_at"] or "")[:10]
        # 新入库判定：acquired 优先，否则用 first_seen_at
        ref_date = acquired or first_seen
        try:
            is_new = bool(ref_date) and \
                datetime.strptime(ref_date, "%Y-%m-%d").date() >= cutoff
        except ValueError:
            is_new = False
        games.append({
            "appid": r["appid"],
            "name": r["name"],
            "source": r["source"],
            "owned_by_me": str(meta.get("steamid", "")) in owners,
            "owner_steamids": owners,
            "owner_nicknames": [nicknames.get(o, o) for o in owners],
            "playable": bool(r["playable"]),
            "exclude_reason": r["exclude_reason"],
            "playtime_minutes": r["playtime_minutes"],
            "acquired": acquired,
            "first_seen_at": first_seen,
            "is_new": is_new,
            "new_date": ref_date,
            "store_url": f"https://store.steampowered.com/app/{r['appid']}",
            "poster_url": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{r['appid']}/library_600x900_2x.jpg",
            "header_url": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{r['appid']}/header.jpg",
        })

    members = []
    try:
        members = json.loads(meta.get("members", "[]"))
    except json.JSONDecodeError:
        pass
    unmapped = [m for m in members if str(m) not in nicknames]

    return {
        "meta": {
            "fetched_at": meta.get("fetched_at", ""),
            "steamid": meta.get("steamid", ""),
            "family_groupid": meta.get("family_groupid", ""),
            "family_name": meta.get("family_name", ""),
            "members": [str(m) for m in members],
            "members_unmapped": [str(m) for m in unmapped],
            "token_status": state["token_status"],
            "token_exp": state.get("token_exp"),
            "last_error": state.get("last_error"),
            "new_window_days": NEW_WINDOW_DAYS,
        },
        "games": games,
    }


# ---------------------------------------------------------------------------
# 全局状态 + 抓取流程
# ---------------------------------------------------------------------------

state = {
    "fetching": False,
    "token_status": "empty",  # empty / ok / expired / invalid
    "token_exp": None,
    "last_error": None,
}

_fetch_lock = threading.Lock()


def _refresh_locked():
    """持锁执行一次抓取。返回 (ok, payload)。"""
    cfg = load_config()
    token = cfg.get("token", "").strip().strip("\"'")
    state["token_status"] = token_status(token)
    if state["token_status"] == "empty":
        err = {"type": "no_token",
               "message": f"config.json 中没有 token。请到 {TOKEN_HELP_URL} 获取 webapi_token。"}
        state["last_error"] = err
        return False, err
    if state["token_status"] in ("expired", "invalid"):
        err = {
            "type": "token_expired" if state["token_status"] == "expired" else "invalid_token",
            "message": f"token 已{'过期' if state['token_status'] == 'expired' else '失效'}，"
                       f"请到 {TOKEN_HELP_URL} 重新获取。",
        }
        state["last_error"] = err
        return False, err

    try:
        data = fetch_library(token, cfg.get("include_free", False),
                             cfg.get("include_non_games", False))
    except FetchError as e:
        err = {"type": e.type, "message": e.message}
        state["last_error"] = err
        print(f"[fetch] 失败: {e.type}: {e.message}", file=sys.stderr)
        return False, err

    # 未映射 member 提醒
    members = [str(m) for m in data["members"]]
    nicknames = cfg.get("members", {})
    unmapped = [m for m in members if m not in nicknames]
    if unmapped:
        print(f"[members] 以下成员未在 config.json members 中映射昵称: "
              f"{', '.join(unmapped)}", file=sys.stderr)

    conn = db_connect()
    try:
        snapshot_id = save_snapshot(conn, data)
    finally:
        conn.close()

    _, exp, _ = parse_jwt(token)
    state["token_exp"] = exp
    state["last_error"] = None
    print(f"[fetch] 快照 #{snapshot_id}: {data['count']} 个游戏，"
          f"家庭「{data['family_name']}」", file=sys.stderr)
    return True, {"snapshot_id": snapshot_id, "count": data["count"],
                  "fetched_at": data["fetched_at"]}


def refresh():
    with _fetch_lock:
        state["fetching"] = True
        try:
            return _refresh_locked()
        finally:
            state["fetching"] = False


def background_refresh():
    ok, payload = refresh()
    if not ok:
        print(f"[startup] 初始抓取失败: {payload['message']}", file=sys.stderr)


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Steam 家庭游戏库看板")


@app.on_event("startup")
def startup():
    db_init()
    state["token_status"] = token_status(load_config().get("token", ""))
    if state["token_status"] == "expired":
        print(f"[token] 已过期，请到 {TOKEN_HELP_URL} 重新获取，"
              f"或在页面中直接输入新 token。", file=sys.stderr)
    elif state["token_status"] == "ok":
        # 有效 token：后台线程做一次初始抓取，不阻塞启动
        threading.Thread(target=background_refresh, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def envelope(ok, data=None, error=None):
    body = {"ok": ok}
    if data is not None:
        body["data"] = data
    if error is not None:
        body["error"] = error
    return JSONResponse(body)


@app.get("/api/health")
def api_health():
    conn = db_connect()
    try:
        game_count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    finally:
        conn.close()
    return envelope(True, {
        "fetching": state["fetching"],
        "token_status": state["token_status"],
        "token_exp": state["token_exp"],
        "game_count": game_count,
        "last_error": state["last_error"],
    })


@app.get("/api/library")
def api_library():
    return envelope(True, load_library())


@app.post("/api/refresh")
def api_refresh():
    if state["fetching"]:
        return envelope(False, error={"type": "busy", "message": "正在抓取中，请稍候。"})
    ok, payload = refresh()
    return envelope(ok, data=payload if ok else None,
                    error=None if ok else payload)


class TokenBody(BaseModel):
    token: str


@app.post("/api/token")
def api_token(body: TokenBody):
    token = body.token.strip().strip("\"'")
    steamid, exp, status = parse_jwt(token)
    if status == "invalid":
        return envelope(False, error={"type": "invalid_token",
                                      "message": "token 不是有效的 JWT，请检查是否复制完整。"})
    if status == "expired":
        return envelope(False, error={"type": "token_expired",
                                      "message": f"token 已过期，请到 {TOKEN_HELP_URL} 重新获取。"})
    cfg = load_config()
    cfg["token"] = token
    save_config(cfg)
    state["token_status"] = "ok"
    state["token_exp"] = exp
    print(f"[token] 已更新，属于 {steamid}", file=sys.stderr)
    ok, payload = refresh()
    return envelope(ok, data=payload if ok else None,
                    error=None if ok else payload)


def main():
    import uvicorn

    print("Steam 家庭游戏库看板 → http://127.0.0.1:8000  (Ctrl+C 停止)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
