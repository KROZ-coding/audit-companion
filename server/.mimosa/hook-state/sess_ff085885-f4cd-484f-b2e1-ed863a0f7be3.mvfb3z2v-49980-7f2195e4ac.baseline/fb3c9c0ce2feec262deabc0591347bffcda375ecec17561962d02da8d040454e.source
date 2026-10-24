"""Local knowledge base: parse documents once, then BM25 keyword retrieval.

MaxKB (vector search) is not available on this machine, so course material is
indexed offline into a single pickle: chunk texts + an inverted index. Retrieval
is BM25 over jieba tokens, which needs no model and no network.
"""

import html
import pickle
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable
from zipfile import ZipFile
from xml.etree import ElementTree as ET

try:
    import jieba

    jieba.setLogLevel(60)
except ImportError:  # pragma: no cover - jieba is optional at runtime
    jieba = None

from ..config import Settings


KB_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("01企业会计准则", "audit_standards"),
    ("02证监会", "audit_standards"),
    ("审计准则汇总", "audit_standards"),
    ("04收集舞弊处罚案例", "audit_cases"),
    ("巨潮资讯网", "audit_cases"),
    ("注册会计师CPA", "cpa_question_bank"),
    ("电子版教材PDF", "audit_textbook"),
    ("财务分析大师知识库", "audit_textbook"),
)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".md", ".txt", ".epub", ".xhtml", ".html"}

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_TAG_RE = re.compile(r"<[^>]+>")

STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "有", "为", "对", "从", "到", "把", "被",
    "我", "你", "他", "它", "这", "那", "什么", "哪些", "哪个", "如何", "怎么", "怎样", "为什么",
    "请", "简述", "说明", "介绍", "解释", "关于", "以及", "可以", "应该", "需要", "进行", "通过",
    "一个", "一种", "这个", "那个", "以上", "以下", "如下", "包括", "其中", "根据", "按照",
    "并", "而", "则", "就", "都", "也", "还", "但", "却", "因", "所以", "如果", "是否",
}


def classify(relative: Path) -> str:
    top = relative.parts[0] if relative.parts else ""
    for prefix, kb in KB_BY_PREFIX:
        if top.startswith(prefix):
            return kb
    return "audit_textbook"


def _docx_text(path: Path) -> str:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    lines: list[str] = []
    for paragraph in root.findall(".//w:body/w:p", namespace):
        parts = [node.text or "" for node in paragraph.iter() if node.tag == f"{{{namespace['w']}}}t"]
        value = "".join(parts).strip()
        if value:
            lines.append(value)
    return "\n".join(lines)


def _pdf_text(path: Path, max_pages: int = 600) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(pages)


def _xlsx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        names = archive.namelist()
        parts: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    parts.append(node.text)
        for name in names:
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                root = ET.fromstring(archive.read(name))
                for node in root.iter():
                    if node.tag.endswith("}t") and node.text:
                        parts.append(node.text)
    return "\n".join(parts)


def _epub_text(path: Path) -> str:
    with ZipFile(path) as archive:
        parts: list[str] = []
        for name in archive.namelist():
            if name.lower().endswith((".html", ".xhtml", ".htm")):
                try:
                    raw = archive.read(name).decode("utf-8", errors="ignore")
                except (KeyError, OSError):
                    continue
                parts.append(html.unescape(_TAG_RE.sub(" ", raw)))
    return "\n".join(parts)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix == ".docx":
        return _docx_text(path)
    if suffix == ".xlsx":
        return _xlsx_text(path)
    if suffix == ".epub":
        return _epub_text(path)
    return path.read_text(encoding="utf-8", errors="ignore")


def _repair_gbk(text: str) -> str:
    """Some PDF fonts report GBK code points as if they were Unicode.

    e.g. U+A3AC is really GBK 0xA3AC, i.e. '，'. Reinterpret only the symbol
    range (U+A1A1–U+A9FE), where Chinese academic text has no legitimate use.
    """
    if not any(0xA1A1 <= ord(ch) <= 0xA9FE for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xA1A1 <= code <= 0xA9FE:
            try:
                out.append(bytes([code >> 8, code & 0xFF]).decode("gbk"))
                continue
            except UnicodeDecodeError:
                pass
        out.append(ch)
    return "".join(out)


def _drop_noise(text: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0xA0:
            out.append(" ")
        elif 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0x10FFFD:
            continue
        elif 0x80 <= code <= 0x9F or code == 0xFFFD:
            continue
        else:
            out.append(ch)
    return "".join(out)


def _clean(text: str) -> str:
    text = _drop_noise(_repair_gbk(text))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("invalid chunk size")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end == len(text):
            break
        start = end - overlap
    return chunks


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        if word.isascii():
            if len(word) > 1 and word.lower() not in STOPWORDS:
                tokens.append(word.lower())
            continue
        if jieba is not None:
            tokens.extend(
                token
                for token in jieba.cut_for_search(word)
                if token.strip() and token not in STOPWORDS and len(token) > 1
            )
        else:
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


class LocalKnowledgeBase:
    """BM25 retrieval over an offline-built index. Loads lazily, never raises."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.chunks: list[dict[str, Any]] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.lengths: list[int] = []
        self.avgdl = 0.0
        self.built_at: str | None = None
        self.source_dir: str | None = None
        self.loaded = False
        self._load_attempted = False
        self._load_lock = Lock()

    @property
    def index_path(self) -> Path:
        configured = self.settings.knowledge_index_path.strip()
        if configured:
            return Path(configured)
        return Path(self.settings.data_dir) / "knowledge" / "index.pkl"

    def load(self) -> bool:
        path = self.index_path
        if not path.is_file():
            return False
        try:
            with path.open("rb") as handle:
                payload = pickle.load(handle)
            self.chunks = payload["chunks"]
            self.postings = payload["postings"]
            self.lengths = payload["lengths"]
            self.avgdl = payload["avgdl"]
            self.built_at = payload.get("built_at")
            self.source_dir = payload.get("source_dir")
            self.loaded = True
            return True
        except (OSError, KeyError, pickle.UnpicklingError, EOFError, ValueError):
            return False

    def ensure_loaded(self) -> bool:
        """Load on first use so startup and tests stay cheap."""
        if self.loaded:
            return True
        # 并发首查时必须串行：加载完成前到达的请求要等待，而不是误判为无索引。
        with self._load_lock:
            if self.loaded:
                return True
            if getattr(self, "_load_attempted", False):
                return False
            self._load_attempted = True
            return self.load()

    def search(self, query: str, top_k: int = 4, kb: str | None = None) -> list[dict[str, Any]]:
        if not self.ensure_loaded():
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        k1, b = 1.5, 0.75
        total = len(self.chunks)
        scores: dict[int, float] = {}
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            postings = self.postings.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = max(0.0, (total - df + 0.5) / (df + 0.5))
            for chunk_id, tf in postings:
                if kb is not None and self.chunks[chunk_id].get("kb") != kb:
                    continue
                length = self.lengths[chunk_id] or 1
                denom = tf + k1 * (1 - b + b * length / (self.avgdl or 1))
                scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * tf * (k1 + 1) / denom
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        top = ranked[0][1] or 1.0
        return [
            {
                "name": self.chunks[chunk_id]["src"],
                "text": self.chunks[chunk_id]["text"],
                "score": round(score / top, 4),
                "kb": self.chunks[chunk_id].get("kb"),
            }
            for chunk_id, score in ranked
        ]

    def stats(self) -> dict[str, Any]:
        self.ensure_loaded()
        return {
            "loaded": self.loaded,
            "chunks": len(self.chunks),
            "built_at": self.built_at,
            "source_dir": self.source_dir,
            "index_path": str(self.index_path),
        }


def build_index(
    source_dir: Path,
    index_path: Path,
    progress: Callable[[str], None] | None = None,
    suffixes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Walk `source_dir`, extract text, chunk, tokenize and persist the index."""
    wanted = {s.lower() for s in (suffixes or SUPPORTED_SUFFIXES)}
    files = sorted(
        path for path in source_dir.rglob("*") if path.is_file() and path.suffix.lower() in wanted
    )
    chunks: list[dict[str, Any]] = []
    skipped: list[str] = []
    started = time.time()
    for number, path in enumerate(files, start=1):
        try:
            text = _clean(extract_text(path))
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the build
            skipped.append(f"{path.name}: {type(exc).__name__}")
            continue
        if not text:
            skipped.append(f"{path.name}: empty")
            continue
        relative = path.relative_to(source_dir)
        kb = classify(relative)
        for piece in chunk_text(text):
            chunks.append({"src": str(relative).replace("\\", "/"), "kb": kb, "text": piece})
        if progress and (number % 25 == 0 or number == len(files)):
            progress(f"  {number}/{len(files)} files, {len(chunks)} chunks, {time.time()-started:.0f}s")
    result = _write_index(chunks, index_path, source_dir=str(source_dir))
    result["files"] = len(files)
    result["skipped"] = skipped
    result["elapsed_seconds"] = round(time.time() - started, 1)
    return result


def reindex_existing(index_path: Path, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Re-tokenize already-extracted chunks without re-parsing any document."""
    with index_path.open("rb") as handle:
        payload = pickle.load(handle)
    chunks = payload["chunks"]
    if progress:
        progress(f"  复用 {len(chunks)} 个知识块，仅重建倒排索引…")
    started = time.time()
    result = _write_index(chunks, index_path, source_dir=payload.get("source_dir") or "")
    result["files"] = 0
    result["skipped"] = []
    result["elapsed_seconds"] = round(time.time() - started, 1)
    return result


def _write_index(chunks: list[dict[str, Any]], index_path: Path, *, source_dir: str) -> dict[str, Any]:
    postings: dict[str, list[tuple[int, int]]] = {}
    lengths: list[int] = []
    for chunk_id, chunk in enumerate(chunks):
        tokens = tokenize(chunk["text"])
        lengths.append(len(tokens) or 1)
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for token, tf in counts.items():
            postings.setdefault(token, []).append((chunk_id, tf))
    payload = {
        "version": 1,
        "chunks": chunks,
        "postings": postings,
        "lengths": lengths,
        "avgdl": (sum(lengths) / len(lengths)) if lengths else 0.0,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_dir": source_dir,
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(index_path)
    return {"chunks": len(chunks), "tokens": len(postings), "index_path": str(index_path)}
