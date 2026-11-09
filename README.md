# Steam 家庭游戏库看板

通过 Steam 未公开接口 `IFamilyGroupsService/GetSharedLibraryApps/v1` 抓取**全家庭合并游戏库**，提供两种使用模式：

- **本地模式**：FastAPI + SQLite，浏览器海报墙看板（功能完整）
- **云端模式**：GitHub Actions 定时抓取 → 生成脱敏公开数据 → 部署到 GitHub Pages（无需电脑开机）

## 本地模式

```bash
pip install fastapi "uvicorn[standard]"
python app.py          # 或 py app.py
```

浏览器打开 <http://127.0.0.1:8000>。

### 本地配置 config.json

```json
{
  "token": "",
  "members": { "76561198xxxxxxxx1": "我的昵称" },
  "include_free": false,
  "include_non_games": false
}
```

| 字段 | 说明 |
|---|---|
| `token` | webapi_token，约 24 小时过期，获取方式见下 |
| `members` | SteamID64 → 昵称映射，用于"谁买的"展示和筛选 |
| `include_free` / `include_non_games` | 是否包含免费游戏 / 非游戏应用 |

**Token 获取**：浏览器登录 Steam → 访问 <https://store.steampowered.com/pointssummary/ajaxgetasyncconfig> → 复制返回 JSON 中 `data.webapi_token` 的值。也可在页面右上角「Token 设置」直接粘贴保存（自动写回 config.json 并重新抓取）。

本地数据存 `library.db`（games / snapshots / meta 三表），抓取失败时保留上次快照，页面标注"数据来自 xx 时间"。

## Windows Token 自动更新工具

`token_helper.py` 只在本机运行，不进入 GitHub Actions，也不开发网页 UI。它使用项目独立的 `steam_profile/` 保存 Steam 登录状态，成功获取并校验 token 后写入 `token.txt`，再尝试通过 GitHub CLI 更新 Secret。

### 安装与首次运行

在本项目目录打开 PowerShell：

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py token_helper.py
```

第一次运行会打开有界面的 Chromium。手动登录 Steam（包括验证码或手机确认），回到终端按回车。以后每天运行会复用 `steam_profile/`，优先无界面获取；登录失效时会再次打开浏览器让你登录。

如需使用自动上传，再安装并登录 GitHub CLI（凭据完全由 `gh` 自己管理，本项目不保存 GitHub PAT）：

```powershell
winget install --id GitHub.cli
# 安装后重新打开 PowerShell
gh auth login
```

`gh auth login` 时选择 GitHub.com、HTTPS，并按提示用浏览器授权；不要在项目配置或命令参数里保存 PAT。确保登录账号对目标仓库有修改 Actions Secrets 的权限。

### local_config.json

```json
{
  "auto_upload": true,
  "trigger_workflow_after_upload": true,
  "workflow_name": "update.yml"
}
```

- `auto_upload`：是否尝试 `gh secret set STEAM_WEBAPI_TOKEN --body <token>`；设为 `false` 时只保存并进入人工复制流程。
- `trigger_workflow_after_upload`：Secret 上传成功后是否立即执行 `gh workflow run update.yml`。
- `workflow_name`：要触发的 Workflow 文件名，默认与现有项目一致。

### 成功与失败表现

- 成功：token 通过三段 JWT 和 `exp` 校验，保存到 `token.txt`；日志只显示前后各 4 个字符。随后更新 Secret，并按配置触发一次云端更新。
- Steam 未登录：打开有界面浏览器重新登录，登录后再尝试获取。
- Steam 页面结构变化、token 已过期：给出明确中文原因，不使用该 token。
- `gh` 未安装、未登录或 GitHub 网络不可用：不反复重试，保留 `token.txt`，把 token 复制到 Windows 剪贴板，提示到仓库 **Settings → Secrets and variables → Actions** 手动更新 `STEAM_WEBAPI_TOKEN`，然后正常结束。
- 上传失败不会主动清空或修改网站数据，也不会先删除旧 Secret。若网络中断发生在 GitHub 已接收请求之后，工具无法确认结果，以 GitHub 页面实际状态为准。
- 某天没有运行可直接跳过；第二天运行仍会获取当时的新 token，无需补历史。

### Windows 任务计划程序：每天 22:00

先手动运行一次，完成 Playwright 安装、Steam 登录和 `gh auth login`，再创建计划任务。

**图形界面方式：**

1. 按 `Win+R`，输入 `taskschd.msc`，打开“任务计划程序”。
2. 右侧选择“创建任务”（不要选“创建基本任务”），名称填 `SteamTokenHelper`。建议选择“只在用户登录时运行”，以便登录失效时能显示浏览器。
3. “触发器”→“新建”：选择“每天”，开始时间设为 `22:00:00`，确认已启用。
4. “操作”→“新建”→“启动程序”：
   - 程序或脚本：填写 Python 启动器的完整路径，可在 PowerShell 执行 `(Get-Command py.exe).Source` 查询。
   - 添加参数：`token_helper.py`
   - 起始于：本项目完整目录，例如 `E:\Code\Python_Projects\steam`
5. “条件”：笔记本可按需取消“只有在计算机使用交流电源时才启动”。
6. “设置”：勾选 **“如果错过了计划开始时间，请尽快启动任务”**。这样白天没开机、晚上开机登录后，任务会尽快补跑。可再设置“如果任务运行时间超过 1 小时则停止”。
7. 保存后右键任务选择“运行”做一次测试；下次可打开“历史记录”排查执行情况。

**PowerShell 命令方式（推荐，直接包含补跑设置）：**

先在 PowerShell 中修改 `$project` 为你的项目目录，然后执行：

```powershell
$project = 'E:\Code\Python_Projects\steam'
$python = (Get-Command py.exe).Source
$action = New-ScheduledTaskAction -Execute $python -Argument 'token_helper.py' -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At '22:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'SteamTokenHelper' -Action $action -Trigger $trigger -Settings $settings -Description '每天获取 Steam token 并尝试更新 GitHub Secret' -Force
```

也可用 `schtasks` 创建基础每日任务：

```powershell
schtasks /Create /TN "SteamTokenHelper" /SC DAILY /ST 22:00 /F /TR "\"C:\完整路径\py.exe\" \"E:\Code\Python_Projects\steam\token_helper.py\""
```

`schtasks` 命令本身没有“错过计划后尽快运行”的对应参数；用它创建后，仍需按上面的图形界面第 6 步勾选，或改用完整 PowerShell 示例。

### 本地工具安全边界

- 只使用本项目 `steam_profile/`，不读取、导出或复制 Chrome/Edge 等其他浏览器的 Cookie。
- `steam_profile/`、`token.txt`、临时写入文件和 `local_config.json` 均已被 `.gitignore` 排除，绝不提交。
- 日志不输出完整 token；`gh` 输出也会先隐藏 token 再展示。
- 不向 Actions 上传浏览器目录；云端仍只读取 GitHub Secrets，不依赖本机文件。

## 云端模式（GitHub Actions + Pages）

仓库已包含 `.github/workflows/update.yml`：每天北京时间约 22:40 自动运行，抓取消敏后部署 Pages。

### 一次性配置

1. **建仓库并推送**本项目（确认 `config.json`、`library.db` 未被提交，它们已在 `.gitignore` 中）。
2. **设置 Secrets**：仓库 → Settings → Secrets and variables → Actions → New repository secret：
   - `STEAM_WEBAPI_TOKEN`：主账号 Steam webapi_token（同本地获取方式，24 小时有效）
   - `MEMBERS_JSON`（可选）：格式 `{"76561198xxxx":"家人A","76561198yyyy":"家人B"}`，注意 Secrets 里要写**单行 JSON**。不填时未映射成员显示为「成员 XXXX」（SteamID 后 4 位）。
   - `FAMILY_TOKENS_JSON`（可选）：多成员不可共享采集配置，格式 `{"家人A":"token_a","家人B":"token_b"}`，必须是单行 JSON。只要配置了该 Secret（包括 `{}`），就会自动纳入主账号 `STEAM_WEBAPI_TOKEN` 自己的不可共享结果；其他成员按这里的 Token 逐个抓取。某个成员过期、401/403 或网络失败时，只跳过该成员并保留其上次成功数据，不影响主库和其他成员。未配置该 Secret 时，`member_unshared` 保持空数组，看板显示空状态。
3. **开启 Pages**：Settings → Pages → Source 选 **GitHub Actions**。
4. **首次运行**：Actions → 左侧 "更新家庭游戏库并部署 Pages" → Run workflow。成功后 Pages 地址在 Settings → Pages 里查看（形如 `https://<用户名>.github.io/<仓库名>/`）。

### Token 过期后的表现（核心设计）

- **过期前 10 分钟内或已过期**：workflow 正常结束（绿色），输出中文跳过原因，**不修改数据、不部署**，旧网站继续可用。
- **运行中返回 401/403**：同样视为失效，正常结束保留旧数据。
- **网络错误 / 返回 0 款游戏**：workflow 标红便于排查，旧数据不覆盖。
- **恢复**：更新了 Secrets 后手动 Run workflow 即可；断更多天也能正确识别期间新增的游戏（历史靠仓库里的 `data/public_library.json` 快照，不依赖 Actions 环境持久化）。

### 云端数据文件

`data/public_library.json` 为脱敏快照：`games` 是主库游戏，包含 appid、游戏名、去重后的提供者昵称、`copy_count`、acquired、游玩时长、可玩状态、first_seen_at、is_new；`library_total_count` 是主库与各成员数据按 appid 去重后的总数；`member_unshared` 按成员保存其共享结果中不可玩的游戏及上次成功更新时间。**不含** Token、SteamID、家庭组 ID。每次有变化才提交；无变化不产生空提交。旧版数据缺少新字段时，看板会自动回退到主库统计。

`site/` 为 Pages 发布目录（`site/index.html` 为简单预览页，后续会有独立任务升级 UI）。

## ⚠️ 私密信息清单（绝不能提交）

| 文件 | 内容 | 状态 |
|---|---|---|
| `config.json` | webapi_token + members 映射 | 已被 `.gitignore` 排除 |
| `library.db` | 本地数据库（含 SteamID） | 已被 `.gitignore` 排除 |
| `steam_profile/` | 独立 Chromium 的 Steam 登录状态 | 已被 `.gitignore` 排除 |
| `token.txt` / `token.txt.tmp` | 完整 webapi_token | 已被 `.gitignore` 排除 |
| `local_config.json` | 本机自动化设置 | 已被 `.gitignore` 排除 |
| GitHub Secrets | token / MEMBERS_JSON | 只存于 GitHub，不进仓库 |
| `data/public_library.json` | 已脱敏 | ✅ 可安全公开 |

> token 等同于账号凭据，泄露后请立即到 Steam 重新登录使其失效，并更换仓库 Secrets。

## API（本地模式）

| 接口 | 说明 |
|---|---|
| `GET /api/library` | 当前库全量 + meta（is_new、拥有者昵称、token 状态） |
| `POST /api/refresh` | 重新抓取 |
| `POST /api/token` | 更新 token（写回 config.json 并立即抓取） |
| `GET /api/health` | 状态（fetching / token_status / game_count） |

统一返回 `{"ok": true, "data": ...}` 或 `{"ok": false, "error": {"type", "message"}}`。

## 新入库判定（本地/云端一致）

`is_new` 表示最近 30 天入库：`acquired`（游戏获得日期）非空时优先使用；为空时使用 `first_seen_at`（该游戏第一次出现在库里的日期）。

## 注意事项

- 仅供个人学习使用；未公开接口随时可能失效
- 云端 workflow 复用 `app.py` 的抓取逻辑（`scripts/cloud_update.py` 直接 import），本地功能未做任何改动
- 本期不含价格查询
