#!/usr/bin/env python3
"""
云端更新入口（GitHub Actions 调用）。

从环境变量读取 STEAM_WEBAPI_TOKEN / MEMBERS_JSON / FAMILY_TOKENS_JSON，
复用 app.py 中的抓取逻辑（fetch_library / parse_jwt / 错误分类），
生成脱敏的 data/public_library.json 作为云端历史快照，
并复制到 site/data/ 供 GitHub Pages 读取。

规则：
- 主 token 过期或 401/403：正常结束（退出码 0），不修改数据，不部署；
- 主库网络错误 / 返回 0 款游戏：退出码 1，保留旧数据；
- 配置 FAMILY_TOKENS_JSON 时自动纳入主账号自己的不可共享结果，其他成员按 Secret 逐个抓取；
- 成员 token 独立处理：过期、401/403 或网络错误只跳过该成员，并保留其上次成功数据；
- 未配置 FAMILY_TOKENS_JSON 时 member_unshared 保持空数组；
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


def load_family_tokens():
    """读取可选的多成员 Token 映射：{"家人A":"token_a", ...}。"""
    raw = os.environ.get("FAMILY_TOKENS_JSON", "").strip()
    if not raw:
        return {}
    try:
        tokens = json.loads(raw)
        if not isinstance(tokens, dict):
            raise ValueError("FAMILY_TOKENS_JSON 必须是 JSON 对象")
        family_tokens = {}
        for member, token in tokens.items():
            member_name = str(member).strip()
            token_value = str(token).strip().strip("\"'")
            if not member_name or not token_value:
                raise ValueError("成员昵称和 token 都不能为空")
            family_tokens[member_name] = token_value
        return family_tokens
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log(f"错误：FAMILY_TOKENS_JSON 解析失败（{e}），请检查 Secrets 格式。")
        finish("error", 1)


def load_old_snapshot():
    """读取上一次完整快照；文件不存在视为首次运行。"""
    if not DATA_PATH.exists():
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        return old if isinstance(old, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log(f"错误：读取上一次快照失败（{e}），为避免历史丢失已中止。")
        finish("error", 1)


def old_games_by_appid(old_snapshot):
    games = old_snapshot.get("games", [])
    if not isinstance(games, list):
        return {}
    result = {}
    for game in games:
        if isinstance(game, dict) and "appid" in game:
            result[game["appid"]] = game
    return result


def old_members_by_name(old_snapshot):
    members = old_snapshot.get("member_unshared", [])
    if not isinstance(members, list):
        return {}
    return {
        entry.get("member"): entry
        for entry in members
        if isinstance(entry, dict) and entry.get("member")
    }


def mask_owner(steamid, members):
    """SteamID -> 昵称；未映射显示为「成员 XXXX」（ID 后 4 位）。"""
    return members.get(steamid) or f"成员 {steamid[-4:]}"


def unique_mapped_owners(owner_steamids, members):
    """先按 SteamID 去重，再映射昵称；昵称重复时公开展示仍保持唯一。"""
    return list(dict.fromkeys(mask_owner(str(s), members) for s in owner_steamids))


def copy_count_from_owners(owner_steamids):
    # copy_count 必须基于去重后的原始 SteamID 数量，不能基于映射后的昵称。
    return max(1, len(set(str(s) for s in owner_steamids)))


def public_game_entry(game, members):
    return {
        "appid": game["appid"],
        "name": game["name"],
        "owners": unique_mapped_owners(game["owner_steamids"], members),
        "copy_count": copy_count_from_owners(game["owner_steamids"]),
        "acquired": game["acquired"] or "",
        "playtime_minutes": game["playtime_minutes"],
        "playable": game["playable"],
        "exclude_reason": game["exclude_reason"],
    }


def build_public_library(data, members, old_by_appid):
    """把主 token 抓取结果转成脱敏公开结构。

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
        entry = public_game_entry(g, members)
        entry.update({
            "first_seen_at": first_seen,
            "is_new": is_new,
        })
        games.append(entry)

    games.sort(key=lambda g: g["name"].lower())
    return {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        "count": len(games),
        "games": games,
    }, appeared


def build_member_unshared(data, members):
    """提取某个成员共享结果中 playable=false 的游戏，保持成员—游戏记录。"""
    games = []
    for game in data["games"]:
        if game["playable"]:
            continue
        entry = public_game_entry(game, members)
        entry["playable"] = False
        games.append(entry)
    games.sort(key=lambda g: g["name"].lower())
    return {
        "member": None,  # 由调用方填充，便于复用构建函数
        "updated_at": data["fetched_at"],
        "games": games,
    }


def member_skip_reason(token):
    """请求前检查成员 token；返回中文原因，None 表示可用。"""
    _, exp, status = parse_jwt(token)
    if status == "invalid":
        return "不是有效的 JWT"
    if exp is not None:
        remaining = exp - datetime.now(tz=timezone.utc).timestamp()
        if remaining < MIN_REMAINING_SECONDS:
            exp_str = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return f"已过期或即将过期（有效期至 {exp_str}）"
    return None


def collect_member_unshared(family_tokens, members, old_by_member, main_member, main_steamid):
    """逐个额外成员抓取；失败的成员保留其上次成功数据，不影响其他成员。

    主账号已使用主 token 的结果，这里会跳过与主账号同昵称或同 SteamID 的重复配置。
    """
    results = []
    for member, token in family_tokens.items():
        token_steamid, _, _ = parse_jwt(token)
        if member == main_member or str(token_steamid) == str(main_steamid):
            continue
        reason = member_skip_reason(token)
        if reason:
            log(f"成员 {member}：跳过，{reason}；保留上次成功数据。")
            if member in old_by_member:
                results.append(old_by_member[member])
            continue
        try:
            data = fetch_library(token, CLOUD_INCLUDE_FREE, CLOUD_INCLUDE_NON_GAMES)
        except TokenExpiredError:
            log(f"成员 {member}：Steam API 返回 401/403；保留上次成功数据。")
        except FetchError as e:
            log(f"成员 {member}：抓取失败（{e.type}）；保留上次成功数据。")
        except (TimeoutError, OSError):
            log(f"成员 {member}：网络异常；保留上次成功数据。")
        else:
            entry = build_member_unshared(data, members)
            entry["member"] = member
            results.append(entry)
            log(f"成员 {member}：不可共享游戏 {len(entry['games'])} 款。")
            continue
        if member in old_by_member:
            results.append(old_by_member[member])
    return results


def library_union_count(games, member_unshared):
    """所有成员数据按 appid 去重；不能用每个家庭合并库的 count 累加。"""
    appids = set()
    for game in games:
        appids.add(game.get("appid") or f"name:{game.get('name', '')}")
    for member in member_unshared:
        for game in member.get("games", []):
            appids.add(game.get("appid") or f"name:{game.get('name', '')}")
    return len(appids)


def check_no_leak(payload, tokens):
    """脱敏自检：输出中不得出现任何 token、完整 SteamID（7656119…）。"""
    text = json.dumps(payload, ensure_ascii=False)
    problems = []
    if any(token and token in text for token in tokens):
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

    # 规则 1：请求前检查主 JWT exp
    steamid, exp, status = parse_jwt(token)
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

    # 规则 2/3：主库抓取；401/403 视为 token 失效，正常跳过
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

    # 规则 4：主库空列表视为异常
    if not data["games"]:
        log("错误：API 返回 0 款游戏，视为异常，不覆盖旧数据。")
        finish("error", 1)

    members = load_members()
    family_tokens_configured = bool(os.environ.get("FAMILY_TOKENS_JSON", "").strip())
    family_tokens = load_family_tokens()
    old_snapshot = load_old_snapshot()
    old_by_appid = old_games_by_appid(old_snapshot)

    main_payload, appeared = build_public_library(data, members, old_by_appid)

    # 多成员功能启用时，主账号自己的不可共享结果也纳入；未配置时保持旧版空数组。
    member_unshared = []
    if family_tokens_configured:
        main_member = members.get(str(steamid)) or f"成员 {str(steamid)[-4:]}"
        main_entry = build_member_unshared(data, members)
        main_entry["member"] = main_member
        member_unshared.append(main_entry)
        log(f"成员 {main_member}：不可共享游戏 {len(main_entry['games'])} 款。")
        member_unshared.extend(collect_member_unshared(
            family_tokens, members, old_members_by_name(old_snapshot),
            main_member, steamid,
        ))

    payload = {
        **main_payload,
        "library_total_count": library_union_count(main_payload["games"], member_unshared),
        "unshareable_total_count": sum(len(entry.get("games", [])) for entry in member_unshared),
        "member_unshared": member_unshared,
    }

    # 规则：除 updated_at 外与上次完全一致时不覆写，避免空提交。
    comparable_payload = {k: v for k, v in payload.items() if k != "updated_at"}
    comparable_old = {k: v for k, v in old_snapshot.items() if k != "updated_at"}
    if old_snapshot and comparable_payload == comparable_old:
        log(f"数据无变化（主库 {payload['count']} 款，成员不可共享记录 "
            f"{payload['unshareable_total_count']} 条），跳过写入与部署。")
        finish("unchanged", 0)

    check_no_leak(payload, [token, *family_tokens.values()])

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SITE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    DATA_PATH.write_text(text, encoding="utf-8")
    SITE_DATA_PATH.write_text(text, encoding="utf-8")

    new_30d = sum(1 for g in payload["games"] if g["is_new"])
    log(f"更新完成：主库 {payload['count']} 款，去重家庭库 "
        f"{payload['library_total_count']} 款，本次新出现 {len(appeared)} 款，"
        f"最近 30 天新入库 {new_30d} 款，成员不可共享记录 "
        f"{payload['unshareable_total_count']} 条。")
    finish("updated", 0)


if __name__ == "__main__":
    main()
