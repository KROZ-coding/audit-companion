"""Build the local knowledge base index.

Usage (from the server directory):
    .\\.venv\\Scripts\\python.exe build_knowledge_index.py "..\\审计学知识库"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402
from app.services.knowledge_base import LocalKnowledgeBase, build_index, reindex_existing  # noqa: E402


def main() -> int:
    index_path = LocalKnowledgeBase(settings).index_path

    if "--reindex" in sys.argv:
        if not index_path.is_file():
            print(f"索引不存在，请先完整构建：{index_path}")
            return 1
        print(f"仅重建倒排索引：{index_path}")
        result = reindex_existing(index_path, progress=print)
        print(f"完成：{result['chunks']} 个知识块，{result['tokens']} 个词条，"
              f"用时 {result['elapsed_seconds']}s")
        return 0

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        source = Path(args[0]).resolve()
    elif settings.knowledge_dir:
        source = Path(settings.knowledge_dir).resolve()
    else:
        source = (Path(__file__).resolve().parent.parent / "审计学知识库").resolve()

    if not source.is_dir():
        print(f"知识库目录不存在：{source}")
        return 1

    print(f"源目录：{source}")
    print(f"索引输出：{index_path}")
    print("开始解析（PDF 较慢，请耐心等待）…")

    result = build_index(source, index_path, progress=print)
    print()
    print(f"完成：{result['files']} 个文件 → {result['chunks']} 个知识块，"
          f"{result['tokens']} 个词条，用时 {result['elapsed_seconds']}s")
    if result["skipped"]:
        print(f"跳过 {len(result['skipped'])} 个文件：")
        for item in result["skipped"][:20]:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
