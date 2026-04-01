#!/bin/bash
# ============================================================
#  Local LLM Stack — 管理腳本
#  使用方式：./manage.sh [指令]
# ============================================================

set -e

COMPOSE_FILE="docker-compose.yml"
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

print_header() {
  echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
  echo -e "${BLUE}║     Local LLM Stack — 管理工具          ║${NC}"
  echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
}

check_env() {
  if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚠️  找不到 .env 檔案，正在從 .env.example 複製...${NC}"
    cp .env.example .env
    echo -e "${GREEN}✅ .env 已建立，請確認設定後再次執行${NC}"
    echo ""
    echo "📝 重要：請檢查以下必填設定："
    echo "   LITELLM_MASTER_KEY  — VSCode 使用的本地 API Key"
    echo "   WEBUI_SECRET_KEY    — Open WebUI 密鑰（隨機字串即可）"
    echo ""
    echo "如果需要雲端備援，也請填入："
    echo "   OPENAI_API_KEY      — OpenAI API Key"
    echo "   ANTHROPIC_API_KEY   — Anthropic API Key"
    exit 1
  fi
}

check_nvidia() {
  if ! command -v nvidia-smi &> /dev/null; then
    echo -e "${YELLOW}⚠️  找不到 nvidia-smi，請確認 NVIDIA Driver 已安裝${NC}"
    exit 1
  fi
  if ! docker info 2>/dev/null | grep -q "nvidia"; then
    echo -e "${YELLOW}⚠️  Docker 未偵測到 NVIDIA runtime，請安裝 nvidia-container-toolkit${NC}"
    echo "   安裝指令：https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
    exit 1
  fi
  echo -e "${GREEN}✅ NVIDIA GPU 環境正常${NC}"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
}

cmd_setup() {
  print_header
  echo ""
  echo -e "${BLUE}🔧 初始化設定...${NC}"
  check_env
  check_nvidia
  echo ""
  echo -e "${GREEN}✅ 環境檢查完成！執行 ./manage.sh start 啟動服務${NC}"
}

cmd_start() {
  print_header
  check_env
  echo ""
  echo -e "${BLUE}🚀 啟動所有服務...${NC}"
  docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
  echo ""
  echo -e "${GREEN}✅ 服務已啟動！${NC}"
  echo ""
  echo "📡 服務端點："
  source .env
  echo "   Ollama API     → http://localhost:${OLLAMA_PORT:-11434}"
  echo "   LiteLLM Proxy  → http://localhost:${LITELLM_PORT:-4000}"
  echo "   Open WebUI     → http://localhost:${WEBUI_PORT:-8080}"
  echo ""
  echo "🔑 VSCode 環境變數："
  echo "   ANTHROPIC_BASE_URL=http://localhost:${LITELLM_PORT:-4000}"
  echo "   ANTHROPIC_API_KEY=${LITELLM_MASTER_KEY}"
}

cmd_stop() {
  echo -e "${YELLOW}🛑 停止所有服務...${NC}"
  docker compose -f "$COMPOSE_FILE" down
  echo -e "${GREEN}✅ 服務已停止${NC}"
}

cmd_restart() {
  cmd_stop
  sleep 2
  cmd_start
}

cmd_logs() {
  SERVICE="${2:-}"
  if [ -n "$SERVICE" ]; then
    docker compose -f "$COMPOSE_FILE" logs -f "$SERVICE"
  else
    docker compose -f "$COMPOSE_FILE" logs -f
  fi
}

cmd_status() {
  print_header
  echo ""
  echo -e "${BLUE}📊 服務狀態：${NC}"
  docker compose -f "$COMPOSE_FILE" ps
  echo ""
  echo -e "${BLUE}🎮 GPU 使用狀況：${NC}"
  nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu \
    --format=csv,noheader,nounits | \
    awk -F',' '{printf "   %-20s %s/%s MB (GPU: %s%%)\n", $1, $2, $3, $4}'
  echo ""
  echo -e "${BLUE}🤖 已載入的模型：${NC}"
  curl -sf http://localhost:${OLLAMA_PORT:-11434}/api/tags 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('models', []):
    size_gb = m.get('size', 0) / 1024**3
    print(f\"   {m['name']:<35} {size_gb:.1f} GB\")
" 2>/dev/null || echo "   （Ollama 尚未就緒）"
}

cmd_pull() {
  MODEL="$2"
  if [ -z "$MODEL" ]; then
    echo "使用方式：./manage.sh pull <model_name>"
    echo "例如：./manage.sh pull gemma2:27b"
    exit 1
  fi
  echo -e "${BLUE}⬇  下載模型：$MODEL${NC}"
  docker compose -f "$COMPOSE_FILE" exec ollama ollama pull "$MODEL"
  echo -e "${GREEN}✅ $MODEL 下載完成${NC}"
}

cmd_list() {
  echo -e "${BLUE}📋 已安裝的模型：${NC}"
  docker compose -f "$COMPOSE_FILE" exec ollama ollama list
}

cmd_remove_model() {
  MODEL="$2"
  if [ -z "$MODEL" ]; then
    echo "使用方式：./manage.sh remove-model <model_name>"
    exit 1
  fi
  echo -e "${YELLOW}🗑  刪除模型：$MODEL${NC}"
  docker compose -f "$COMPOSE_FILE" exec ollama ollama rm "$MODEL"
  echo -e "${GREEN}✅ $MODEL 已刪除${NC}"
}

cmd_test() {
  source .env 2>/dev/null || true
  PORT="${LITELLM_PORT:-4000}"
  KEY="${LITELLM_MASTER_KEY:-sk-local-dev-2025}"
  echo -e "${BLUE}🧪 測試 LiteLLM Proxy...${NC}"
  echo ""
  curl -s http://localhost:${PORT}/v1/messages \
    -H "x-api-key: ${KEY}" \
    -H "Content-Type: application/json" \
    -d '{
      "model": "nemotron-nano",
      "max_tokens": 80,
      "messages": [{"role": "user", "content": "用一句話介紹你自己"}]
    }' | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    text = d['content'][0]['text'] if 'content' in d else str(d)
    print(f'✅ 回應成功：{text[:120]}')
except:
    print('原始回應：', sys.stdin.read()[:300])
" 2>/dev/null || echo "⚠️  Proxy 未回應，確認服務是否已啟動"
}

cmd_vscode() {
  source .env 2>/dev/null || true
  RAG_PORT="${RAG_PROXY_PORT:-4001}"
  DIRECT_PORT="${LITELLM_PORT:-4000}"
  KEY="${LITELLM_MASTER_KEY:-sk-local-dev-2025}"
  RAG="${RAG_ENABLED:-true}"
  print_header
  echo ""
  if [ "$RAG" = "true" ]; then
    echo -e "${GREEN}📋 VSCode 設定（經 RAG Proxy，加入 ~/.zshrc 或 ~/.bashrc）：${NC}"
    echo ""
    echo -e "${YELLOW}export ANTHROPIC_BASE_URL=\"http://<GPU_SERVER_IP>:${RAG_PORT}\"${NC}"
    echo -e "${YELLOW}export ANTHROPIC_API_KEY=\"${KEY}\"${NC}"
    echo ""
    echo -e "${BLUE}ℹ  RAG 已啟用 — 請求會經過 :${RAG_PORT} (RAG Proxy) → :${DIRECT_PORT} (LiteLLM)${NC}"
  else
    echo -e "${GREEN}📋 VSCode 設定（直連 LiteLLM，加入 ~/.zshrc 或 ~/.bashrc）：${NC}"
    echo ""
    echo -e "${YELLOW}export ANTHROPIC_BASE_URL=\"http://<GPU_SERVER_IP>:${DIRECT_PORT}\"${NC}"
    echo -e "${YELLOW}export ANTHROPIC_API_KEY=\"${KEY}\"${NC}"
  fi
  echo ""
  echo -e "${BLUE}ℹ  將 <GPU_SERVER_IP> 替換為 GPU Server 的實際 IP${NC}"
}

cmd_index() {
  shift  # Remove 'index' from args

  DIR=""
  PROJECT=""
  COLLECTION=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project)
        PROJECT="$2"
        shift 2
        ;;
      --collection)
        COLLECTION="$2"
        shift 2
        ;;
      --*)
        echo "未知選項：$1"
        exit 1
        ;;
      *)
        DIR="$1"
        shift
        ;;
    esac
  done

  if [ -z "$DIR" ]; then
    echo "使用方式：./manage.sh index <directory> --project <project-name>"
    echo ""
    echo "範例："
    echo "  ./manage.sh index /path/to/xse-apps --project xse-apps"
    echo "  ./manage.sh index /path/to/xse-api --project xse-api"
    echo ""
    echo "Collection 命名規則："
    echo "  --project xse-apps  →  codebase-xse-apps"
    echo "  --collection foo    →  foo（直接使用）"
    echo "  無參數              →  codebase（預設）"
    exit 1
  fi
  # Resolve to absolute path
  DIR=$(cd "$DIR" 2>/dev/null && pwd)
  if [ ! -d "$DIR" ]; then
    echo -e "${RED}❌ 目錄不存在：$DIR${NC}"
    exit 1
  fi

  # Build indexer arguments
  INDEXER_ARGS=()
  if [ -n "$COLLECTION" ]; then
    INDEXER_ARGS+=("--collection" "$COLLECTION")
    echo -e "${BLUE}📦 正在索引：$DIR → collection: $COLLECTION${NC}"
  elif [ -n "$PROJECT" ]; then
    INDEXER_ARGS+=("--project" "$PROJECT")
    echo -e "${BLUE}📦 正在索引：$DIR → project: $PROJECT (collection: codebase-$PROJECT)${NC}"
  else
    echo -e "${BLUE}📦 正在索引：$DIR → collection: codebase (預設)${NC}"
  fi

  echo ""
  docker compose -f "$COMPOSE_FILE" run --rm \
    -v "${DIR}:/data/project:ro" \
    rag-proxy \
    python indexer.py /data/project "${INDEXER_ARGS[@]}"
  echo ""
  echo -e "${GREEN}✅ 索引完成！${NC}"
}

cmd_rag_status() {
  source .env 2>/dev/null || true
  RAG_PORT="${RAG_PROXY_PORT:-4001}"
  QDRANT_PORT="${QDRANT_PORT:-6333}"
  print_header
  echo ""
  echo -e "${BLUE}🔍 RAG 狀態：${NC}"
  echo ""

  # RAG Proxy health
  echo -n "   RAG Proxy (:${RAG_PORT})  → "
  RAG_HEALTH=$(curl -sf http://localhost:${RAG_PORT}/health 2>/dev/null)
  if [ -n "$RAG_HEALTH" ]; then
    echo -e "${GREEN}✅ 運行中${NC}"
    echo "      $RAG_HEALTH" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"      RAG enabled:  {d.get('rag_enabled', '?')}\")
    print(f\"      Qdrant:       {'connected' if d.get('qdrant_connected') else 'disconnected'}\")
    print(f\"      Embed model:  {d.get('embed_model', '?')}\")
    print(f\"      Collection:   {d.get('collection', '?')}\")
    print(f\"      Top K:        {d.get('top_k', '?')}\")
except: pass
" 2>/dev/null
  else
    echo -e "${RED}❌ 未回應${NC}"
  fi

  echo ""

  # Qdrant collections
  echo -n "   Qdrant (:${QDRANT_PORT})   → "
  QDRANT_HEALTH=$(curl -sf http://localhost:${QDRANT_PORT}/healthz 2>/dev/null)
  if [ -n "$QDRANT_HEALTH" ]; then
    echo -e "${GREEN}✅ 運行中${NC}"
    curl -sf http://localhost:${QDRANT_PORT}/collections 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    cols = d.get('result', {}).get('collections', [])
    if cols:
        for c in cols:
            name = c.get('name', '?')
            # Get collection info
            import urllib.request
            info = json.loads(urllib.request.urlopen(f'http://localhost:${QDRANT_PORT}/collections/{name}').read())
            count = info.get('result', {}).get('points_count', '?')
            print(f'      Collection: {name} ({count} vectors)')
    else:
        print('      （尚無 collection，請先執行 ./manage.sh index）')
except Exception as e:
    print(f'      （無法取得 collection 資訊）')
" 2>/dev/null
  else
    echo -e "${RED}❌ 未回應${NC}"
  fi
}

cmd_help() {
  print_header
  echo ""
  echo "使用方式：./manage.sh <指令>"
  echo ""
  echo -e "${GREEN}初始化：${NC}"
  echo "  setup          — 檢查環境、建立 .env 設定檔"
  echo ""
  echo -e "${GREEN}服務管理：${NC}"
  echo "  start          — 啟動所有服務"
  echo "  stop           — 停止所有服務"
  echo "  restart        — 重啟所有服務"
  echo "  status         — 查看服務與 GPU 狀態"
  echo "  logs [service] — 查看 log（可指定 ollama/litellm/open-webui）"
  echo ""
  echo -e "${GREEN}模型管理：${NC}"
  echo "  list           — 列出已安裝的模型"
  echo "  pull <model>   — 下載新模型"
  echo "  remove-model <model> — 刪除模型"
  echo ""
  echo -e "${GREEN}RAG 向量搜尋：${NC}"
  echo "  index <dir> --project <name> — 將目錄索引到 Qdrant（collection: codebase-<name>）"
  echo "  rag-status                   — 查看 RAG Proxy + Qdrant 狀態"
  echo ""
  echo -e "${GREEN}測試 & 設定：${NC}"
  echo "  test           — 測試 LiteLLM Proxy 是否正常"
  echo "  vscode         — 顯示 VSCode 環境變數設定"
}

# ── 主程式 ──────────────────────────────────────────────────
case "${1:-help}" in
  setup)         cmd_setup ;;
  start)         cmd_start ;;
  stop)          cmd_stop ;;
  restart)       cmd_restart ;;
  logs)          cmd_logs "$@" ;;
  status)        cmd_status ;;
  pull)          cmd_pull "$@" ;;
  list)          cmd_list ;;
  remove-model)  cmd_remove_model "$@" ;;
  index)         cmd_index "$@" ;;
  rag-status)    cmd_rag_status ;;
  test)          cmd_test ;;
  vscode)        cmd_vscode ;;
  help|--help|-h) cmd_help ;;
  *)
    echo "未知指令：$1"
    echo "執行 ./manage.sh help 查看說明"
    exit 1
    ;;
esac
