from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile


def validate_uploaded_content(suffix: str, content: bytes, max_uncompressed_bytes: int = 100 * 1024 * 1024) -> None:
    if suffix == ".md":
        content.decode("utf-8-sig")
        return
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("invalid pdf")
        return
    if suffix == ".docx":
        try:
            with ZipFile(BytesIO(content)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("document.xml missing")
                if sum(item.file_size for item in archive.infolist()) > max_uncompressed_bytes:
                    raise ValueError("archive expands beyond limit")
                if any(Path(item.filename).is_absolute() or ".." in Path(item.filename).parts for item in archive.infolist()):
                    raise ValueError("unsafe archive path")
        except BadZipFile as exc:
            raise ValueError("invalid docx") from exc
        return
    raise ValueError("unsupported file")
