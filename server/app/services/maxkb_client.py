import httpx
from typing import Any

from ..config import Settings


class MaxKBClient:
    def __init__(self, settings: Settings, api_keys: tuple[str, ...] | None = None):
        self.settings = settings
        self.api_keys = api_keys if api_keys is not None else ((settings.maxkb_api_key,) if settings.maxkb_api_key else ())

    @property
    def configured(self) -> bool:
        return bool(self.settings.maxkb_url and self.api_keys)

    async def retrieve(self, question: str, course_id: str | None = None) -> list[dict[str, Any]] | None:
        """Normalized retrieval records, or None when unconfigured/failed."""
        records = await self._fetch(question, course_id)
        if records is None:
            return None
        normalized: list[dict[str, Any]] = []
        for record in records[: self.settings.maxkb_top_k]:
            text = record.get("content") or record.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            name = record.get("document_name") or record.get("source") or "未命名资料"
            score = record.get("similarity", record.get("score"))
            try:
                score = float(score) if score is not None else None
            except (TypeError, ValueError):
                score = None
            normalized.append({"name": str(name), "text": text.strip(), "score": score})
        return normalized

    async def _fetch(self, question: str, course_id: str | None = None) -> list[dict[str, Any]] | None:
        if not self.configured:
            return None

        payload: dict[str, Any] = {"question": question, "top_k": self.settings.maxkb_top_k}
        course_datasets = dict(self.settings.maxkb_course_dataset_ids)
        if course_datasets:
            if course_id not in course_datasets:
                return None
            dataset_ids = course_datasets[course_id]
            if not dataset_ids:
                return None
        else:
            dataset_ids = self.settings.maxkb_dataset_ids
        if not dataset_ids:
            return None
        if dataset_ids:
            payload["dataset_ids"] = list(dataset_ids)
        for index, api_key in enumerate(self.api_keys):
            try:
                async with httpx.AsyncClient(timeout=self.settings.maxkb_timeout_seconds) as client:
                    response = await client.post(
                        f"{self.settings.maxkb_url}/api/v1/retrieval",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    return self._records(response.json())
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403} and index + 1 < len(self.api_keys):
                    continue
                return None
            except (httpx.HTTPError, ValueError, TypeError):
                return None
        return None

    def _records(self, payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        records = data.get("records", []) if isinstance(data, dict) else []
        return [record for record in records if isinstance(record, dict)]
