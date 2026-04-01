"""
RAG Proxy — 在 Claude Code 與 LiteLLM 之間注入向量搜尋結果
接收 Anthropic Messages API 格式請求，從 Qdrant 檢索相關程式碼，
注入 system prompt 後轉發到 LiteLLM。

多人隔離機制：
  - API Key 驗證：每位團隊成員需帶 X-API-Key header 才能使用
  - Per-project collection：透過 key 綁定或 X-Project-Id header 路由到獨立 Qdrant collection
  - GPU 請求排隊：使用 asyncio.Semaphore 限制同時推理數量，避免 GPU 過載
"""

import os
import json
import asyncio
import logging
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse, JSONResponse
from qdrant_client import QdrantClient
from qdrant_client.models import ScoredPoint

# ── Config ──────────────────────────────────────────────────────
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "codebase")
DEFAULT_COLLECTION = os.getenv("QDRANT_COLLECTION", "codebase")
TOP_K = int(os.getenv("RAG_TOP_K", "5"))
MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.3"))
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"

# ── Queue Config ────────────────────────────────────────────────
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))
QUEUE_TIMEOUT = int(os.getenv("QUEUE_TIMEOUT", "300"))  # seconds

# ── API Key Config ──────────────────────────────────────────────
API_KEYS_FILE = os.getenv("API_KEYS_FILE", "/app/config/api_keys.json")
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [RAG] %(message)s")
log = logging.getLogger("rag-proxy")

app = FastAPI(title="RAG Proxy for Claude Code")

# ── API Key Store ───────────────────────────────────────────────
_api_keys: dict | None = None
_api_keys_mtime: float = 0


def load_api_keys() -> dict:
    """Load API keys from JSON file. Auto-reloads when file changes."""
    global _api_keys, _api_keys_mtime

    try:
        mtime = os.path.getmtime(API_KEYS_FILE)
    except OSError:
        if _api_keys is None:
            log.warning(f"API keys file not found: {API_KEYS_FILE}")
            _api_keys = {}
        return _api_keys

    if _api_keys is None or mtime > _api_keys_mtime:
        with open(API_KEYS_FILE) as f:
            data = json.load(f)
        _api_keys = data.get("keys", {})
        _api_keys_mtime = mtime
        enabled = sum(1 for v in _api_keys.values() if v.get("enabled", True))
        log.info(f"Loaded {enabled} API keys from {API_KEYS_FILE}")

    return _api_keys


def validate_api_key(request: Request) -> dict | None:
    """Validate X-API-Key header. Returns key config or None."""
    if not AUTH_ENABLED:
        return {"user_id": request.headers.get("x-user-id", "anonymous"), "project_id": None}

    api_key = request.headers.get("x-api-key", "").strip()
    if not api_key:
        return None

    keys = load_api_keys()
    key_config = keys.get(api_key)
    if key_config is None:
        return None
    if not key_config.get("enabled", True):
        return None

    return key_config

# ── GPU Request Queue (Semaphore) ──────────────────────────────
_gpu_semaphore: asyncio.Semaphore | None = None
_queue_waiting = 0  # track how many requests are waiting


def get_semaphore() -> asyncio.Semaphore:
    global _gpu_semaphore
    if _gpu_semaphore is None:
        _gpu_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        log.info(f"GPU queue initialized: max_concurrent={MAX_CONCURRENT}, timeout={QUEUE_TIMEOUT}s")
    return _gpu_semaphore


# ── Qdrant Client (lazy init) ──────────────────────────────────
_qdrant: QdrantClient | None = None


def get_qdrant() -> QdrantClient | None:
    global _qdrant
    if _qdrant is None:
        try:
            _qdrant = QdrantClient(url=QDRANT_URL, timeout=10)
            _qdrant.get_collections()  # test connection
        except Exception as e:
            log.warning(f"Qdrant not available: {e}")
            _qdrant = None
    return _qdrant


# ── Collection Resolution ──────────────────────────────────────
def resolve_collection(request: Request, key_config: dict | None = None) -> str:
    """Resolve Qdrant collection name.

    Priority:
      1. X-Project-Id header (explicit override)
      2. API key config's project_id (per-user default)
      3. DEFAULT_COLLECTION fallback
    """
    # Header takes priority (allows user to switch projects)
    project_id = request.headers.get("x-project-id", "").strip()

    # Fallback to key config's default project
    if not project_id and key_config:
        project_id = key_config.get("project_id", "") or ""

    if project_id:
        # Sanitize: only allow alphanumeric, hyphens, underscores
        safe_id = "".join(c for c in project_id if c.isalnum() or c in "-_").lower()
        if safe_id:
            return f"{COLLECTION_PREFIX}-{safe_id}"
    return DEFAULT_COLLECTION


# ── Embedding ───────────────────────────────────────────────────
async def get_embedding(text: str) -> list[float] | None:
    """Call Ollama embedding API."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": text[:8000]},
            )
            resp.raise_for_status()
            return resp.json()["embeddings"][0]
    except Exception as e:
        log.warning(f"Embedding failed: {e}")
        return None


# ── RAG Search ──────────────────────────────────────────────────
async def search_context(query: str, collection: str) -> str:
    """Search Qdrant for relevant code chunks and format as context."""
    qdrant = get_qdrant()
    if qdrant is None:
        return ""

    embedding = await get_embedding(query)
    if embedding is None:
        return ""

    try:
        # Check if collection exists
        collections = [c.name for c in qdrant.get_collections().collections]
        if collection not in collections:
            log.info(f"Collection '{collection}' not found, skipping RAG")
            return ""

        results = qdrant.query_points(
            collection_name=collection,
            query=embedding,
            limit=TOP_K,
            with_payload=True,
        )

        if not results.points:
            return ""

        chunks = []
        for point in results.points:
            if point.score < MIN_SCORE:
                continue
            p = point.payload
            file_path = p.get("file_path", "?")
            start_line = p.get("start_line", "?")
            end_line = p.get("end_line", "?")
            content = p.get("content", "")
            score = f"{point.score:.2f}"
            chunks.append(
                f"### {file_path} (lines {start_line}-{end_line}, relevance: {score})\n"
                f"```\n{content}\n```"
            )

        if not chunks:
            return ""

        log.info(f"[{collection}] Found {len(chunks)} relevant chunks (top score: {results.points[0].score:.2f})")
        return "\n\n".join(chunks)

    except Exception as e:
        log.warning(f"Qdrant search failed: {e}")
        return ""


# ── Message Parsing ─────────────────────────────────────────────
def extract_query(messages: list) -> str:
    """Extract text from the last user message."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content[:2000]
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            return " ".join(texts)[:2000]
    return ""


def inject_context(body: dict, context: str) -> dict:
    """Inject RAG context into the system prompt."""
    rag_block = (
        "\n\n<retrieved_context>\n"
        "The following code snippets were retrieved from the codebase via vector search "
        "and may be relevant to the user's request. Use them as additional reference:\n\n"
        f"{context}\n"
        "</retrieved_context>"
    )

    system = body.get("system", "")

    if isinstance(system, str):
        body["system"] = (system + rag_block) if system else rag_block.strip()
    elif isinstance(system, list):
        # Anthropic format: list of content blocks
        body["system"].append({"type": "text", "text": rag_block})
    else:
        body["system"] = rag_block.strip()

    return body


# ── Health（不需驗證）─────────────────────────────────────────
@app.get("/health")
async def health():
    qdrant_ok = get_qdrant() is not None
    sem = get_semaphore()
    return {
        "status": "healthy",
        "auth_enabled": AUTH_ENABLED,
        "rag_enabled": RAG_ENABLED,
        "qdrant_connected": qdrant_ok,
        "embed_model": EMBED_MODEL,
        "default_collection": DEFAULT_COLLECTION,
        "collection_prefix": COLLECTION_PREFIX,
        "top_k": TOP_K,
        "queue": {
            "max_concurrent": MAX_CONCURRENT,
            "available_slots": sem._value,
            "waiting": _queue_waiting,
            "timeout": QUEUE_TIMEOUT,
        },
    }


# ── Queue Status ────────────────────────────────────────────────
@app.get("/queue/status")
async def queue_status():
    """Return current GPU queue status for monitoring."""
    sem = get_semaphore()
    return {
        "max_concurrent": MAX_CONCURRENT,
        "available_slots": sem._value,
        "waiting": _queue_waiting,
        "timeout": QUEUE_TIMEOUT,
    }


# ── Catch-all Proxy ─────────────────────────────────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(request: Request, path: str):
    global _queue_waiting

    # ── API Key 驗證 ────────────────────────────────────────────
    key_config = validate_api_key(request)
    if key_config is None:
        log.warning(f"[auth] Rejected request: missing or invalid X-API-Key from {request.client.host}")
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "type": "authentication_error",
                    "message": "Invalid or missing X-API-Key. Contact your admin to get a key.",
                }
            },
        )

    user_id = key_config.get("user_id", "anonymous")

    body_bytes = await request.body()

    # Build forwarding headers (drop hop-by-hop + internal headers)
    headers = {}
    for k, v in request.headers.items():
        if k.lower() not in ("host", "content-length", "transfer-encoding", "x-project-id", "x-api-key"):
            headers[k] = v

    # Resolve per-project collection (key config -> header -> default)
    collection = resolve_collection(request, key_config)

    # ── RAG injection (only for POST /v1/messages) ──────────────
    if request.method == "POST" and "messages" in path and RAG_ENABLED:
        try:
            body = json.loads(body_bytes)
            query = extract_query(body.get("messages", []))
            if query:
                context = await search_context(query, collection)
                if context:
                    body = inject_context(body, context)
                    log.info(f"[{collection}] Injected {len(context)} chars of context for query: {query[:80]}...")
                else:
                    log.debug("No relevant context found")
            body_bytes = json.dumps(body).encode()
        except json.JSONDecodeError:
            pass  # Not JSON, forward as-is
        except Exception as e:
            log.error(f"RAG injection error: {e}")
            # Forward original request on error

    # ── Detect streaming ────────────────────────────────────────
    is_stream = False
    try:
        is_stream = json.loads(body_bytes).get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        pass

    target_url = f"{LITELLM_URL}/{path}"

    # ── GPU Queue: acquire semaphore before forwarding ──────────
    sem = get_semaphore()
    _queue_waiting += 1
    log.info(f"[queue] {user_id} waiting (queue_depth={_queue_waiting}, available={sem._value})")

    try:
        await asyncio.wait_for(sem.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        _queue_waiting -= 1
        log.warning(f"[queue] {user_id} timed out after {QUEUE_TIMEOUT}s")
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "type": "rate_limit_error",
                    "message": f"GPU queue full. {_queue_waiting} requests waiting. Try again later.",
                }
            },
        )

    _queue_waiting -= 1
    start_time = time.monotonic()
    log.info(f"[queue] {user_id} acquired slot (available={sem._value})")

    try:
        if is_stream:
            # Stream response through
            async def stream_generator():
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                        async with client.stream(
                            request.method,
                            target_url,
                            content=body_bytes,
                            headers=headers,
                        ) as resp:
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                finally:
                    elapsed = time.monotonic() - start_time
                    log.info(f"[queue] {user_id} released slot after {elapsed:.1f}s (stream)")
                    sem.release()

            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Regular response
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                    resp = await client.request(
                        request.method,
                        target_url,
                        content=body_bytes,
                        headers=headers,
                    )
                    # Pass through response headers
                    resp_headers = {}
                    for k, v in resp.headers.items():
                        if k.lower() not in ("content-length", "transfer-encoding", "content-encoding"):
                            resp_headers[k] = v

                    return Response(
                        content=resp.content,
                        status_code=resp.status_code,
                        headers=resp_headers,
                    )
            finally:
                elapsed = time.monotonic() - start_time
                log.info(f"[queue] {user_id} released slot after {elapsed:.1f}s")
                sem.release()
    except Exception:
        # Ensure semaphore is released on unexpected errors (non-stream path)
        if not is_stream:
            sem.release()
        raise
