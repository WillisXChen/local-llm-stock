#!/bin/bash
# ============================================================
#  自動拉取模型腳本
#  由 model-puller container 執行，Ollama 服務就緒後自動下載
# ============================================================

set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://ollama:11434}"

echo "⏳ 等待 Ollama 服務就緒..."
until curl -sf "${OLLAMA_HOST}/api/version" > /dev/null 2>&1; do
  sleep 3
done
echo "✅ Ollama 服務就緒！"

# 要自動拉取的模型清單（按優先順序）
# 可以根據需要調整此清單
MODELS=(
  "nomic-embed-text"          # 274MB — RAG 向量嵌入模型（必要）
  "nemotron-3-nano"           # 6GB  — 快速日常
  "codegemma:7b"              # 6GB  — Google coding
  "gemma2:9b"                 # 8GB  — Google 通用
  "nemotron-3-super"          # 16GB — 主力推薦
  "gemma2:27b"                # 18GB — 高品質
  # "nemotron"                # 40GB — 旗艦（選填，很大）
)

echo ""
echo "📦 開始下載模型..."
echo "─────────────────────────────────────────"

for MODEL in "${MODELS[@]}"; do
  # 跳過被 # 標記的模型
  [[ "$MODEL" =~ ^#.* ]] && continue

  echo ""
  echo "⬇  正在下載: $MODEL"
  OLLAMA_HOST="$OLLAMA_HOST" ollama pull "$MODEL" && \
    echo "✅ $MODEL 下載完成" || \
    echo "⚠️  $MODEL 下載失敗，跳過"
done

echo ""
echo "─────────────────────────────────────────"
echo "🎉 模型下載完畢！已安裝的模型："
OLLAMA_HOST="$OLLAMA_HOST" ollama list
