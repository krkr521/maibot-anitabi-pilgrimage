# AniTabi 动画巡礼 · MaiBot 插件

让 MaiBot 按作品名查询动画取景地和巡礼地图。作品名由 Bangumi 公开 API 解析，地标来自 AniTabi；不需要安装其他 Bangumi 插件，也不需要 API Key。

## 功能

- 模型工具 `search_anitabi_locations` 支持作品名、作品类型和地点关键词。
- 返回地标名称、地图链接，以及接口实际提供的坐标、集数、片中时间和标注来源。
- 同名作品先核对 AniTabi 是否收录点位；仍有多个候选时返回名称、类型与年份，供用户澄清。
- 作品与点位查询使用有界内存缓存，保留 15 分钟；网络请求在工作线程中执行，并限制超时和响应大小。
- 提供稳定的只读插件 API，可供日程或其他插件调用。插件本身不直接向 QQ 发送消息。

对话示例：

- “《摇曳露营》有哪些巡礼地点？”
- “查《吹响吧！上低音号》在宇治的取景点。”
- “看看《孤独摇滚！》的下北泽取景地。”

模型是否调用工具由当前模型和上下文决定；数据覆盖范围以 AniTabi 实际收录为准。

## 安装

需要 Python 3.12+、MaiBot 1.3.18–1.4.x 和 MaiBot Plugin SDK 2.19+（2.x）。离线验证环境为 MaiBot 1.4.3 / SDK 2.22.1。

在 MaiBot 根目录安装：

```bash
git clone https://github.com/krkr521/maibot-anitabi-pilgrimage.git plugins/local_anitabi-pilgrimage
```

在 WebUI 插件页加载并启用“AniTabi 动画巡礼”。插件使用标准库访问公开 API，SDK 由宿主提供。可以从 `config.example.toml` 复制运行配置，也可以由宿主生成配置。

本次公开发布保持插件 ID `local.anitabi-pilgrimage` 不变，已有调用方无需更改 API 名称。升级已有安装时，请先备份配置；不需要新增服务、端口或系统计划任务。

## 配置

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[search]
default_subject_id = 0
```

`default_subject_id` 仅用于没有传入作品参数的旧版插件 API 调用；0 表示要求调用方提供作品名或 ID。聊天工具要求明确的作品名。

## 工具和插件 API

`search_anitabi_locations` 的参数：

| 参数 | 说明 |
| --- | --- |
| `work_name` | 必填，中文名、原名或可被 Bangumi 搜索到的别名 |
| `subject_type` | 默认 `all`；可选 `anime`、`book`、`game`、`music`、`real` |
| `keyword` | 可选，按地标名称筛选 |
| `limit` | 默认 6，聊天工具最多返回 12 个点位 |

只读 API 名称为 `local.anitabi-pilgrimage.search_locations`，版本 `1`。支持 `work_name` 或 `subject_id`，两者不同时传入；API 返回数量上限为 60。

```python
result = await ctx.api.call(
    "local.anitabi-pilgrimage.search_locations",
    version="1",
    subject_id=207195,
    keyword="",
    limit=6,
)
```

SDK 返回的结果直接包含 `success`、`error` 和 `data`；不要额外解包一层 `result`。查询失败或作品有歧义时会返回明确状态，不会伪造点位。

## 开发与验证

```bash
uv sync --group dev
uv run pytest tests -q
uv run ruff check .
```

测试使用隔离数据和模拟网络，覆盖公开 API 请求格式、作品歧义、旧参数兼容、缓存、错误反馈和点位解析。测试不会启动 QQ 或向聊天发送消息。

## 数据来源与许可

- [Bangumi API](https://bangumi.github.io/api/) 用于解析作品。
- [AniTabi 开放 API](https://navi.anitabi.cn/docs/api/) 提供巡礼地标，结果保留地图和点位来源。
- 程序源码采用 [MIT](LICENSE) 许可。AniTabi 的地标详情数据按其声明的 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) 共享；程序许可不改变第三方数据、图片与作品素材的权利。

版本变化见 [CHANGELOG](CHANGELOG.md)。
