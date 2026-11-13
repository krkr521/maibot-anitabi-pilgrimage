"""AniTabi lookup tool and read-only plugin API."""

from typing import Any, Dict, List
from unicodedata import normalize
from urllib.error import HTTPError
import asyncio

from maibot_sdk import API, Field, MaiBotPlugin, PluginConfigBase, Tool

from .client import AniTabiClient


SUBJECT_TYPES = {"anime": "动画", "book": "书籍", "game": "游戏", "music": "音乐", "real": "真人影视"}


class PluginSettings(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用 AniTabi 巡礼地点查询。")
    config_version: str = Field(default="1.0.0", description="配置版本。")


class SearchSettings(PluginConfigBase):
    default_subject_id: int = Field(default=0, ge=0, le=999999999, description="兼容旧版插件 API 的默认作品 ID。0 表示由调用方指定作品。")


class AniTabiSettings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)


def _failure(error: str, content: str) -> Dict[str, Any]:
    return {"success": False, "error": error, "content": content, "data": None}


def _title_key(value: str) -> str:
    return "".join(char for char in normalize("NFKC", value).casefold() if char.isalnum())


def _title_keys(subject: Dict[str, Any]) -> List[str]:
    titles = [subject.get("name"), subject.get("name_cn")]
    aliases = subject.get("aliases")
    if isinstance(aliases, list):
        titles.extend(aliases)
    return [_title_key(title) for title in titles if isinstance(title, str) and title]


def _ambiguous_work(subjects: List[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = subjects[:6]
    lines = ["Bangumi 找到多个可能的作品，请按名称、年份或类型选一个。"]
    for subject in candidates:
        name = subject.get("name_cn") or subject.get("name")
        kind = SUBJECT_TYPES.get(subject.get("type"), subject.get("type") or "")
        year = (subject.get("date") or "")[:4]
        details = " · ".join(part for part in (kind, year) if part)
        lines.append(f'- {name}（{details}） https://bgm.tv/subject/{subject["id"]}')
    return {
        **_failure("ambiguous_work", "\n".join(lines)),
        "data": {"subjects": candidates},
    }


def _point_text(point: Dict[str, Any]) -> str:
    name = point["name"]
    if point["name_cn"] and point["name_cn"] != name:
        name = f'{point["name_cn"]}（{name}）'
    details = []
    if "episode" in point:
        details.append(f'第 {point["episode"]} 集')
    if "seconds" in point:
        minutes, seconds = divmod(point["seconds"], 60)
        details.append(f"片中 {minutes}:{seconds:02d}")
    if "coordinates" in point:
        latitude, longitude = point["coordinates"]
        details.append(f"坐标 {latitude}, {longitude}")
    if point["origin"]:
        details.append(f'标注来源 {point["origin"]}')
    suffix = f"（{'，'.join(details)}）" if details else ""
    return f'- {name}{suffix} {point["url"]}'


class AniTabiPlugin(MaiBotPlugin):
    config_model = AniTabiSettings

    def __init__(self) -> None:
        super().__init__()
        self._client = AniTabiClient()

    async def on_load(self) -> None:
        self.ctx.logger.info("AniTabi 动画巡礼已就绪")

    async def on_unload(self) -> None:
        self.ctx.logger.info("AniTabi 动画巡礼已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("AniTabi 动画巡礼配置已更新")

    async def _resolve_title(self, work_name: str, subject_type: str) -> int | Dict[str, Any]:
        try:
            subjects = await asyncio.wait_for(
                self._client.lookup_subjects(work_name, subject_type, limit=10),
                timeout=12,
            )
        except (OSError, ValueError, TimeoutError) as exc:
            self.ctx.logger.warning(f"AniTabi 查询 Bangumi 作品失败：{exc}")
            return _failure("bangumi_unavailable", "Bangumi 作品搜索暂时不可用。")

        unique = {
            subject["id"]: subject for subject in subjects
            if isinstance(subject, dict) and type(subject.get("id")) is int and 1 <= subject["id"] <= 999999999
        }
        if not unique:
            return _failure("work_not_found", f"Bangumi 没有找到与「{work_name}」匹配的作品。")

        query = _title_key(work_name)
        named = [(subject, _title_keys(subject)) for subject in unique.values()]
        exact = [subject for subject, keys in named if query in keys]
        partial = [subject for subject, keys in named if any(query in key for key in keys)]
        candidates = exact or partial or list(unique.values())
        if len(candidates) == 1:
            return candidates[0]["id"]

        # A shared title may refer to both an adaptation and its source work.
        # Check coverage only for a small set of exact title matches.
        if 1 < len(exact) <= 4:
            try:
                coverage = await asyncio.gather(*(self._client.has_landmarks(subject["id"]) for subject in exact))
            except (OSError, ValueError) as exc:
                self.ctx.logger.warning(f"AniTabi 同名作品收录查询失败：{exc}")
                return _failure("unavailable", "AniTabi 暂时无法确认同名作品的点位收录情况。")
            candidates = [subject for subject, has_points in zip(exact, coverage, strict=True) if has_points]
            if len(candidates) == 1:
                return candidates[0]["id"]
            if not candidates:
                return _failure("not_found", f"AniTabi 暂无「{work_name}」同名条目的巡礼资料。")
            if not subject_type:
                animated = [subject for subject in candidates if subject.get("type") == "anime"]
                if len(animated) == 1:
                    return animated[0]["id"]
        return _ambiguous_work(candidates)

    @API(
        name="search_locations",
        description="按作品名或 Bangumi ID 查询 AniTabi 巡礼点位，可按地点名筛选。",
        version="1",
        public=True,
        timeout_ms=30000,
    )
    async def search_locations(
        self, subject_id: int | None = None, keyword: str = "", limit: int = 40,
        *, work_name: str = "", subject_type: str = "",
    ) -> Dict[str, Any]:
        if not self.config.plugin.enabled:
            return _failure("disabled", "AniTabi 动画巡礼插件已关闭。")
        if not isinstance(work_name, str) or len(work_name) > 128 or any(ord(char) < 32 for char in work_name):
            return _failure("invalid_work_name", "作品名须为 128 字以内的单行文字。")
        if subject_type == "all":
            subject_type = ""
        if not isinstance(subject_type, str) or (subject_type and subject_type not in SUBJECT_TYPES):
            return _failure("invalid_arguments", "作品类型须为动画、书籍、游戏、音乐或真人影视。")
        if not isinstance(keyword, str) or len(keyword) > 100 or type(limit) is not int or not 1 <= limit <= 60:
            return _failure("invalid_arguments", "地名关键词最多 100 字，返回数量须为 1 至 60。")
        if work_name := work_name.strip():
            if not _title_key(work_name) or subject_id is not None:
                return _failure("invalid_work_name", "请提供一个有效作品名或作品 ID。")
            resolved = await self._resolve_title(work_name, subject_type)
            if isinstance(resolved, dict):
                return resolved
            work_id = resolved
        else:
            work_id = self.config.search.default_subject_id if subject_id is None else subject_id
            if type(work_id) is not int or not 1 <= work_id <= 999999999:
                return _failure("invalid_subject_id", "请提供作品名或有效的 Bangumi 作品 ID。")
        try:
            data = await self._client.search(work_id, keyword, limit)
        except HTTPError as exc:
            if exc.code == 404:
                return _failure("not_found", "AniTabi 没有找到该作品的巡礼资料。")
            self.ctx.logger.warning(f"AniTabi 作品 {work_id} 查询失败，HTTP {exc.code}")
            return _failure("unavailable", f"AniTabi 暂时无法查询（HTTP {exc.code}）。")
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning(f"AniTabi 作品 {work_id} 查询失败：{exc}")
            return _failure("unavailable", "AniTabi 暂时无法查询。")
        return {"success": True, "error": None, "data": data}

    @Tool(
        "search_anitabi_locations",
        description=(
            "查作品取景地、圣地巡礼地点或 AniTabi 地图时使用。work_name 填用户说的作品名，如摇曳露营。"
            "subject_type 仅在用户明确指定版本时填动画、漫画等对应值，其他情况填 all。"
            "用户指定地名时填入 keyword，查询整部作品的巡礼地点时留空。"
            "收到点位、候选、暂无资料或查询不可用的状态后，如实告诉用户 AniTabi 查询结果。"
            "用户明确选择其他来源后再使用通用 Web 搜索。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "work_name": {"type": "string", "maxLength": 128, "description": "作品名，可用中文、日文或别名"},
                "subject_type": {
                    "type": "string", "enum": ["all", *SUBJECT_TYPES], "default": "all",
                    "description": "未指定版本用 all。anime 动画、book 漫画或小说、game 游戏、music 音乐、real 真人影视。",
                },
                "keyword": {"type": "string", "maxLength": 100, "default": "", "description": "地点关键词，如立川、サンサンロード"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6, "description": "返回点位数"},
            },
            "required": ["work_name"],
            "additionalProperties": False,
        },
        visibility="visible",
        timeout_ms=30000,
    )
    async def lookup(
        self, subject_id: int | None = None, keyword: str = "", limit: int = 6,
        *, work_name: str = "", subject_type: str = "all", **_kwargs: Any,
    ) -> Dict[str, Any]:
        if not work_name and subject_id is None:
            return _failure("missing_work", "请告诉我想查哪部作品。")
        if type(limit) is not int or not 1 <= limit <= 12:
            return _failure("invalid_arguments", "聊天查询最多返回 12 个点位。")
        # Prefer the supplied title when replaying calls from the old tool schema.
        result = await self.search_locations(
            None if work_name else subject_id, keyword, limit, work_name=work_name, subject_type=subject_type,
        )
        if not result["success"]:
            if result["error"] in {"ambiguous_work", "work_not_found", "not_found", "bangumi_unavailable", "unavailable"}:
                return {"success": True, "content": result["content"], "query_status": result["error"]}
            return result
        data = result["data"]
        lines = [f'《{data["title"]}》AniTabi 巡礼图 {data["url"]}']
        if data["matched"]:
            lines.append(f'匹配到 {data["matched"]} 条点位，显示 {len(data["points"])} 条。')
            lines.extend(_point_text(point) for point in data["points"])
        else:
            lines.append("本次查询没有匹配到巡礼点位。")
        return {"success": True, "content": "\n".join(lines)}


def create_plugin() -> AniTabiPlugin:
    return AniTabiPlugin()
