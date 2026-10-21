# B 的交接說明（Agent AI 導覽＋地圖碳排）

> 分支：`feature/agent`，已透過 PR #2 合進 `main`（2026-10-15）。詳細規格見 `docs/agent-spec.md`、`docs/carbon-route-prompt.md`。

## 做了什麼

**1. AI Guide（Agent AI 導覽）** — `agent/`
- Python + FastAPI + 本機 Ollama（`llama3.2:3b`，Meta），四種語言、串流回覆、可用使用者位置找附近景點。
- Agent 沒開時，網頁自動改用離線版問答（`index.html` 的 `CHAT_KB`）。
- 安全處理：回覆過濾 HTML／網址／系統提示詞外洩，前端不用 `innerHTML` 顯示 Agent 回覆。

**2. 地圖資料改用 data.gov.tw** — `scripts/fetch-opendata.mjs`
- 每縣市景點 20、餐廳 10、活動 8（共 736 筆，含 `city` 欄位），免金鑰。舊的 TDX 版 `fetch-tdx.mjs` 註解保留。

**3. 地圖頁「我的行程」（路線與碳排）** — `carbon/`
- 景點資訊面板多一個「加入行程」按鈕（最多 8 個），選起點和人數後「查詢路線」，每段列出各交通方式的時間、碳排與建議。
- 碳排係數：環境部官方資料（data.gov.tw 28176）。車站與公車站牌：TDX，每週由 GitHub Actions 自動更新。
- Agent 和地圖頁共用同一份係數檔（`carbon/carbon-factors.json`），建議只由地圖頁產生。
- 競賽用說明文件：`docs/carbon-method.md`。

## 動到共用檔案的地方（請留意）

| 檔案 | 改了什麼 |
| --- | --- |
| `index.html` | AI Guide 分頁與離線問答；地圖頁**只新增**行程面板容器、`#tripSlot` 按鈕容器、1 個 CSS 和 4 個 JS 引入。`selectLocation` 沒有改，是從外面包一層 |
| `.github/workflows/update-tdx-data.yml` | 改跑 `fetch-opendata.mjs`；新增 TDX 車站資料步驟（失敗不影響觀光資料） |
| `data/` | 新增 `transit_stations.json`、`bus_access.json`（自動產生，不要手改） |

## 想請 A 看一下（A 的範圍，B 沒有動）

1. 手機上左側欄預設是展開的，地圖只剩約 100px 寬，「我的行程」面板會被擋住。
2. `selectLocation` 用 `innerHTML` 顯示景點圖片、名稱和網址，建議改成 `textContent`／`setAttribute`（較安全）。

## 還沒完成

- 日文、德文文字待懂的組員審閱：`carbon/strings.js`、`agent/config/site_help.json`、`index.html` 的功能介紹。

## 怎麼跑

```bash
python3 -m http.server 5500 --bind 127.0.0.1          # 網站：http://localhost:5500
cd agent && source .venv/bin/activate && uvicorn app.main:app --port 8787   # Agent（需先開 Ollama）
node --test carbon/tests/*.test.js scripts/build-transit-data.test.mjs     # 碳排測試
cd agent && .venv/bin/python -m pytest -q                                   # Agent 測試
```
