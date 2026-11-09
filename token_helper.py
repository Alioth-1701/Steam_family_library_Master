#!/usr/bin/env python3
"""Windows 本地 Steam webapi_token 获取与 GitHub Secret 更新工具。"""

import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "steam_profile"
TOKEN_PATH = BASE_DIR / "token.txt"
CONFIG_PATH = BASE_DIR / "local_config.json"

ASYNC_CONFIG_URL = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"
STEAM_LOGIN_URL = "https://store.steampowered.com/login/"
SECRET_NAME = "STEAM_WEBAPI_TOKEN"
DEFAULT_CONFIG = {
    "auto_upload": True,
    "trigger_workflow_after_upload": True,
    "workflow_name": "update.yml",
}

BROWSER_TIMEOUT_MS = 60_000
GH_TIMEOUT_SECONDS = 45


class HelperError(Exception):
    """可直接展示给用户的错误。"""


class SteamNotLoggedIn(HelperError):
    pass


class PageStructureError(HelperError):
    pass


class TokenValidationError(HelperError):
    pass


class TokenExpiredError(TokenValidationError):
    pass


def log(message):
    print(message, flush=True)


def mask_token(token):
    return f"{token[:4]}…{token[-4:]}"


def hide_token(text, token):
    return (text or "").replace(token, "<token 已隐藏>") if token else (text or "")


def load_config():
    config = dict(DEFAULT_CONFIG)
    if not CONFIG_PATH.exists():
        return config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise HelperError(f"读取 local_config.json 失败：{e}。请检查 JSON 格式。")
    if not isinstance(user_config, dict):
        raise HelperError("local_config.json 顶层必须是 JSON 对象。")
    for key in DEFAULT_CONFIG:
        if key in user_config:
            config[key] = user_config[key]
    if not isinstance(config["auto_upload"], bool) or not isinstance(
        config["trigger_workflow_after_upload"], bool
    ):
        raise HelperError("auto_upload 和 trigger_workflow_after_upload 必须是 true 或 false。")
    if not isinstance(config["workflow_name"], str) or not config["workflow_name"].strip():
        raise HelperError("workflow_name 必须是非空字符串，例如 update.yml。")
    return config


def validate_token(token):
    """返回 JWT payload；三段格式错误、无 exp 或已过期均拒绝使用。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenValidationError("Token 校验失败：不是三段 JWT，请确认 webapi_token 复制完整。")
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenValidationError(f"Token 校验失败：JWT payload 无法解码（{e}）。")
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), (int, float)):
        raise TokenValidationError("Token 校验失败：payload 中没有有效的 exp 过期时间。")
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    if expires_at <= datetime.now(tz=timezone.utc):
        raise TokenExpiredError(
            f"Token 已过期（过期时间 {expires_at:%Y-%m-%d %H:%M:%S} UTC），未写入或上传。"
        )
    log(f"Token 校验通过：{mask_token(token)}，有效期至 {expires_at:%Y-%m-%d %H:%M:%S} UTC。")
    return payload


def save_token(token):
    temp_path = TOKEN_PATH.with_name("token.txt.tmp")
    try:
        temp_path.write_text(token, encoding="utf-8")
        temp_path.replace(TOKEN_PATH)
    except OSError as e:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HelperError(f"保存 token.txt 失败：{e}")
    log(f"Token 已保存到 {TOKEN_PATH}（内容不会打印到日志）。")


def copy_to_clipboard(token):
    """优先使用 Windows 剪贴板 API；tkinter 不可用时尝试 PowerShell。"""
    errors = []
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(token)
        root.update()
        root.destroy()
        return True
    except Exception as e:
        errors.append(f"tkinter: {type(e).__name__}")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
            input=token,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return True
    except Exception as e:
        errors.append(f"PowerShell: {type(e).__name__}")
    log(f"复制到剪贴板失败（{'；'.join(errors)}），请从 token.txt 手动复制。")
    return False


def show_manual_upload(token):
    if copy_to_clipboard(token):
        log("Token 已复制到 Windows 剪贴板。")
    log("请打开 GitHub 仓库 → Settings → Secrets and variables → Actions，")
    log(f"找到并更新 {SECRET_NAME}，粘贴剪贴板内容后保存。")
    log("本地 token.txt 已保留；本次不上传、不触发更新，明天可正常再次运行。")


def looks_like_login_page(page, body):
    url = (page.url or "").lower()
    text = (body or "").lower()
    return (
        "login.steampowered.com" in url
        or "/login" in url
        or "<title>sign in" in text
        or "login.steampowered.com" in text
    )


def request_webapi_token(page):
    """访问 Steam 的 ajax 配置接口并提取 webapi_token。"""
    try:
        response = page.goto(
            ASYNC_CONFIG_URL,
            wait_until="domcontentloaded",
            timeout=BROWSER_TIMEOUT_MS,
        )
        if response is None:
            raise PageStructureError("页面结构变化或访问失败：Steam 请求没有返回响应。")
        body = response.text()
    except PageStructureError:
        raise
    except Exception as e:
        raise HelperError(
            f"访问 Steam 配置页面失败（{type(e).__name__}）。请检查网络，稍后或明天再运行。"
        )

    if response.status in (401, 403) or looks_like_login_page(page, body):
        raise SteamNotLoggedIn("Steam 未登录或登录状态已失效，需要重新登录。")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        if looks_like_login_page(page, body):
            raise SteamNotLoggedIn("Steam 未登录或登录状态已失效，需要重新登录。")
        raise PageStructureError(
            f"页面结构变化：Steam 返回的不是 JSON（HTTP {response.status}），"
            "未找到可用的 webapi_token。"
        )
    if not isinstance(data, dict):
        raise PageStructureError("页面结构变化：Steam 返回的 JSON 顶层不是对象。")
    if data.get("success") is False:
        raise SteamNotLoggedIn("Steam 未登录或登录状态已失效，需要重新登录。")
    # Steam 当前返回 {"success":1,"data":{"webapi_token":"..."}}；
    # 兼容旧的顶层 webapi_token 结构，便于接口变化时继续工作。
    token = data.get("webapi_token")
    if not isinstance(token, str) or not token.strip():
        nested_data = data.get("data")
        if isinstance(nested_data, dict):
            token = nested_data.get("webapi_token")
    if not isinstance(token, str) or not token.strip():
        raise PageStructureError(
            "页面结构变化：JSON 中找不到非空的 webapi_token 字段，请检查 Steam 接口是否调整。"
        )
    return token.strip()


def fetch_from_browser(headless, manual_login):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise HelperError(
            "未安装 Playwright。请先执行：py -m pip install -r requirements.txt，"
            "再执行：py -m playwright install chromium"
        )

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                viewport={"width": 1100, "height": 800},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if manual_login:
                    try:
                        page.goto(
                            STEAM_LOGIN_URL,
                            wait_until="domcontentloaded",
                            timeout=BROWSER_TIMEOUT_MS,
                        )
                    except PlaywrightError:
                        log("登录页自动打开失败，请在已打开的 Chromium 中手动访问 Steam 并登录。")
                    log("请在 Chromium 窗口中登录 Steam（含必要的验证码/手机确认）。")
                    try:
                        input("登录完成后回到这里按回车，程序将自动获取 token：")
                    except EOFError:
                        raise HelperError(
                            "需要人工登录，但当前运行方式没有可输入的控制台。"
                            "请在终端手动运行一次 token_helper.py 完成登录。"
                        )
                return request_webapi_token(page)
            finally:
                context.close()
    except HelperError:
        raise
    except PlaywrightError as e:
        message = str(e)
        if "Executable doesn't exist" in message or "playwright install" in message.lower():
            raise HelperError("Chromium 尚未安装。请执行：py -m playwright install chromium")
        raise HelperError(
            f"启动或操作 Chromium 失败（{type(e).__name__}）。"
            "请关闭残留的自动化浏览器窗口后重试，并检查网络。"
        )


def get_token_from_steam():
    """日常优先无界面运行；首次或登录失效时打开有界面 Chromium。"""
    first_run = not PROFILE_DIR.exists()
    if first_run:
        log("首次运行：将打开 Chromium，登录状态只保存在本项目的 steam_profile/。")
        return fetch_from_browser(headless=False, manual_login=True)
    try:
        return fetch_from_browser(headless=True, manual_login=False)
    except SteamNotLoggedIn:
        log("检测到 Steam 登录失效，将打开有界面 Chromium 供你重新登录。")
        return fetch_from_browser(headless=False, manual_login=True)


class GhError(Exception):
    def __init__(self, kind, detail=""):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def classify_gh_output(output):
    text = output.lower()
    if any(x in text for x in (
        "not logged", "authentication", "unauthorized", "forbidden", "401", "403",
    )):
        return "auth"
    if any(x in text for x in (
        "could not resolve", "no such host", "dial tcp", "timeout", "timed out",
        "connection refused", "connection reset", "tls handshake", "i/o timeout",
        "network", "connect", "proxy", "eof",
    )):
        return "network"
    return "unknown"


def run_gh(gh_path, args, token):
    try:
        result = subprocess.run(
            [gh_path, *args],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise GhError("network", "命令执行超时")
    except OSError as e:
        raise GhError("missing", str(e))
    if result.returncode != 0:
        output = hide_token(f"{result.stdout}\n{result.stderr}".strip(), token)
        raise GhError(classify_gh_output(output), output[:500])
    return result


def show_gh_failure(error):
    if error.kind == "network":
        log("GitHub 网络不可用或请求超时，未确认 Secret 是否更新；不会反复重试。")
    elif error.kind == "auth":
        log("gh 未登录或没有当前仓库的 Secret 权限。可执行 gh auth login 后重试。")
    elif error.kind == "missing":
        log("gh 无法启动。可安装后执行 gh auth login。")
    else:
        log("gh 更新 Secret 失败，原因无法自动分类。")
    if error.detail:
        log(f"gh 返回信息（已脱敏）：{error.detail}")


def upload_secret_and_trigger(token, config):
    gh_path = shutil.which("gh")
    if not gh_path:
        log("未检测到 GitHub CLI（gh）。可用 winget install GitHub.cli 安装，并执行 gh auth login。")
        show_manual_upload(token)
        return

    try:
        run_gh(gh_path, ["auth", "status"], token)
    except GhError as e:
        show_gh_failure(e)
        show_manual_upload(token)
        return

    try:
        run_gh(gh_path, ["secret", "set", SECRET_NAME, "--body", token], token)
    except GhError as e:
        show_gh_failure(e)
        show_manual_upload(token)
        return

    log(f"GitHub Secret {SECRET_NAME} 更新成功。")
    if not config["trigger_workflow_after_upload"]:
        log("按 local_config.json 配置，不立即触发 Workflow；将等待下一次定时运行。")
        return

    workflow_name = config["workflow_name"].strip()
    try:
        run_gh(gh_path, ["workflow", "run", workflow_name], token)
    except GhError as e:
        show_gh_failure(e)
        log(f"Secret 已成功更新，仅立即触发失败。可在 Actions 页面手动 Run workflow：{workflow_name}。")
        log("不触发也不会影响现有网站；本地 token.txt 已保留。")
        return
    log(f"已请求触发 Workflow：{workflow_name}。请稍后到 GitHub Actions 查看结果。")


def main():
    token = ""
    try:
        config = load_config()
        token = get_token_from_steam()
        validate_token(token)
        save_token(token)
        if config["auto_upload"]:
            upload_secret_and_trigger(token, config)
        else:
            log("auto_upload=false，按配置跳过 gh 上传。")
            show_manual_upload(token)
        return 0
    except HelperError as e:
        log(hide_token(str(e), token))
        if token and not TOKEN_PATH.exists():
            log("本次未取得可保存的有效 Token，已有数据不受影响。")
        return 1
    except KeyboardInterrupt:
        log("用户中止操作；已有 token.txt、GitHub Secret 和网站均不受影响。")
        return 1
    except Exception as e:
        log(hide_token(f"发生未预期错误（{type(e).__name__}），操作已停止。", token))
        return 1


if __name__ == "__main__":
    sys.exit(main())
