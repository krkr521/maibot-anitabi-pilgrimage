"""Read AniTabi's public work and landmark data."""

from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, AsyncIterator, Dict, List, Tuple
from unicodedata import normalize
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import asyncio
import json
import re

API_ROOT = "https://api.anitabi.cn/bangumi"
BANGUMI_SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
MAP_ROOT = "https://anitabi.cn/map"
CACHE_SECONDS = 15 * 60
MAX_CACHED_WORKS = 24
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 4

_POINT_ID = re.compile(r"[a-zA-Z0-9_-]{1,48}")
_NAME_VARIANTS = str.maketrans({"站": "駅", "车": "車", "臺": "台", "\u200b": None})
_SUBJECT_TYPES = {"book": 1, "anime": 2, "music": 3, "game": 4, "real": 6}
_SUBJECT_TYPE_NAMES = {value: name for name, value in _SUBJECT_TYPES.items()}
_USER_AGENT = "krkr521/maibot-anitabi-pilgrimage (https://github.com/krkr521/maibot-anitabi-pilgrimage)"


def _text(value: Any, limit: int = 120) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _search_key(text: str) -> str:
    return "".join(normalize("NFKC", text).casefold().split()).translate(_NAME_VARIANTS)


def _request_json(request: Request) -> Any:
    with urlopen(request, timeout=6) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Lookup response exceeded the size limit")
    return json.loads(body)


def _get_json(subject_id: int, resource: str) -> Any:
    return _request_json(Request(
        f"{API_ROOT}/{subject_id}/{resource}",
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    ))


def _search_subjects(keyword: str, subject_type: str, limit: int) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {"nsfw": False}
    if subject_type:
        filters["type"] = [_SUBJECT_TYPES[subject_type]]
    payload = {"keyword": keyword, "sort": "match", "filter": filters}
    data = _request_json(Request(
        f"{BANGUMI_SEARCH_URL}?limit={limit}&offset=0",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    ))
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Unexpected Bangumi search response")
    subjects = []
    for item in data["data"][:limit]:
        if not isinstance(item, dict) or type(item.get("id")) is not int or not 1 <= item["id"] <= 999999999:
            continue
        name, name_cn = _text(item.get("name")), _text(item.get("name_cn"))
        if not (name or name_cn):
            continue
        kind = item.get("type")
        if type(kind) is not int or kind not in _SUBJECT_TYPE_NAMES:
            continue
        aliases = []
        for info_field in item.get("infobox", []) if isinstance(item.get("infobox"), list) else []:
            if not isinstance(info_field, dict) or info_field.get("key") != "别名":
                continue
            values = info_field.get("value")
            if isinstance(values, str):
                aliases.append(_text(values))
            elif isinstance(values, list):
                aliases.extend(_text(value.get("v")) for value in values if isinstance(value, dict))
        subjects.append({
            "id": item["id"], "name": name, "name_cn": name_cn,
            "type": _SUBJECT_TYPE_NAMES[kind], "date": _text(item.get("date"), 20),
            "aliases": [alias for alias in aliases if alias],
        })
    if data["data"] and not subjects:
        raise ValueError("Bangumi returned no readable subject records")
    return subjects


def _coordinates(value: Any) -> List[float] | None:
    if isinstance(value, list) and len(value) == 2 and all(type(item) in (int, float) for item in value):
        latitude, longitude = value
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return [latitude, longitude]
    return None


def _work_data(subject_id: int, overview: Any, points: Any) -> Dict[str, Any]:
    if not isinstance(overview, dict) or overview.get("id") != subject_id or not isinstance(points, list):
        raise ValueError("Unexpected AniTabi work or landmark data")
    title = _text(overview.get("cn")) or _text(overview.get("title"))
    if not title:
        raise ValueError("Missing AniTabi work title")

    url = f"{MAP_ROOT}?bangumiId={subject_id}"
    lite_points = overview.get("litePoints")
    lite_by_id = {
        row["id"]: row for row in lite_points
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(lite_points, list) else {}
    items = {}
    for row in points:
        if not isinstance(row, dict):
            continue
        point_id = row.get("id")
        name, name_cn = _text(row.get("name")), _text(row.get("cn"))
        if not isinstance(point_id, str) or not _POINT_ID.fullmatch(point_id) or not (name or name_cn):
            continue
        lite = lite_by_id.get(point_id, {})
        item = {
            "name": name or name_cn,
            "name_cn": name_cn or _text(lite.get("cn")),
            "url": f"{url}&pid={point_id}",
            "origin": _text(row.get("origin")),
        }
        geo = _coordinates(row.get("geo")) or _coordinates(lite.get("geo"))
        if geo is not None:
            item["coordinates"] = geo
        for source, target in (("ep", "episode"), ("s", "seconds")):
            value = row.get(source)
            if type(value) is int and value >= 0:
                item[target] = value
        items[point_id] = item
    if points and not items:
        raise ValueError("AniTabi returned no readable landmark records")
    return {
        "subject_id": subject_id,
        "title": title,
        "city": _text(overview.get("city")),
        "url": url,
        "points": list(items.values()),
    }


@dataclass
class _LookupLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class AniTabiClient:
    def __init__(self) -> None:
        self._cache: OrderedDict[int, tuple[float, Dict[str, Any]]] = OrderedDict()
        self._subject_cache: OrderedDict[tuple[str, str, int], tuple[float, List[Dict[str, Any]]]] = OrderedDict()
        self._lookup_locks: Dict[Tuple[Any, ...], _LookupLock] = {}
        self._network_slots = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    @asynccontextmanager
    async def _key_lock(self, key: Tuple[Any, ...]) -> AsyncIterator[None]:
        entry = self._lookup_locks.get(key)
        if entry is None:
            entry = _LookupLock()
            self._lookup_locks[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._lookup_locks[key]

    async def _fetch_json(self, subject_id: int, resource: str) -> Any:
        async with self._network_slots:
            return await asyncio.to_thread(_get_json, subject_id, resource)

    async def lookup_subjects(self, keyword: str, subject_type: str = "", limit: int = 10) -> List[Dict[str, Any]]:
        """Resolve titles through the public Bangumi API without another plugin."""
        key = (keyword, subject_type, limit)
        async with self._key_lock(("title", *key)):
            cached = self._subject_cache.get(key)
            if cached and cached[0] > monotonic():
                self._subject_cache.move_to_end(key)
                return cached[1]
            async with self._network_slots:
                subjects = await asyncio.to_thread(_search_subjects, keyword, subject_type, limit)
            self._subject_cache[key] = (monotonic() + CACHE_SECONDS, subjects)
            self._subject_cache.move_to_end(key)
            if len(self._subject_cache) > MAX_CACHED_WORKS:
                self._subject_cache.popitem(last=False)
            return subjects

    async def _work(self, subject_id: int) -> Dict[str, Any]:
        # Merge requests for this work while other works use their own cache locks.
        async with self._key_lock(("work", subject_id)):
            cached = self._cache.get(subject_id)
            if cached and cached[0] > monotonic():
                self._cache.move_to_end(subject_id)
                return cached[1]
            overview, points = await asyncio.gather(
                self._fetch_json(subject_id, "lite"),
                self._fetch_json(subject_id, "points/detail"),
            )
            work = _work_data(subject_id, overview, points)
            self._cache[subject_id] = (monotonic() + CACHE_SECONDS, work)
            self._cache.move_to_end(subject_id)
            if len(self._cache) > MAX_CACHED_WORKS:
                self._cache.popitem(last=False)
            return work


    async def has_landmarks(self, subject_id: int) -> bool:
        cached = self._cache.get(subject_id)
        if cached and cached[0] > monotonic():
            return bool(cached[1]["points"])
        try:
            overview = await self._fetch_json(subject_id, "lite")
        except HTTPError as exc:
            if exc.code == 404:
                return False
            raise
        count = overview.get("pointsLength") if isinstance(overview, dict) and overview.get("id") == subject_id else None
        if type(count) is not int or count < 0:
            raise ValueError("Unexpected AniTabi landmark count")
        return count > 0

    async def search(self, subject_id: int, keyword: str, limit: int) -> Dict[str, Any]:
        work = await self._work(subject_id)
        key = _search_key(keyword)
        matches = [
            point for point in work["points"]
            if key in _search_key(point["name"]) or key in _search_key(point["name_cn"])
        ]
        return {**work, "points": matches[:limit], "matched": len(matches)}
