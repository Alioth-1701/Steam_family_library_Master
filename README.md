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

**Token 获取**：浏览器登录 Steam → 访问 <https://store.steampowered.com/pointssummary/ajaxgetasyncconfig> → 复制返回 JSON 中的 `webapi_token` 值。也可在页面右上角「Token 设置」直接粘贴保存（自动写回 config.json 并重新抓取）。

本地数据存 `library.db`（games / snapshots / meta 三表），抓取失败时保留上次快照，页面标注"数据来自 xx 时间"。

## 云端模式（GitHub Actions + Pages）

仓库已包含 `.github/workflows/update.yml`：每天北京时间约 22:40 自动运行，抓取消敏后部署 Pages。

### 一次性配置

1. **建仓库并推送**本项目（确认 `config.json`、`library.db` 未被提交，它们已在 `.gitignore` 中）。
2. **设置 Secrets**：仓库 → Settings → Secrets and variables → Actions → New repository secret：
   - `STEAM_WEBAPI_TOKEN`：Steam webapi_token（同本地获取方式，24 小时有效）
   - `MEMBERS_JSON`（可选）：格式 `{"76561198xxxx":"家人A","76561198yyyy":"家人B"}`，注意 Secrets 里要写**单行 JSON**。不填时未映射成员显示为「成员 XXXX」（SteamID 后 4 位）。
3. **开启 Pages**：Settings → Pages → Source 选 **GitHub Actions**。
4. **首次运行**：Actions → 左侧 "更新家庭游戏库并部署 Pages" → Run workflow。成功后 Pages 地址在 Settings → Pages 里查看（形如 `https://<用户名>.github.io/<仓库名>/`）。

### Token 过期后的表现（核心设计）

- **过期前 10 分钟内或已过期**：workflow 正常结束（绿色），输出中文跳过原因，**不修改数据、不部署**，旧网站继续可用。
- **运行中返回 401/403**：同样视为失效，正常结束保留旧数据。
- **网络错误 / 返回 0 款游戏**：workflow 标红便于排查，旧数据不覆盖。
- **恢复**：更新了 Secrets 后手动 Run workflow 即可；断更多天也能正确识别期间新增的游戏（历史靠仓库里的 `data/public_library.json` 快照，不依赖 Actions 环境持久化）。

### 云端数据文件

`data/public_library.json` 为脱敏快照，只含：appid、游戏名、拥有者昵称、acquired、游玩时长、可玩状态、first_seen_at、is_new。**不含** Token、SteamID、家庭组 ID。每次有变化才提交；无变化不产生空提交。

`site/` 为 Pages 发布目录（`site/index.html` 为简单预览页，后续会有独立任务升级 UI）。

## ⚠️ 私密信息清单（绝不能提交）

| 文件 | 内容 | 状态 |
|---|---|---|
| `config.json` | webapi_token + members 映射 | 已被 `.gitignore` 排除 |
| `library.db` | 本地数据库（含 SteamID） | 已被 `.gitignore` 排除 |
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
