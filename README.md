# Local LLM Stack — 48GB GPU Edition

VSCode Claude Code Plugin x 本地 LLM 完整部署包
支援 NVIDIA Nemotron + Google Gemma，多人隔離 + RAG 向量搜尋 + GPU 請求排隊。

---

## 架構總覽

```
開發機 (Mac/PC)                          GPU Server (Linux + NVIDIA)
┌──────────────────┐        LAN        ┌──────────────────────────────────────┐
│ VSCode           │                   │  RAG Proxy (:4001)                   │
│ + Claude Code    │ ── X-API-Key ───> │  ├─ API Key 驗證                    │
│   Plugin         │                   │  ├─ GPU 排隊 (Semaphore)            │
│                  │                   │  ├─ Qdrant 向量搜尋                  │
│ ANTHROPIC_BASE_  │                   │  └─ 注入 context → LiteLLM (:4000)  │
│ URL=:4001        │                   │       └─ Ollama (:11434) → GPU 推理  │
└──────────────────┘                   │                                      │
                                       │  Open WebUI (:8080) ─ 模型管理介面   │
                                       │  Qdrant (:6333) ─ 向量資料庫         │
                                       └──────────────────────────────────────┘
```

---

## 目錄結構

```
local-llm-stack/
├── docker-compose.yml              # 主要服務定義
├── .env.example                    # 環境變數範本（複製為 .env）
├── manage.sh                       # 管理腳本（啟動、停止、測試）
├── config/
│   ├── litellm_config.yaml         # LiteLLM 模型路由設定
│   └── api_keys.json               # 團隊成員 API Key（多人隔離）
├── services/
│   └── rag-proxy/
│       ├── app.py                  # RAG Proxy 主程式（驗證 + 排隊 + RAG）
│       ├── indexer.py              # 程式碼向量索引工具
│       ├── Dockerfile
│       └── requirements.txt
├── scripts/
│   ├── pull_models.sh              # 自動拉取模型腳本
│   └── generate_ssl.sh             # SSL 憑證產生（內部部署可略過）
└── guide-vscode-claude-code.html   # 完整圖文設定指南
```

---

## 部署流程（GPU Server 端）

### Step 1 — 環境變數設定

```bash
cp .env.example .env
vim .env
```

**必填項目：**

| 變數 | 說明 | 範例 |
|------|------|------|
| `LITELLM_MASTER_KEY` | LiteLLM 認證 Key（自訂，`sk-` 開頭） | `sk-mydev-2025` |
| `WEBUI_SECRET_KEY` | Open WebUI 密鑰 | `openssl rand -hex 32` |

> 其餘 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 為可選雲端備援。

**驗證方式：**

```bash
cat .env | grep -E "^(LITELLM_MASTER_KEY|WEBUI_SECRET_KEY)"
# 應看到兩個非空值
```

---

### Step 2 — 檢查 NVIDIA 環境

```bash
chmod +x manage.sh
./manage.sh setup
```

**驗證方式：**

```bash
nvidia-smi
# 應顯示 GPU 型號和 48GB VRAM

docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
# 應顯示相同結果（確認 Docker GPU 存取正常）
```

> 若缺少 `nvidia-container-toolkit`，參考 `.env.example` 或 `guide-vscode-claude-code.html` 安裝指引。

---

### Step 3 — 設定團隊 API Key

為每位成員產生專屬 Key：

```bash
# 產生隨機 Key
python3 -c "import secrets; print(f'rag-{secrets.token_urlsafe(32)}')"
```

編輯 `config/api_keys.json`：

```json
{
  "keys": {
    "rag-xxxxx-willis的key": {
      "user_id": "willis",
      "project_id": "xse-apps",
      "enabled": true
    },
    "rag-xxxxx-dev2的key": {
      "user_id": "dev2",
      "project_id": "xse-api",
      "enabled": true
    }
  }
}
```

| 欄位 | 說明 |
|------|------|
| `user_id` | Log 追蹤 + GPU 排隊識別 |
| `project_id` | 預設 RAG collection（自動路由到 `codebase-{project_id}`）|
| `enabled` | 設 `false` 即時停用（不需重啟服務）|

**驗證方式：**

```bash
python3 -c "import json; d=json.load(open('config/api_keys.json')); print(f'Keys: {len(d[\"keys\"])} 把')"
# 應顯示 Key 數量
```

---

### Step 4 — 啟動服務

```bash
./manage.sh start
```

首次啟動會自動下載模型（`model-puller`），依網速約需 20-40 分鐘。

**驗證方式：**

```bash
# 所有容器應為 running / healthy
docker compose ps

# 預期輸出：6 個服務 (ollama, litellm, open-webui, qdrant, rag-proxy, model-puller)
# model-puller 完成後會自動退出（Exited (0)）
```

```bash
# 逐一確認服務健康
curl -s http://localhost:11434/api/version  # Ollama
curl -s http://localhost:4000/health        # LiteLLM
curl -s http://localhost:6333/healthz       # Qdrant
curl -s http://localhost:4001/health        # RAG Proxy
```

每個都應回傳 JSON，不應 timeout 或 connection refused。

---

### Step 5 — 測試 API 推理

```bash
./manage.sh test
```

**驗證方式：**

```bash
# 從 Server 本機直接測 LiteLLM
curl -s http://localhost:4000/v1/messages \
  -H "x-api-key: $(grep LITELLM_MASTER_KEY .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nemotron-super",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
# 應回傳包含 "content" 的 JSON

# 測 RAG Proxy（帶 API Key）
curl -s http://localhost:4001/v1/messages \
  -H "x-api-key: $(grep LITELLM_MASTER_KEY .env | cut -d= -f2)" \
  -H "X-API-Key: rag-你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nemotron-super",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
# 應回傳模型回應（而非 401 驗證失敗）
```

---

### Step 6 — 開放防火牆

```bash
# Ubuntu / Debian
sudo ufw allow 4001/tcp comment 'RAG Proxy'
sudo ufw allow 4000/tcp comment 'LiteLLM (optional)'
sudo ufw allow 8080/tcp comment 'Open WebUI (optional)'
sudo ufw reload
```

**驗證方式（從開發機執行）：**

```bash
curl -s http://GPU_SERVER_IP:4001/health
# 應回傳 RAG Proxy 健康狀態 JSON
```

---

### Step 7 — 建立 RAG 索引（可選但推薦）

為每個專案建立獨立的向量索引：

```bash
# --project 名稱必須與 api_keys.json 的 project_id 一致
./manage.sh index /path/to/xse-apps --project xse-apps
./manage.sh index /path/to/xse-api  --project xse-api
```

**驗證方式：**

```bash
./manage.sh rag-status
# 應顯示 Qdrant 中的 collection 和 vector 數量
# 例如：Collection: codebase-xse-apps (1234 vectors)
```

---

## 開發機端設定（VSCode + Claude Code Plugin）

### Step 1 — 設定環境變數

在 `~/.zshrc`（macOS）或 `~/.bashrc`（Linux）加入：

```bash
# ── Local LLM Stack ──────────────────────────
export ANTHROPIC_BASE_URL="http://GPU_SERVER_IP:4001"
export ANTHROPIC_API_KEY="你的LITELLM_MASTER_KEY"
```

```bash
source ~/.zshrc
```

### Step 2 — 設定 API Key（VSCode settings.json）

按 `Cmd+Shift+P` → "Preferences: Open User Settings (JSON)"：

```jsonc
{
  "claude-code.apiHeaders": {
    "X-API-Key": "rag-你的專屬Key"
  }
}
```

> `ANTHROPIC_API_KEY` = 團隊共用的 LiteLLM Master Key
> `X-API-Key` = 你個人專屬的 RAG Proxy Key（向 Server 管理員索取）

### Step 3 — 重啟 VSCode 並驗證

完全關閉再重開 VSCode，開啟 Claude Code 面板，輸入任意訊息。

**驗證方式：**

```bash
# 在開發機 Terminal 確認環境變數
echo $ANTHROPIC_BASE_URL
# 應顯示 http://GPU_SERVER_IP:4001

# 從開發機測 RAG Proxy 連線
curl -s http://GPU_SERVER_IP:4001/health
# 應回傳 JSON

# 確認 GPU Server 有收到請求
# 在 GPU Server 上執行：
./manage.sh logs rag-proxy | grep "queue"
# 應看到你的 user_id 和 slot 取得記錄
```

---

## 確認清單

部署完成後，逐項打勾確認：

### GPU Server 端

- [ ] `.env` 已設定 `LITELLM_MASTER_KEY` 和 `WEBUI_SECRET_KEY`
- [ ] `nvidia-smi` 顯示 GPU 正常、48GB VRAM
- [ ] `docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi` 成功
- [ ] `config/api_keys.json` 已為每位成員建立 Key
- [ ] `./manage.sh start` 所有容器啟動成功
- [ ] `curl localhost:11434/api/version` 回傳 Ollama 版本
- [ ] `curl localhost:4000/health` 回傳 LiteLLM 健康
- [ ] `curl localhost:6333/healthz` 回傳 Qdrant 健康
- [ ] `curl localhost:4001/health` 回傳 RAG Proxy 健康（含 `auth_enabled: true`）
- [ ] `./manage.sh test` 顯示 `回應成功`
- [ ] `./manage.sh list` 顯示已下載的模型（至少 nemotron-3-nano, nemotron-3-super）
- [ ] 防火牆已開放 port 4001（及可選的 4000, 8080）
- [ ] `./manage.sh index /path/to/project --project xxx` 索引完成（若使用 RAG）
- [ ] `./manage.sh rag-status` 顯示 collection 和 vector 數量

### 開發機端

- [ ] `~/.zshrc` 已設定 `ANTHROPIC_BASE_URL=http://GPU_SERVER_IP:4001`
- [ ] `~/.zshrc` 已設定 `ANTHROPIC_API_KEY=你的LITELLM_MASTER_KEY`
- [ ] `source ~/.zshrc` 後 `echo $ANTHROPIC_BASE_URL` 顯示正確
- [ ] VSCode settings.json 已設定 `claude-code.apiHeaders` 含 `X-API-Key`
- [ ] `curl http://GPU_SERVER_IP:4001/health` 從開發機回傳成功
- [ ] VSCode 已完全重啟（非 Reload Window，而是關閉再開啟）
- [ ] Claude Code 面板發送訊息後正常回應
- [ ] GPU Server 的 `./manage.sh logs rag-proxy` 有看到請求 log

### 多人驗證（5+ 人團隊）

- [ ] 每人有不同的 `X-API-Key`
- [ ] 不帶 `X-API-Key` 的請求被擋下（回傳 `401`）
- [ ] `curl GPU_SERVER_IP:4001/queue/status` 顯示正確的排隊狀態
- [ ] 兩人同時使用時，GPU Server log 顯示各自的 `user_id`
- [ ] 不同 `project_id` 的人搜到的 RAG 結果來自各自的 collection

---

## 模型清單（48GB VRAM）

### NVIDIA Nemotron

| LiteLLM Name | 實際模型 | VRAM | 用途 |
|--------------|----------|------|------|
| `nemotron-nano` | nemotron-3-nano | ~6GB | 快速日常 |
| `nemotron-super` | nemotron-3-super | ~16GB | **主力推薦** |
| `nemotron-70b` | nemotron | ~40GB | 旗艦品質 |

### Google Gemma

| LiteLLM Name | 實際模型 | VRAM | 用途 |
|--------------|----------|------|------|
| `codegemma-2b` | codegemma:2b | ~2GB | 極輕量補全 |
| `codegemma-7b` | codegemma:7b | ~6GB | Python/JS |
| `gemma2-9b` | gemma2:9b | ~8GB | 通用平衡 |
| `gemma2-27b` | gemma2:27b | ~18GB | 高品質通用 |

### Claude Code 自動路由

Claude Code 發出的 model name 自動對應本地模型：

| Claude Code 呼叫 | 路由到 | 說明 |
|-----------------|--------|------|
| `claude-haiku-4-5-20251001` | nemotron-nano | 快速回應 |
| `claude-sonnet-4-5` | nemotron-super | 主力推薦 |
| `claude-opus-4-6` | nemotron-70b | 旗艦品質 |

---

## 常用指令

```bash
# 服務管理
./manage.sh start              # 啟動服務
./manage.sh stop               # 停止服務
./manage.sh restart            # 重啟服務
./manage.sh status             # 查看狀態 + GPU 使用量
./manage.sh logs rag-proxy     # 查看 RAG Proxy log（含 auth + queue）
./manage.sh logs litellm       # 查看 LiteLLM log

# 模型管理
./manage.sh list               # 列出已安裝模型
./manage.sh pull gemma2:27b    # 下載新模型
./manage.sh remove-model xxx   # 刪除模型

# RAG 索引
./manage.sh index /path/to/project --project project-name
./manage.sh rag-status         # 查看 RAG + Qdrant 狀態

# 設定輔助
./manage.sh test               # 測試 API
./manage.sh vscode             # 顯示 VSCode 環境變數設定
```

---

## 多人隔離機制

| 層級 | 機制 | 說明 |
|------|------|------|
| 身份驗證 | `X-API-Key` header | 每人專屬 Key，無 Key 回傳 401 |
| RAG 隔離 | Per-project collection | Key 綁定 `project_id`，自動路由到對應 collection |
| 資源保護 | GPU Semaphore | `MAX_CONCURRENT_REQUESTS=3`，超過排隊等待 |
| 用量追蹤 | Log 記錄 | 每筆請求記錄 `user_id`、等待時間、GPU 佔用時間 |

### 管理員操作

```bash
# 新增成員
python3 -c "import secrets; print(f'rag-{secrets.token_urlsafe(32)}')"
vim config/api_keys.json     # 加入新 Key（不需重啟）

# 停用成員
# 編輯 api_keys.json，將 enabled 改為 false（即時生效）

# 查看排隊狀態
curl http://localhost:4001/queue/status

# 查看誰在使用
./manage.sh logs rag-proxy | grep "[queue]"
```

### 建議的 .env 參數（5+ 人團隊）

```bash
OLLAMA_NUM_PARALLEL=4           # 並行推理數
OLLAMA_MAX_LOADED=2             # 同時載入模型數（避免 VRAM OOM）
OLLAMA_KEEP_ALIVE=4h            # 縮短保留時間
MAX_CONCURRENT_REQUESTS=3       # RAG Proxy 同時轉發上限
QUEUE_TIMEOUT=300               # 排隊逾時秒數
```

---

## 常見問題

**Q: 第一次啟動很慢？**
A: `model-puller` 自動下載模型中，用 `./manage.sh logs model-puller` 查看進度。

**Q: Claude Code 回傳 401 authentication_error？**
A: `X-API-Key` 驗證失敗。確認 VSCode settings.json 的 `claude-code.apiHeaders` 設定，以及 Key 存在於 Server 的 `config/api_keys.json` 且 `enabled: true`。

**Q: 429 rate_limit_error / GPU queue full？**
A: GPU 排隊已滿。稍等再試，或調高 `MAX_CONCURRENT_REQUESTS`。用 `curl localhost:4001/queue/status` 查看排隊狀態。

**Q: Claude Code 連不上？**
A: 確認 `ANTHROPIC_BASE_URL` 指向 `http://GPU_SERVER_IP:4001`，防火牆已開放 4001 port，且 `curl GPU_SERVER_IP:4001/health` 有回應。

**Q: VRAM 不足 / CUDA OOM？**
A: 減少 `OLLAMA_MAX_LOADED`，或統一使用 `nemotron-super`(16GB) 避免有人載入 70B 佔滿 VRAM。

**Q: RAG 沒有注入 context？**
A: 確認已執行 `./manage.sh index --project xxx`、`ANTHROPIC_BASE_URL` 指向 `:4001`（非 `:4000`）、`./manage.sh rag-status` 顯示 collection 有 vectors。

**Q: 想切回 Anthropic 雲端？**
A: `unset ANTHROPIC_BASE_URL && export ANTHROPIC_API_KEY="sk-ant-你的Key"`，重啟 VSCode。
