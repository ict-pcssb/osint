# 贵州旅行助手 MCP 调用手册

适用对象：开发单智能体的工具调用层、卡片渲染层和验收用例的同事。旅行工具快照核对于 2026-11-25；网页搜索更新于 2026-11-26。下面的日期、班次、天气和价格取自联调快照；运行时换成游客实际出行日。JSON 示例只列业务需要的字段，数组通常只展示一项。凡使用占位符或模拟资料的地方均另行标明。

## 先选工具

| 游客问题 | 首选工具 | 核心结果 |
|---|---|---|
| 青岩明天天气怎么样？ | Open-Meteo · `weather_forecast` | 对应地区、目标日、温度、降雨概率、逐时预报 |
| 到下一站走多久？附近有什么？ | OSM · `route`、`find_nearby_pois` | 米、秒、候选地点 |
| 贵阳北到遵义有哪些车？ | 12306 · 先查站码，再 `get-tickets` | 指定日期、发到站、车次、席别 |
| 上海飞贵阳多少钱？ | flights-mcp · `search_flights`；备用 fli | 航段、当地时间、人数、币种、参考价 |
| 两个人在贵阳住一晚？ | RollingGo · `searchHotels`；需要具体房型再 `getHotelDetail` | 每晚参考价或房型总价、币种、退改 |
| 贵阳北站打车去青岩？ | 高德打车 · `get_taxi_service` | 上下车点候选，或车型及预估价区间 |
| 只需要路线和普通出租车估算？ | 高德地图 MCP · `maps_direction_driving`；出租车费取高德 REST | 路线耗时、路程、整车参考价 |
| 打开青岩景点资料 | 项目本地 MCP · `get_scenic_card` | 景点目录卡及资料来源 |
| 找攻略和票价公告来源 | `travel-search.search_travel_sources`（Exa 优先，AnySearch 备用）；贵州知识库 HTTP | 标题、URL、摘要、来源和查询记录，原文适用性由助手核查 |

## 接入约定

新项目的 [MCP 配置](../.mcp.json) 登记了主用航班 `flights` 和统一网页搜索 `travel-search`。其余服务分别通过下文的 SDK、旧项目适配器或 HTTP 接入。工具注册到新助手后，仍应沿用原网站的账号与限流边界。凭据由服务端从本项目 `.env` 加载；文档和浏览器只出现变量名。高德调用前后查阅并追加[用量记录](../用量记录/高德用量记录.md)。

**MCP 公共调用方法。** 本地 stdio 用子进程的标准输入输出传输，远程 MCP 用 Streamable HTTP。先 `initialize`，再用 `list_tools` 读取目标工具的 `inputSchema.required`、`properties`，最后 `call_tool(name, arguments)`。可以在同一会话里连续调多个工具。下面的客户端示例对应原项目锁定的 `mcp==1.30.0`。主用航班服务在自己的 `.venv-flight` 环境启动。Windows 或容器内经代理运行 Node 工具时，可复用旧项目 `travel_tools.invoke` 的子进程环境设置。

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import httpx
from mcp.client.streamable_http import streamable_http_client

async def call_local(command, args, name, arguments, cwd=None):
    params = StdioServerParameters(command=command, args=args, cwd=cwd)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)

async def call_remote(url, headers, name, arguments):
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=False) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                return await session.call_tool(name, arguments)
```

例如，在新项目根目录、已安装上述客户端的 Python 环境中，下面的函数可与上面的示例放在同一文件。传入游客的实际出发日期即可调用已登记的航班服务。

```python
import json
from pathlib import Path

async def shanghai_to_guiyang(depart_date):
    config = json.loads(Path('.mcp.json').read_text(encoding='utf-8-sig'))
    service = config['mcpServers']['flights']
    return await call_local(service['command'], service['args'], 'search_flights', {
        'origin': 'PVG', 'destination': 'KWE', 'depart_date': depart_date,
        'adults': 1, 'currency': 'CNY', 'max_results': 3,
    })
```

**读取结果。** 先查 MCP 的 `isError`；再读 `structuredContent`。若业务字段不在其中，遍历 `content` 的 text 块，逐块尝试 `json.loads`。酒店和高德打车都可能返回文本块；高德打车还可能同时给人读的文字和给程序读的 JSON。保留原始单位，检查业务错误、日期、地点、人数及币种，再输出卡片。HTTP 200、握手成功、找到工具，都不足以证明查到了业务结果。空数组表示本次没有命中；超时、认证失败、配额不足应分别显示。证据附查询时间、数据来源及适用日期，缓存保留原采集时间。

## 1. Open-Meteo 天气 MCP

**启动。** Node stdio，`open-meteo-mcp-server@2.5.6`，入口 `dist/index.js`，无需 Key。与 OSM、12306 共用 [npm 锁文件目录](../../samples-1124/scripts/travel_eval/mcp)。该目录执行 `npm ci` 后可从 `node_modules/<包名>/<入口>` 启动；原项目容器在构建时安装。现有 [forecast 适配器](../../samples-1124/travel_tools.py)接受 `forecast(poi_id, start, end)`，两个日期为 Python `date`。

**调用。** `weather_forecast` 使用 WGS84 经纬度、当地时区、起止日期；`daily` / `hourly` 指定返回字段。下面为青岩目录坐标，精度为近似位置。

```json
{"latitude":26.3308,"longitude":106.6822,"timezone":"Asia/Shanghai","start_date":"2026-11-28","end_date":"2026-11-28","daily":["temperature_2m_min","temperature_2m_max","precipitation_probability_max"],"hourly":["temperature_2m"]}
```

**返回。** 原始 `daily.time[]` 与各 `daily.<指标>[]` 按同一下标对齐，`hourly.time[]` 同理，单位在 `daily_units` 和 `hourly_units`。项目适配器返回 `{location, kind, timezone, days, units, as_of}`。每个 day 有 `date`、分日指标、`hours[]`。实测花溪区 11 月 28 日为 5.3～20.5 ℃、24 条逐时记录。需要空气质量时，`air_quality` 另传经纬度、日期、时区及 `hourly:["pm2_5","pm10"]` 等字段，其查询失败可继续展示天气。超预报窗口、日期不匹配或缺少概率单位时保留待查；花溪区预报只按其覆盖范围说明。

## 2. OSM 地图 MCP

**启动。** `osm-mcp@0.3.2`，Node stdio，入口 `dist/index.js`，免 Key。项目对 OSM 点串使用 **WGS84，纬度,经度**。地图页任意两点问路调用通用 `route`；旧行程的 `route(station, gate)` 对站点及入口坐标有额外限制。

```json
{"waypoints":["26.58,106.71","26.59,106.72"],"profile":"foot"}
```

**返回。** `route` 的核心字段是 `profile`、`distance_m`、`duration_s`。上述样例返回 2059 米、1663 秒。`profile` 支持 `foot/bike/car`，业务层展示时再转公里、分钟。路线失败应返回“暂未查到可通行路线”，不要用两点直线距离估算驾车时间。

| 其他地图工具 | 参数和结果用途 |
|---|---|
| `geocode`、`reverse_geocode` | 前者传 `query`、可选 `limit/provider`，读 `results[]` 中的地点候选及坐标；后者传数值 `latitude, longitude`，读 `result` 中该坐标的地址。 |
| `find_nearby_pois`、`poi_details` | 前者传 `near, category, radius_m, limit`，如 `category="amenity=restaurant"`；后者传 `osm_id="node/1681825151"`，读取该点属性。附近搜索读 `results[]`，详情读 `osm, name, lat` 等字段。地图收录时间和营业信息分开处理。 |
| `route_matrix`、`optimize_route` | 传 `origins[], destinations[], profile` 读 `durations_minutes[i][j]`，单位为分钟；传 `stops[], profile, roundtrip` 读 `optimized_order[]`、`legs[]`。把改动后的景点顺序交给游客确认。 |
| `isochrone`、`suggest_meeting_point` | 传 `center, profile, minutes` 读 `reach, bounding_box` 的 `north/south/east/west`；传 `locations[], profile, venue_category, search_radius_m` 读 `suggestion, travel_times[]`。 |
| `straight_line_distance`、`map_link` | 前者传 `from, to`，读 `distance_m`；后者传 `place` 读 `marker`，或传 `from, to, profile` 生成路线链接。 |

通过原网站调用时，可 `POST /api/travel/skills/osm-tools`，请求 `{"tool":"route","arguments":{"waypoints":["26.58,106.71","26.59,106.72"],"profile":"foot"}}`。响应的 `result` 才是地图数据，外层 `data_mode, as_of, source, source_url` 用于证据展示。支持类别、坐标范围、点数限制见[参数模型](../../samples-1124/travel_osm_workbench.py)和[HTTP 路由](../../samples-1124/travel_skill_api.py)。

## 3. 12306 铁路 MCP

**启动。** `12306-mcp@0.3.10`，Node stdio，入口 `build/index.js`，免 Key。推荐直接复用 `rail("贵阳北","遵义","2026-11-28")`。自行接入时分两次调用：

1. `get-station-code-by-names` 传 `{"stationNames":"贵阳北|遵义"}`，响应按站名索引，取各站的 `station_code`。
2. `get-tickets` 请求如下。将占位替换成第一步返回的站码。`format="json"` 用来获取车次数组。

```json
{"date":"2026-11-28","fromStation":"<贵阳北站码>","toStation":"<遵义站码>","format":"json","limitedNum":0}
```

**返回。** 原始 JSON 是车次数组；适配器包装为 `{date, from, to, trains:[…]}`。每条有 `start_train_code, from_station, to_station, start_date, arrive_date, start_time, arrive_time, prices`。席别数组元素含 `seat_name, seat_type_code, num, price, discount` 等字段；没有票价或余量时单项标待查。11 月 28 日样例返回 48 条，其中 G1324 为 07:18–08:09。查询返回 0 条、站名无法解析、工具在握手前断开，这三种结果要分开处理。

## 4. 飞贵阳的机票：主用和备用

| | 主用 `ricardo-perello/flights-mcp` | 备用 `punitarani/fli` |
|---|---|---|
| 启动及凭据 | 读本项目 `.mcp.json → mcpServers.flights` 的 command 和 args。独立 Python ≥3.11 环境安装 [requirements-flight.txt](../requirements-flight.txt)，Windows 需要 tzdata。无 Key。 | 按[上游说明](https://github.com/punitarani/fli)在独立环境安装 `flights[mcp]`，以 `fli-mcp` 启动 stdio。无 Key。上轮从临时环境验证，未注册为持久服务。 |
| 工具 | `search_flights` | `search_flights` |
| 请求 | `{"origin":"PVG","destination":"KWE","depart_date":"2026-11-28","adults":1,"currency":"CNY","max_results":3}` | `{"origin":"SHA","destination":"KWE","departure_date":"2026-12-12","currency":"CNY","top_n":2}` |
| 返回 | `origin, destination, currency, count, days[], flights[], search_url`。每个航班读 `price, currency, flight_numbers[], route[], depart_local, arrive_local, duration_minutes, stops, legs[]`。 | `success, count, flights[], booking_url`。航班价格读 `flights[].price/currency`；各段在 `legs[]`，其中有 `departure_airport, arrival_airport, departure_time, arrival_time, airline_code, flight_number`。 |
| 样例 | CZ6352，浦东→贵阳，19:30–22:30，2040 CNY。 | 虹桥→贵阳，春秋 9C8885，17:00–20:00，500 CNY。 |

接入时按日期参数名、条数参数名分别构造调用。主用工具要检查 `days[].status`、`dates_not_searched` 以及每条路线的币种、航段和请求人数。备用工具也要检查 `success` 与 `flights`。两者共享 Google Flights 上游，同时报同一价格只算一个来源。报价是查询时的参考价；行李、税费或退改字段缺失时保留未知。迁移电脑或云环境后更新配置中的 Windows 绝对路径。[主用工具](https://github.com/ricardo-perello/flights-mcp)

## 5. RollingGo 酒店 MCP

**连接。** Streamable HTTP `https://mcp.rollinggo.cn/mcp`，请求头 `Authorization: Bearer <ROLLINGGO_HOTEL_API_KEY>`。从现有网站接入优先使用登录态酒店路由，让旧后端执行账号归属检查与限流。[酒店适配器](../../samples-1124/travel_hotels.py)

**搜酒店。** `searchHotels` 的两人一晚示例：

```json
{"originQuery":"贵阳市附近住宿","place":"贵阳市","placeType":"城市","checkInParam":{"checkInDate":"2026-11-28","stayNights":1,"adultCount":2},"size":2}
```

响应包含 `success, code, message, nextPageToken, hotelInformationList[]`。取 `hotelId, name, address, starRating, distanceInMeters`。每晚参考价在 `price.lowestPrice`，币种在 `price.currency`；`hasPrice=false` 时不要展示数字。上次样例返回 2 家。经网站调用则 `POST /api/travel/hotels/search`，传 `{"place":"贵阳市","place_type":"城市","check_in":"2026-11-28","nights":1,"adults":2}`，读取 `{provider, hotels:[{id,name,reference_price,currency,…}], cursor}`。

**查房价与退改。** `getHotelDetail` 的 `hotelId` 必须取自搜索。示例填法如下，数字 ID 为占位示意。

```json
{"hotelId":123,"dateParam":{"checkInDate":"2026-11-28","checkOutDate":"2026-11-29"},"occupancyParam":{"adultCount":2,"roomCount":1},"filter":{"mealType":"ALL","cancelPolicy":"ALL"}}
```

结果数组 `roomRatePlans[]` 读 `roomName, ratePlanName, averagePrice, totalPrice, currency, cancelPolicy, isOnRequest`。区分每晚和入住总价。另有 `getHotelSearchTags({}) → tags[].name` 和 `getHotelStaticInfo({hotelId,categories:["policy","facilities","images"]}) → hotelStaticInfo`。最近一次验收覆盖搜索；上述详情字段按现有源码核对。上游业务失败或字段缺失不能显示“已订”。本助手只挂查询工具。

## 6. 高德地点、驾车路线与出租车参考价

**凭据与坐标。** 使用 `AMAP_KEY`（Web 服务类型）。高德点串是 **GCJ-02，经度,纬度**。同一行程复用已确定的地点和坐标；OSM 的 WGS84 点需转换后再用于高德。每次调用参照[用量记录](../用量记录/高德用量记录.md)。

| 调用方式 | 输入 | 返回与用法 |
|---|---|---|
| REST 地点：`GET https://restapi.amap.com/v5/place/text` | query 传 `key, keywords, region=贵阳, city_limit=true, page_size=5` | 检查 `status=="1"`，读 `pois[].name/location`。站点、停车场、售票处可能并列，请游客选择会影响行动的歧义。 |
| 地图 MCP：`https://mcp.amap.com/mcp?key=<AMAP_KEY>`，Streamable HTTP，`maps_direction_driving` | `{"origin":"106.674554,26.619478","destination":"106.688325,26.333825"}` | 读 `paths[].distance/duration/steps[]`，米、秒通常为字符串。贵阳北站→青岩景区北城门返回 `"36050"`、`"2548"`。 |
| REST 路线：`GET https://restapi.amap.com/v5/direction/driving` | query 传 `key, origin, destination, show_fields=cost`，坐标同上 | 检查 `status, info`；读 `route.taxi_cost`（元）、`route.paths[].distance`、`route.paths[].cost.duration/tolls`。该样例普通出租车预估 110 元，路桥费字段为 0。 |

只显示供应商真实返回的估价。高德的工具握手、业务调用和官方账单计数分别记录。[路线文档](https://lbs.amap.com/api/webservice/guide/api/newroute)

## 7. 高德打车 MCP：取得车型报价

**连接。** Streamable HTTP `https://mcp.amap.com/rest/amap_car/dev/mcp?key=<AMAP_KEY>`。工具白名单只放 `get_taxi_service`（行前规划）。起点用车站时，工具可能需要具体上车区；交互应保留服务端原样返回的上下文。

**调用参数。** 核心是 `origin, destination, origin_city, destination_city, agent_name`。城市用行政区全称；`agent_name` 按服务要求填调用宿主。下面按上游工具契约给出经济车型的完整请求示例。接入新版本时用 `tools/list` 核对实际必填字段。出发时间和到达时间只能指定一项。即时出行 `departure_time=""`，无价格偏好 `price_range=["",""]`，首次规划 `businessData=""`。类型和枚举见[官方工具契约](https://github.com/AMap-Web/amap-ride-hailing-skill/blob/main/references/tool-contracts.md)。

```json
{"origin":"贵阳北站-东广场-地下停车场-网约车候车区","destination":"青岩古镇北门停车场","origin_city":"贵阳市","destination_city":"贵阳市","agent_name":"Codex","departure_time":"","arrival_time":"","vehicle_type":["经济"],"vehicle_brands":[],"price_range":["",""],"price_reference":"","power_source":"","vehicle_body":[],"incar_environment":[],"driver_requirements":[],"route_requirements":[],"waypoints":[],"businessData":""}
```

**结果分支。** 遍历 text 块，保留自然语言摘要，解析含 `entities[]` 的 JSON 块：

| `entities[].cardScene` | 要读的字段 | 助手动作 |
|---|---|---|
| `amap_car_point` | `modelInput.headerTitle`、`modelInput.points[]`；候选有 `name, pointId`，有时包含 `childNodeList[]`；保存同一实体的 `businessData` | 展示候选给游客。游客选中后，按 `cardSubScene` 把候选原名填回 `origin`（`start_point`）、`destination`（`end_point`）或 `waypoints`（`pass_point*`）。原样传回这次选点的 `businessData`，再次调用同一工具。 |
| `amap_car_plan` | `modelInput.startPoint/endPoint`、`modelInput.transportTypeInfoList[]` 以及车型分组的 `minPrice/maxPrice` | 取已选车型的区间。上例经济车型 67.6～101.6 元，约 36.4 公里、39 分钟。不要跨车型取全局最低最高。 |
| `amap_car_abnormal` 或未知场景 | 服务返回的说明 | 显示待查；可用第 6 节独立查询普通出租车参考价，并在卡片上注明口径。 |

这里的北站东广场至北门停车场，与第 6 节站点至景区北城门是两对不同的端点。比较费用时一并显示端点、计价方式和核查时间。

## 8. 本地景点资料 MCP

**启动。** 在 [samples-1124](../../samples-1124) 仓库，用已安装项目依赖的 Python 运行 `python -B mcp_local_server.py`，传输为 stdio，无 Key。调用 `get_scenic_card`，参数 `{"poi_id":"qingyan"}`。`poi_id` 取自当前地图选点或[目录](../../samples-1124/static/travel/data/pois.json)。

成功返回一个景点对象，包含 `poi_id, name, region, county, coordinates, description, images, sources, booking, reviewed_at`。青岩的 `coordinates` 是 `{"longitude":106.6822,"latitude":26.3308,"system":"WGS84","precision":"approximate"}`。未知 ID 会产生工具错误。`reviewed_at` 是资料复核时间，不能直接当成当天开放证明。[服务源代码](../../samples-1124/mcp_local_server.py)

## 9. 统一网页搜索 MCP（Exa 优先、AnySearch 备用）

新项目的 [MCP 配置](../.mcp.json) 登记了 `travel-search`。智能体只调用 `search_travel_sources`；[服务代码](../travel_search_mcp.py) 负责选择供应商。它通过 Streamable HTTP 访问 Exa 官方 MCP 的 `web_search_exa`，需要回退时才调用 AnySearch 的 HTTP `POST /v1/search`。Exa 搜索走免 Key 限速入口，AnySearch 默认匿名。若部署方决定使用已有的 AnySearch 账号，在**服务进程环境**同时设置 `ANYSEARCH_AUTH_MODE=key` 和 `ANYSEARCH_API_KEY`；仅在 .env 里存有 Key 不会启用账号调用。测试未使用付费凭据。[Exa 官方说明](https://exa.ai/docs/get-started/exa-mcp)

**启动与调用。** 本机使用配置中已安装依赖的 Python 3.13 环境，通过 stdio 启动。迁移时安装 [requirements-search.txt](../requirements-search.txt)，并更新配置里的解释器和脚本路径。工具发现和调用遵循本手册的 MCP 公共流程。下面是发给项目工具的完整业务请求示例：

```json
{"name":"search_travel_sources","arguments":{"query":"梵净山 景区 儿童 身高 门票 观光车 优惠 官方","subject":"梵净山","required_terms":["观光车"],"source_domains":["fanjingshan.gz.cn"],"limit":3}}
```

| 参数 | 调用方填什么 |
|---|---|
| `query`、`subject`（必填） | 中文搜索词及一个必须出现在标题或摘要里的地点、门店或线路。用具体名称，避免只填“贵州”。 |
| `required_terms`（可选） | 候选必须同时包含的字词。公交查询补起终点、返程方向或末班关键词，口味判断还需实际到店材料。词匹配采用字面匹配。 |
| `source_domains`（可选） | 已知官网或指定来源域名数组。命中本域及其子域；留空允许多类来源。限定转载平台适用于查传播线索，不能据此认定官方价格。 |
| `target_date`（可选） | 查询特定日活动时填 `YYYY-MM-DD`。只检查标题和返回摘要中明确写出的年月日，独立的发布日期字段不参与。常规票价规则省略，再打开原文查生效期。仅写日期范围、长期有效或历法简称的材料可能被漏掉。 |
| `rejected_urls`（可选） | 已读原文后发现过期、跑题或证据不足的 URL。再次查询时两家都排除这些链接。 |
| `limit`（可选） | 返回 1～5 条，默认 3；每家会多取最多 2 条以便筛选、去重。 |

**回退规则。** Exa 有至少一条通过筛选就直接返回。没有合适候选、工具异常或超过 20 秒时，再向 AnySearch 查询一次，沿用同一组条件。结果是供阅读核查的线索；“文本提到某日”不足以断言“该日确认开放”。

**返回。** MCP 业务对象在 `structuredContent`（Python SDK 2.x 属性 `structured_content`）；兼容客户端也可解析文本 JSON。下例为一次回退命中的结构示意：

```json
{
  "status":"candidates",
  "checked_at":"2026-11-26T01:37:03+00:00",
  "sources":[{"provider":"anysearch","title":"示例页面","url":"https://example.org/article","excerpt":"检索摘要示例","published_at":null}],
  "attempts":[{"provider":"exa","status":"no_match","returned":5,"accepted":0},{"provider":"anysearch","status":"matched","returned":5,"accepted":1}]
}
```

`checked_at` 是本次检索时间（UTC）。`published_at` 可空，另行核实实际到访时间和规则适用期。每条摘要最多 2,800 字符。`status=candidates` 表示有候选；`unconfirmed` 表示至少一家执行成功但没有候选通过；`unavailable` 表示两家请求均失败。后二者均返回 `sources:[]`。单次请求失败在 `attempts` 中记作 `status=error` 和 `reason`，例如 `timeout`、`http_429`。

**2026-11-26 从项目配置实测。** 梵净山门票按景区域名筛选，Exa 返回 3 条，含购票须知和观光车票价公示，未触发备用源。为验证非空回退，另做两个限制来源的用例：路线问答/路线站点返回 AnySearch 的 3 条线索；新浪票价报道返回 AnySearch 的 2 条转载线索。两例均先查询 Exa。青岩 11 月 28 日夜游公告、指定日返程末班车、限定贵州省政府域名的民生路餐饮查询均经过两家后返回待确认。这些测试的域名限制决定了查询范围。5 个离线回归用例覆盖首选命中、发布日期误用、超时回退、双错误、排除已驳回链接与域名边界。[回归测试](../test_travel_search_mcp.py)

**景区门票。** 新项目没有专用实时门票 MCP。旧项目预算 Skill 文本提到 `ticket.quote` 和途牛 `startPrice`，没有找到对应注册工具或实际报价调用。旧项目的 `travel_ticket.py` 做票面字段识别；景点卡提供预约入口。当前通过统一搜索核实公开的门票、观光车和儿童优惠规则；指定日期的实时库存、可预订票种和成交价仍缺专用供应商。[预算 Skill](../../samples-1124/skills/travel/check-trip-budget/SKILL.md) · [旧项目验收记录](../../samples-1124/scripts/travel_eval/README.md)

## 10. 配套 HTTP：AnySearch 和贵州公共知识库

**AnySearch 搜索。** `POST https://api.anysearch.com/v1/search`，JSON 请求体 `{"query":"青岩古镇 贵阳市 site:gov.cn","max_results":2}`；带 Key 时加 `Authorization: Bearer <ANYSEARCH_API_KEY>`。近期带 Key 联调返回 HTTP 200、`code=0`、两条结果。读 `data.results[].title/url/snippet`。摘要可用于选择要追查的原文，不足以证明周六有活动。

**AnySearch 原文。** `POST https://api.anysearch.com/v1/extract`，同一鉴权，请求 `{"url":"<游客分享的公开网页 URL>"}`。核对 HTTP 状态和 `code==0` 后，旧适配器读取 `data.content` 或 `data.text`。空正文、登录受限或网页不可读时，请游客贴截图或复述待核实的那句话。带 Key 搜索已实测。11 月 26 日另测匿名提取，部分网页返回正文，另有网页返回 422。提取字段见[现有适配器](../../samples-1124/travel_weekend.py)核对。Key 只发往服务商域名。

**公共文旅知识库。** `POST <TRAVEL_KB_BASE_URL>/v1/retrieve`。配置了 `TRAVEL_KB_TOKEN` 时发 Bearer 头。请求和示例响应节选如下：

```json
{"query":"青岩门票和观光车分别怎么收费","poi_ids":["qingyan"],"region":"贵阳","trip_dates":["2026-11-28","2026-11-29"],"k":4}
```

```json
{"corpus_version":"guizhou-mock-v1","data_mode":"fixture","hits":[{"doc_id":"mock-qingyan-fare","chunk_id":"c1","poi_id":"qingyan","source_type":"official_notice","excerpt":"【模拟资料】样例中成人票与观光车分别计费，儿童优惠条件待补查。","source_url":null}]}
```

读 `hits[].title/excerpt/doc_id/chunk_id/source_type`，有适用日期再读 `valid_from/valid_to`。无命中是 `hits:[]`，应保留 `corpus_version`。mock 用 `data_mode=fixture`，即便来源类型叫 `official_notice`，也只能用作联调。`GET /v1/health` 可核对服务版本；本地启动命令和正式替换方式见[mock 服务](../../samples-1124/scripts/travel_eval/mock_kb.py)与[跨团队契约](../../samples-1124/contracts/travel/v1/README.md)。

## 返回 JSON 对照

以下是解析 MCP 消息后交给业务代码的字段节选。快照中的数值来自同目录复测记录；酒店和网页条目标为“示例”的值专用于说明结构。字段的缺失、空数组与价格为零应分别处理。

| 工具或接口 | 成功响应的业务字段节选 |
|---|---|
| Open-Meteo 天气原始结果 | `{"daily":{"time":["2026-11-28"],"temperature_2m_min":[5.3],"temperature_2m_max":[20.5]},"daily_units":{"temperature_2m_min":"°C","temperature_2m_max":"°C"}}` |
| OSM 步行路线 | `{"profile":"foot","distance_m":2059,"duration_s":1663}` |
| 12306 项目适配器 | `{"date":"2026-11-28","from":"贵阳北","to":"遵义","trains":[{"start_train_code":"G1324","start_time":"07:18","arrive_time":"08:09"}]}` |
| 主用机票 | `{"origin":"PVG","destination":"KWE","adults":1,"currency":"CNY","count":3,"flights":[{"flight_numbers":["CZ6352"],"price":2040,"currency":"CNY"}]}` |
| 备用机票 | `{"success":true,"flights":[{"price":500,"currency":"CNY","legs":[{"departure_airport":"SHA","arrival_airport":"KWE"}]}]}` |
| RollingGo 搜索（结构示例） | `{"success":true,"hotelInformationList":[{"hotelId":123,"name":"示例酒店","price":{"hasPrice":true,"lowestPrice":260,"currency":"CNY"}}]}` |
| 高德地图 MCP | `{"paths":[{"distance":"36050","duration":"2548"}]}` |
| 高德打车 MCP | 解析文本块中的 JSON 后读取 `{"entities":[{"cardScene":"amap_car_plan","modelInput":{"transportTypeInfoList":[]}}]}`。此处空列表仅示意嵌套层级；真实结果应读取选定车型的费用。 |
| 本地景点卡 | `{"poi_id":"qingyan","name":"青岩古镇","region":"贵阳","county":"花溪区","coordinates":{"longitude":106.6822,"latitude":26.3308,"system":"WGS84","precision":"approximate"}}` |
| AnySearch 搜索（结构示例） | `{"code":0,"data":{"results":[{"title":"示例网页","url":"https://example.org/notice","snippet":"示例摘要"}]}}` |

## 接入范围和验收

- 早期候选 `altunoren/googleflights-mcp` 虽然能列出工具，上次在上海飞贵阳场景中未取得报价。见[早期试验记录](../测试记录/接口实测报告-2026-11-25.md)。
- 优先复用当前配置中的主用航班；备用 fli 的实测环境位于临时目录，部署前需要重新安装并注册。旧研究流程的百炼 WebSearch 尚缺凭据。飞常准已从新助手的航班方案中替换。
- 候选[腾讯地图](https://github.com/TencentLBS/tencentmap-webservice-skill)的驾车路线可能返回 `taxi_fare`，须另配腾讯 Key；最近匿名调用返回 301。[滴滴](https://github.com/didi/didi-ride-skill)提供 `taxi_estimate`，需要滴滴 MCP Key，匿名测试未取到报价。两者暂不作为已接通的回退。
- 上线前分别用一个正常请求、一个空结果或失效日期、一次上游不可达来验收。判断依据是工具结果能否生成正确卡片，并保留来源、适用日期和缺失项。历史实测数字只用于对照，持续集成可以验证字段、单位和日期匹配，不应断言实时价格永远等于快照。
- 已验证范围和原始样例见[全量旅行接口复测](../测试记录/全量旅行接口复测-2026-11-25.md)。本手册没有触发新的付费查询或高德业务调用。

## Parallel Search MCP · 2026-11-26

项目配置名为 `parallel-search`，通过 Streamable HTTP 直连 `https://search.parallel.ai/mcp`，本次匿名实测无需 Key。它供显式搜索或交叉核查使用；统一入口 `search_travel_sources` 的自动顺序仍为 Exa → AnySearch。[官方仓库](https://github.com/parallel-web/search-mcp)

| MCP 工具 | 请求示例 | 业务返回 |
|---|---|---|
| `web_search` | `{"objective":"核对梵净山儿童门票和观光车规则","search_queries":["梵净山 儿童 购票须知","梵净山 观光车 优惠"]}` | 解析 MCP 文本块中的 JSON，读取 `search_id, results[], warnings`；结果有 `title, url, publish_date, excerpts`，日期可空。 |
| `web_fetch` | `{"urls":["https://fanjingshan.gz.cn/?page_id=1188"],"objective":"查儿童身高、年龄和观光车条款"}` | 读取 `extract_id, results[], errors, warnings`。从各结果的 `excerpts[]` 取相关原文，逐 URL 检查错误。 |

从项目配置完成握手和工具发现，四题各搜索两轮，并两次读取景区原文。梵净山官网购票须知排第一，原文能区分儿童门票和观光车。青岩 11 月 28 日夜游未找到目标日节目单，返程公交未核实目标方向有效末班。民生路能找到罗记门店，但相关新闻及门店资料不足以确定近期同一时段排队时间。发布日期与活动日、到访日分别核验。
