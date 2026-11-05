#!/usr/bin/env python3
"""
云端更新入口（GitHub Actions 调用）。

从环境变量读取 STEAM_WEBAPI_TOKEN / MEMBERS_JSON，
复用 app.py 中的抓取逻辑（fetch_library / parse_jwt / 错误分类），
生成脱敏的 data/public_library.json 作为云端历史快照，
并复制到 site/data/ 供 GitHub Pages 读取。

规则：
- token 过期或 401/403：正常结束（退出码 0），不修改数据，不部署；
- 其他网络错误 / 返回 0 款游戏：退出码 1，保留旧数据；
- 数据与上次相同时不覆写文件（updated_at 保持不变，避免空提交）。

日志只输出总数、新增数等聚合信息，绝不输出 Token / SteamID / 家庭组 ID。
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "data" / "public_library.json"
SITE_DATA_PATH = BASE_DIR / "site" / "data" / "public_library.json"

sys.path.insert(0, str(BASE_DIR))

from app import fetch_library, parse_jwt, FetchError, TokenExpiredError  # noqa: E402

NEW_WINDOW_DAYS = 30
MIN_REMAINING_SECONDS = 600  # 距过期不足 10 分钟视为不可用，直接跳过

# 本地 config.json 的 include 默认值；云端默认不包含免费游戏/非游戏
CLOUD_INCLUDE_FREE = False
CLOUD_INCLUDE_NON_GAMES = False

STEAMID64_RE = re.compile(r"7656119\d{10}")


def log(msg):
    print(msg, flush=True)


def finish(result, code=0):
    """通过 GITHUB_OUTPUT 通知 workflow 本次运行结果，然后退出。"""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"result={result}\n")
    sys.exit(code)


def load_members():
    raw = os.environ.get("MEMBERS_JSON", "").strip()
    if not raw:
        return {}
    try:
        members = json.loads(raw)
        if not isinstance(members, dict):
            raise ValueError("MEMBERS_JSON 必须是 JSON 对象")
        return {str(k): str(v) for k, v in members.items()}
    except (json.JSONDecodeError, ValueError) as e:
        log(f"错误：MEMBERS_JSON 解析失败（{e}），请检查 Secrets 格式。")
        finish("error", 1)


def load_old_games():
    """读取上一次快照，返回 {appid: entry}。文件不存在视为首次运行。"""
    if not DATA_PATH.exists():
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        return {g["appid"]: g for g in old.get("games", [])}
    except (json.JSONDecodeError, KeyError, OSError) as e:
        log(f"错误：读取上一次快照失败（{e}），为避免历史丢失已中止。")
        finish("error", 1)


def mask_owner(steamid, members):
    """SteamID -> 昵称；未映射显示为「成员 XXXX」（ID 后 4 位）。"""
    return members.get(steamid) or f"成员 {steamid[-4:]}"


def build_public_library(data, members, old_by_appid):
    """把 fetch_library 的结果转成脱敏公开结构。

    与本地模式保持一致的判定规则：
    - first_seen_at：appid 已存在则保留旧值，否则记为今天（UTC）；
    - is_new：acquired 非空优先用 acquired，否则用 first_seen_at，30 天窗口。
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=NEW_WINDOW_DAYS)).date()

    appeared = set()
    games = []
    for g in data["games"]:
        appid = g["appid"]
        owners = [mask_owner(s, members) for s in g["owner_steamids"]]
        old = old_by_appid.get(appid)
        if old:
            first_seen = old.get("first_seen_at") or today
        else:
            first_seen = today
            appeared.add(appid)
        acquired = g["acquired"] or ""
        ref_date = acquired or first_seen
        try:
            is_new = bool(ref_date) and \
                datetime.strptime(ref_date, "%Y-%m-%d").date() >= cutoff
        except ValueError:
            is_new = False
        games.append({
            "appid": appid,
            "name": g["name"],
            "owners": owners,
            "acquired": acquired,
            "playtime_minutes": g["playtime_minutes"],
            "playable": g["playable"],
            "exclude_reason": g["exclude_reason"],
            "first_seen_at": first_seen,
            "is_new": is_new,
        })

    games.sort(key=lambda g: g["name"].lower())
    return {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        "count": len(games),
        "games": games,
    }, appeared


def check_no_leak(payload, token):
    """脱敏自检：输出中不得出现 token、完整 SteamID（7656119…）。"""
    text = json.dumps(payload, ensure_ascii=False)
    problems = []
    if token and token in text:
        problems.append("token")
    if STEAMID64_RE.search(text):
        problems.append("SteamID64")
    if problems:
        log(f"错误：脱敏校验失败，输出中包含敏感信息：{'、'.join(problems)}。已中止，未写入文件。")
        finish("error", 1)


def main():
    token = os.environ.get("STEAM_WEBAPI_TOKEN", "").strip().strip("\"'")
    if not token:
        log("错误：缺少 Secrets STEAM_WEBAPI_TOKEN。")
        finish("error", 1)

    # 规则 1：请求前检查 JWT exp
    _, exp, status = parse_jwt(token)
    if status == "invalid":
        log("错误：STEAM_WEBAPI_TOKEN 不是有效的 JWT，请确认 Secret 复制完整。")
        finish("error", 1)
    if exp is not None:
        remaining = exp - datetime.now(tz=timezone.utc).timestamp()
        if remaining < MIN_REMAINING_SECONDS:
            exp_str = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            log(f"跳过本次更新：token 已过期或即将过期（有效期至 {exp_str}，不足 10 分钟）。")
            log("旧数据保留，网站不受影响。请更新 Secrets 中的 STEAM_WEBAPI_TOKEN 后手动触发。")
            finish("skipped", 0)

    # 规则 2/3：抓取；401/403 视为 token 失效，正常跳过
    try:
        data = fetch_library(token, CLOUD_INCLUDE_FREE, CLOUD_INCLUDE_NON_GAMES)
    except TokenExpiredError:
        log("跳过本次更新：Steam API 返回 401/403，token 已失效。旧数据保留。")
        log("请更新 Secrets 中的 STEAM_WEBAPI_TOKEN 后手动触发。")
        finish("skipped", 0)
    except FetchError as e:
        log(f"错误：抓取失败（{e.type}）：{e.message}")
        log("旧数据保留。可稍后重试或在 Actions 页面查看详细日志。")
        finish("error", 1)
    except (TimeoutError, OSError) as e:
        # urllib 低层超时/连接错误不一定包装成 URLError，统一按网络错误处理
        log(f"错误：网络异常（{type(e).__name__}）：{e}")
        log("旧数据保留，workflow 标红便于排查。")
        finish("error", 1)

    # 规则 4：空列表视为异常
    if not data["games"]:
        log("错误：API 返回 0 款游戏，视为异常，不覆盖旧数据。")
        finish("error", 1)

    members = load_members()
    old_by_appid = load_old_games()
    payload, appeared = build_public_library(data, members, old_by_appid)

    # 规则：与上次完全一致时不覆写（updated_at 也不动），避免空提交
    if old_by_appid and len(payload["games"]) == len(old_by_appid) and \
            all(old_by_appid.get(g["appid"]) == g for g in payload["games"]):
        log(f"数据无变化（共 {payload['count']} 款），跳过写入与部署。")
        finish("unchanged", 0)

    check_no_leak(payload, token)

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SITE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    DATA_PATH.write_text(text, encoding="utf-8")
    SITE_DATA_PATH.write_text(text, encoding="utf-8")

    new_30d = sum(1 for g in payload["games"] if g["is_new"])
    log(f"更新完成：共 {payload['count']} 款，本次新出现 {len(appeared)} 款，"
        f"最近 30 天新入库 {new_30d} 款。")
    finish("updated", 0)


if __name__ == "__main__":
    main()
