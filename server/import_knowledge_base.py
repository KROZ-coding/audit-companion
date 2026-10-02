"""Register 审计学知识库 files as platform documents.

`build_knowledge_index.py` makes the material searchable in chat (BM25 index).
This script makes the *same* files visible in 「资料库导入」, which reads
`store.documents`. Run it once after the index exists; it is idempotent (files
already registered by `source_path` are skipped).

    .\\.venv\\Scripts\\python.exe import_knowledge_base.py "..\\审计学知识库"
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import Settings, settings  # noqa: E402
from app.services.knowledge_base import LocalKnowledgeBase, SUPPORTED_SUFFIXES, classify  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1:
        source = Path(sys.argv[1]).resolve()
    elif settings.knowledge_dir:
        source = Path(settings.knowledge_dir).resolve()
    else:
        source = (Path(__file__).resolve().parent.parent / "审计学知识库").resolve()

    if not source.is_dir():
        print(f"知识库目录不存在：{source}")
        return 1

    # This script must persist even when called directly, without the launcher.
    runtime_settings = Settings(data_dir=settings.data_dir, persistence_enabled=True)
    store = Store(runtime_settings)
    owner = next((u for u in store.users.values() if u.username == "teacher01"), None)
    owner = owner or next((u for u in store.users.values() if u.role == "admin"), None)
    if owner is None:
        print("没有可用用户，请先启动一次服务生成种子数据。")
        return 1

    files = sorted(
        path for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        print(f"目录下没有可导入的文件：{source}")
        return 1

    index = LocalKnowledgeBase(runtime_settings)
    indexed_sources: set[str] = set()
    if index.load():
        indexed_sources = {str(item.get("src", "")).replace("\\", "/") for item in index.chunks}

    registered = {item.get("source_path") for item in store.documents.values()}
    by_kb: dict[str, list[tuple[Path, Path]]] = defaultdict(list)
    for path in files:
        by_kb[classify(path.relative_to(source))].append((path, path.relative_to(source)))

    added_docs = 0
    for kb, entries in sorted(by_kb.items()):
        for path, rel in entries:
            key = str(rel).replace("\\", "/")
            if key in registered:
                existing = next((item for item in store.documents.values() if item.get("source_path") == key), None)
                if existing is not None:
                    existing["shared"] = True
                    existing["vector_status"] = "ready" if key in indexed_sources else "failed"
                continue
            document_id = str(uuid4())
            stat = path.stat()
            store.documents[document_id] = {
                "id": document_id, "name": path.name, "target_kb": kb,
                "size": stat.st_size,
                "vector_status": "ready" if key in indexed_sources else "failed",
                "file_key": None, "source_path": key, "shared": True,
                "uploaded_by": owner.id,
                "uploaded_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
            added_docs += 1

    # Refresh status for documents registered by an earlier run.
    for document in store.documents.values():
        source_path = document.get("source_path")
        if source_path:
            document["shared"] = True
            document["vector_status"] = "ready" if source_path in indexed_sources else "failed"

    if not store.save():
        print("保存失败：store 无法写入。")
        return 1

    print(f"完成：新增 {added_docs} 份资料")
    print(f"documents 共 {len(store.documents)} 份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
