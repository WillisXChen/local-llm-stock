"""
Codebase Indexer — 將程式碼切塊、嵌入、存入 Qdrant

Usage:
    python indexer.py <directory> [--project NAME] [--collection NAME] [--chunk-lines N] [--overlap N]

Multi-user / per-project 使用方式：
    # 為 xse-apps 前端專案建立獨立 collection
    python indexer.py /path/to/xse-apps --project xse-apps

    # 為 xse-api 後端專案建立獨立 collection
    python indexer.py /path/to/xse-api --project xse-api

    # 自訂 collection 名稱（覆蓋 prefix 邏輯）
    python indexer.py /path/to/repo --collection my-custom-name

Collection 命名規則：
    --project xse-apps  =>  codebase-xse-apps
    --collection foo    =>  foo（直接使用）
    預設               =>  codebase
"""

import os
import sys
import argparse
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

# ── Config ──────────────────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "codebase")

# File extensions to index
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs",
    ".html", ".css", ".scss", ".less",
    ".sql", ".graphql",
    ".yaml", ".yml", ".toml", ".json",
    ".md", ".txt", ".rst",
    ".sh", ".bash", ".zsh",
    ".tf", ".hcl",
}

# Special filenames (no extension)
CODE_FILENAMES = {
    "dockerfile", "makefile", "rakefile", "gemfile",
    "procfile", "vagrantfile", "justfile",
}

# Directories to skip
IGNORE_DIRS = {
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".idea", ".vscode", "coverage", ".pytest_cache", ".tox",
    ".eggs", "*.egg-info", ".mypy_cache", ".ruff_cache",
    "bower_components", ".terraform",
}

# Max file size to index (skip large generated files)
MAX_FILE_SIZE = 100_000  # 100KB


def should_index(path: Path) -> bool:
    """Check if a file should be indexed."""
    # Skip ignored directories
    if any(part in IGNORE_DIRS for part in path.parts):
        return False
    # Skip hidden files
    if any(part.startswith(".") for part in path.parts[1:] if part != "."):
        return False
    # Skip large files
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    # Check extension or filename
    if path.suffix.lower() in CODE_EXTENSIONS:
        return True
    if path.name.lower() in CODE_FILENAMES:
        return True
    return False


def chunk_file(file_path: Path, base_dir: Path, chunk_lines: int, overlap: int) -> list[dict]:
    """Split a file into overlapping chunks."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    lines = content.split("\n")
    if not lines or not content.strip():
        return []

    rel_path = str(file_path.relative_to(base_dir))
    chunks = []

    if len(lines) <= chunk_lines:
        # Small file: single chunk
        chunks.append({
            "file_path": rel_path,
            "start_line": 1,
            "end_line": len(lines),
            "content": content,
            "language": file_path.suffix.lstrip(".") or "text",
        })
    else:
        # Large file: sliding window chunks
        start = 0
        while start < len(lines):
            end = min(start + chunk_lines, len(lines))
            chunk_content = "\n".join(lines[start:end])
            chunks.append({
                "file_path": rel_path,
                "start_line": start + 1,
                "end_line": end,
                "content": chunk_content,
                "language": file_path.suffix.lstrip(".") or "text",
            })
            if end >= len(lines):
                break
            start += chunk_lines - overlap

    return chunks


def get_embedding(text: str) -> list[float]:
    """Get embedding from Ollama."""
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text[:8000]},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Get embeddings for multiple texts (Ollama supports batch)."""
    with httpx.Client(timeout=120) as client:
        resp = client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": [t[:8000] for t in texts]},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]


def main():
    parser = argparse.ArgumentParser(description="Index codebase into Qdrant")
    parser.add_argument("directory", help="Directory to index")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project",
                       help=f"Project name — collection becomes '{COLLECTION_PREFIX}-<name>' (recommended for multi-user)")
    group.add_argument("--collection", default=None,
                       help="Qdrant collection name (overrides --project)")
    parser.add_argument("--chunk-lines", type=int, default=60,
                        help="Lines per chunk (default: 60)")
    parser.add_argument("--overlap", type=int, default=10,
                        help="Overlap lines between chunks (default: 10)")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Embedding batch size (default: 10)")
    args = parser.parse_args()

    target_dir = Path(args.directory)
    if not target_dir.is_dir():
        print(f"Error: {target_dir} is not a directory")
        sys.exit(1)

    # Resolve collection name: --collection > --project > env > default
    if args.collection:
        collection = args.collection
    elif args.project:
        safe_id = "".join(c for c in args.project if c.isalnum() or c in "-_").lower()
        collection = f"{COLLECTION_PREFIX}-{safe_id}"
    else:
        collection = os.getenv("QDRANT_COLLECTION", "codebase")

    # ── Scan files ──────────────────────────────────────────────
    print(f"🔍 Scanning {target_dir}...")
    files = sorted(f for f in target_dir.rglob("*") if f.is_file() and should_index(f))
    print(f"📄 Found {len(files)} files to index")

    if not files:
        print("No indexable files found!")
        sys.exit(0)

    # ── Chunk files ─────────────────────────────────────────────
    all_chunks = []
    for f in files:
        all_chunks.extend(chunk_file(f, target_dir, args.chunk_lines, args.overlap))
    print(f"📦 Split into {len(all_chunks)} chunks")

    if not all_chunks:
        print("No chunks to index!")
        sys.exit(0)

    # ── Test embedding model ────────────────────────────────────
    print(f"🧠 Testing embedding model: {EMBED_MODEL}...")
    try:
        test_emb = get_embedding("test")
        dim = len(test_emb)
        print(f"   Embedding dimension: {dim}")
    except Exception as e:
        print(f"❌ Embedding model not available: {e}")
        print(f"   Make sure '{EMBED_MODEL}' is pulled in Ollama")
        sys.exit(1)

    # ── Create Qdrant collection ────────────────────────────────
    client = QdrantClient(url=QDRANT_URL, timeout=30)

    existing = [c.name for c in client.get_collections().collections]
    if collection in existing:
        print(f"🗑  Deleting existing collection: {collection}")
        client.delete_collection(collection)

    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    print(f"✅ Created collection: {collection}")

    # ── Embed & Index ───────────────────────────────────────────
    print(f"\n⬆  Indexing {len(all_chunks)} chunks (batch size: {args.batch_size})...")

    total_indexed = 0
    batch_texts = []
    batch_chunks = []

    for i, chunk in enumerate(all_chunks):
        batch_texts.append(chunk["content"])
        batch_chunks.append(chunk)

        if len(batch_texts) >= args.batch_size or i == len(all_chunks) - 1:
            # Get embeddings for batch
            try:
                embeddings = get_embeddings_batch(batch_texts)
            except Exception as e:
                print(f"\n⚠️  Batch embedding failed at chunk {i}: {e}")
                # Fallback to one-by-one
                embeddings = []
                for t in batch_texts:
                    try:
                        embeddings.append(get_embedding(t))
                    except Exception:
                        embeddings.append(None)

            # Create points
            points = []
            for j, (emb, ch) in enumerate(zip(embeddings, batch_chunks)):
                if emb is None:
                    continue
                points.append(PointStruct(
                    id=total_indexed + j,
                    vector=emb,
                    payload=ch,
                ))

            if points:
                client.upsert(collection_name=collection, points=points)

            total_indexed += len(batch_texts)
            pct = (total_indexed / len(all_chunks)) * 100
            last_file = batch_chunks[-1]["file_path"]
            print(f"\r   [{pct:5.1f}%] {total_indexed}/{len(all_chunks)} — {last_file}", end="", flush=True)

            batch_texts = []
            batch_chunks = []

    print(f"\n\n🎉 Indexing complete!")
    print(f"   Collection : {collection}")
    print(f"   Chunks     : {total_indexed}")
    print(f"   Files      : {len(files)}")
    print(f"   Dimension  : {dim}")

    # Show top 5 largest files indexed
    print(f"\n📊 Largest files indexed:")
    file_chunks = {}
    for ch in all_chunks:
        fp = ch["file_path"]
        file_chunks[fp] = file_chunks.get(fp, 0) + 1
    for fp, count in sorted(file_chunks.items(), key=lambda x: -x[1])[:5]:
        print(f"   {count:3d} chunks — {fp}")


if __name__ == "__main__":
    main()
