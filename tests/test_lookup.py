from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import json
import logging
import sys
import threading

import pytest


package = ModuleType("anitabi_public_tests")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
client = import_module(f"{package.__name__}.client")
plugin = import_module(f"{package.__name__}.plugin")


@pytest.fixture
def instance():
    value = plugin.create_plugin()
    value.set_plugin_config(value.get_default_config())
    value._set_context(SimpleNamespace(logger=logging.getLogger(__name__)))
    return value


def test_search_uses_public_bangumi_contract(monkeypatch):
    observed = []

    def request(value):
        observed.append(value)
        return {"data": [{
            "id": 207195, "name": "ゆるキャン△", "name_cn": "摇曳露营", "type": 2,
            "date": "2018-01-04", "infobox": [{"key": "别名", "value": [{"v": "Yuru Camp"}]}],
        }]}

    monkeypatch.setattr(client, "_request_json", request)
    subjects = client._search_subjects("摇曳露营", "anime", 10)
    req = observed[0]
    assert req.full_url == "https://api.bgm.tv/v0/search/subjects?limit=10&offset=0"
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {
        "keyword": "摇曳露营", "sort": "match", "filter": {"nsfw": False, "type": [2]},
    }
    assert subjects[0]["type"] == "anime"
    assert subjects[0]["aliases"] == ["Yuru Camp"]
    assert req.get_header("Authorization") is None


@pytest.mark.parametrize("payload", [None, {}, {"data": {}}, {"data": [{"id": True}]}])
def test_invalid_search_response_is_an_error(monkeypatch, payload):
    monkeypatch.setattr(client, "_request_json", lambda _: payload)
    with pytest.raises(ValueError):
        client._search_subjects("example", "", 10)


def test_empty_search_is_a_valid_empty_result(monkeypatch):
    monkeypatch.setattr(client, "_request_json", lambda _: {"data": []})
    assert client._search_subjects("missing", "", 10) == []


def test_search_cache_is_shared_and_bounded(monkeypatch):
    calls = []

    def search(keyword, subject_type, limit):
        calls.append(keyword)
        return [{"id": 207195, "name_cn": keyword}]

    monkeypatch.setattr(client, "_search_subjects", search)

    async def scenario():
        value = client.AniTabiClient()
        a, b = await asyncio.gather(value.lookup_subjects("same"), value.lookup_subjects("same"))
        assert a == b and calls == ["same"]
        for i in range(client.MAX_CACHED_WORKS + 1):
            await value.lookup_subjects(f"title-{i}")
        assert len(value._subject_cache) == client.MAX_CACHED_WORKS
        await value.lookup_subjects("same")
        assert calls.count("same") == 2

    asyncio.run(scenario())


def test_expired_title_cache_is_refreshed(monkeypatch):
    now = [10.0]
    calls = []
    monkeypatch.setattr(client, "monotonic", lambda: now[0])
    monkeypatch.setattr(client, "_search_subjects", lambda *args: calls.append(args) or [])

    async def scenario():
        value = client.AniTabiClient()
        await value.lookup_subjects("same")
        now[0] += client.CACHE_SECONDS + 1
        await value.lookup_subjects("same")
        assert len(calls) == 2

    asyncio.run(scenario())


def test_title_lookup_does_not_require_another_plugin(instance):
    instance._client.lookup_subjects = AsyncMock(return_value=[{
        "id": 207195, "name_cn": "摇曳露营", "name": "ゆるキャン△", "type": "anime", "date": "2018-01-04",
    }])
    instance._client.search = AsyncMock(return_value={
        "title": "摇曳露营", "url": "https://anitabi.cn/map?bangumiId=207195", "matched": 0, "points": [],
    })
    result = asyncio.run(instance.lookup(work_name="摇曳露营"))
    assert result["success"] and "bangumiId=207195" in result["content"]
    instance._client.lookup_subjects.assert_awaited_once_with("摇曳露营", "", limit=10)


def test_numeric_plugin_api_bypasses_title_search(instance):
    instance._client.lookup_subjects = AsyncMock(side_effect=AssertionError("unexpected title search"))
    instance._client.search = AsyncMock(return_value={"subject_id": 207195, "points": [], "matched": 0})
    result = asyncio.run(instance.search_locations(subject_id=207195))
    assert result["success"]
    instance._client.lookup_subjects.assert_not_awaited()


def test_legacy_tool_arguments_prefer_the_supplied_title(instance):
    instance._client.lookup_subjects = AsyncMock(return_value=[{"id": 207195, "name": "摇曳露营"}])
    instance._client.search = AsyncMock(return_value={"title": "摇曳露营", "url": "map", "matched": 0, "points": []})
    result = asyncio.run(instance.lookup(subject_id=51, work_name="摇曳露营"))
    assert result["success"]
    instance._client.search.assert_awaited_once_with(207195, "", 6)


def test_ambiguity_is_reported_instead_of_picking_a_work(instance):
    instance._client.lookup_subjects = AsyncMock(return_value=[
        {"id": 51, "name": "CLANNAD", "type": "anime", "date": "2007-10-04"},
        {"id": 52, "name": "CLANNAD", "type": "anime", "date": "2008-10-02"},
    ])
    instance._client.has_landmarks = AsyncMock(return_value=True)
    instance._client.search = AsyncMock()
    result = asyncio.run(instance.lookup(work_name="CLANNAD"))
    assert result["query_status"] == "ambiguous_work"
    assert "2007" in result["content"] and "2008" in result["content"]
    instance._client.search.assert_not_awaited()


def test_anime_with_landmarks_is_preferred_over_another_medium(instance):
    instance._client.lookup_subjects = AsyncMock(return_value=[
        {"id": 51, "name": "CLANNAD", "type": "anime"},
        {"id": 52, "name": "CLANNAD", "type": "game"},
    ])
    instance._client.has_landmarks = AsyncMock(return_value=True)
    assert asyncio.run(instance._resolve_title("CLANNAD", "")) == 51


@pytest.mark.parametrize("error", [OSError("network"), ValueError("invalid response"), TimeoutError()])
def test_search_failure_remains_visible(instance, error):
    instance._client.lookup_subjects = AsyncMock(side_effect=error)
    result = asyncio.run(instance.lookup(work_name="摇曳露营"))
    assert result["query_status"] == "bangumi_unavailable"
    assert "暂时不可用" in result["content"]


@pytest.mark.parametrize("arguments", [{"subject_id": True}, {"work_name": "\n"}, {"work_name": "name", "subject_type": "invalid"}])
def test_invalid_arguments_do_not_start_a_network_request(instance, arguments):
    instance._client.search = AsyncMock()
    instance._client.lookup_subjects = AsyncMock()
    result = asyncio.run(instance.search_locations(**arguments))
    assert result["success"] is False
    instance._client.search.assert_not_awaited()
    instance._client.lookup_subjects.assert_not_awaited()


def test_landmark_metadata_and_filtering(monkeypatch):
    overview = {"id": 207195, "cn": "摇曳露营", "litePoints": [{"id": "station", "geo": [35.0, 138.0]}]}
    points = [{"id": "station", "name": "山梨市駅", "cn": "山梨市站", "ep": 1, "s": 125, "origin": "community"}]
    work = client._work_data(207195, overview, points)
    assert work["points"][0]["coordinates"] == [35.0, 138.0]
    assert work["points"][0]["episode"] == 1
    assert work["points"][0]["seconds"] == 125
    value = client.AniTabiClient()
    value._work = AsyncMock(return_value=work)
    result = asyncio.run(value.search(207195, "山梨市站", 6))
    assert result["matched"] == 1
    assert "pid=station" in result["points"][0]["url"]


def test_response_size_limit_is_enforced(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b"x" * size

    monkeypatch.setattr(client, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="size limit"):
        client._get_json(207195, "lite")


@pytest.mark.parametrize("slow_kind", ["title", "work"])
@pytest.mark.parametrize("fast_kind", ["title", "work"])
def test_unrelated_queries_do_not_wait_for_a_slow_request(monkeypatch, slow_kind, fast_kind):
    started, release = threading.Event(), threading.Event()

    def search(keyword, subject_type, limit):
        if keyword == "slow":
            started.set()
            assert release.wait(5)
        return [{"id": 207195, "name_cn": keyword}]

    def get_json(subject_id, resource):
        if subject_id == 1:
            started.set()
            assert release.wait(5)
        return {"id": subject_id, "title": str(subject_id)} if resource == "lite" else []

    monkeypatch.setattr(client, "_search_subjects", search)
    monkeypatch.setattr(client, "_get_json", get_json)

    async def scenario():
        value = client.AniTabiClient()
        slow = asyncio.create_task(value.lookup_subjects("slow") if slow_kind == "title" else value._work(1))
        fast = None
        try:
            assert await asyncio.to_thread(started.wait, 1)
            fast = asyncio.create_task(value.lookup_subjects("fast") if fast_kind == "title" else value._work(2))
            result = await asyncio.wait_for(asyncio.shield(fast), timeout=1)
            assert result
            assert not slow.done()
        finally:
            release.set()
            await asyncio.gather(slow, *([fast] if fast is not None else []), return_exceptions=True)

    asyncio.run(scenario())


def test_same_work_is_loaded_once_when_a_waiting_caller_is_cancelled(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def get_json(subject_id, resource):
        calls.append((subject_id, resource))
        started.set()
        assert release.wait(5)
        return {"id": subject_id, "title": "work"} if resource == "lite" else []

    monkeypatch.setattr(client, "_get_json", get_json)

    async def scenario():
        value = client.AniTabiClient()
        first = asyncio.create_task(value._work(207195))
        second = third = None
        try:
            assert await asyncio.to_thread(started.wait, 1)
            second = asyncio.create_task(value._work(207195))
            third = asyncio.create_task(value._work(207195))
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            release.set()
            a, b = await asyncio.gather(first, third)
            assert a == b
            assert await value._work(207195) == a
            assert sorted(calls) == [(207195, "lite"), (207195, "points/detail")]
        finally:
            release.set()
            await asyncio.gather(first, *[t for t in (second, third) if t is not None], return_exceptions=True)

    asyncio.run(scenario())


def test_landmark_probes_share_the_network_concurrency_limit(monkeypatch):
    release, first_wave = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = 0

    def get_json(subject_id, resource):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 4:
                first_wave.set()
        try:
            assert release.wait(5)
            return {"id": subject_id, "pointsLength": 0}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(client, "_get_json", get_json)

    async def scenario():
        value = client.AniTabiClient()
        pending = [asyncio.create_task(value.has_landmarks(subject_id)) for subject_id in range(1, 11)]
        try:
            assert await asyncio.to_thread(first_wave.wait, 1)
            await asyncio.sleep(0.05)
            assert peak == 4
            release.set()
            assert await asyncio.gather(*pending) == [False] * 10
            assert peak == 4
        finally:
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(scenario())
