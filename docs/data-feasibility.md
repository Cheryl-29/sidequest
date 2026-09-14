# M0 数据可行性探针

更新：2026-09-14（Australia/Sydney）。状态：**M0 通过，范围已收缩并冻结**。

> 2026-09-15 收敛：M0 探针脚本（`probe_m0.py`、`probe_tfnsw.py`、`probe_search_ddgs.py`、`freeze_m0_replay.py`）、TfNSW 与 DDGS 原始快照、`evals/provider-m0.json` 以及实施边界文档 `m0-next-run.md` 已删除。本文作为结论记录保留；仍在仓库中的原始证据只有 `data/probes/20260914T041841Z/`（OSM 与天气响应，测试使用）。

## 决策

首版 Live 范围冻结为悉尼 CBD 的少量博物馆／美术馆常设参观：OSM 用于候选发现，场馆官方页面补核营业时间、收费和入场条件，TfNSW 提供逐段路线。公园只作条件性候选；定时活动暂不纳入可验证规划。

DDGS 固定 Bing 后端只作为开发期机会发现工具。精确日期召回不足，且无项目级 SLA，因此不进入首版 Live 验证链。已冻结的 Provider Replay 只用于历史评测；应用继续保持合成 Replay，本轮真实响应不进入生产 Validator，也不代表持续可用的实时行程。

## 可重复执行与原始证据

```bash
.venv/bin/python scripts/probe_m0.py
.venv/bin/python scripts/probe_tfnsw.py
.venv/bin/python scripts/probe_search_ddgs.py
```

脚本顺序请求四个区域的 museum/gallery/park 对象，再请求悉尼两天天气；无重试，遇到 429/406 停止后续 Overpass 查询。每次建立新目录，保存请求参数、HTTP 状态、获取时间、原始响应、SHA-256 和统计。失败不计为零候选，零样本缺失率为 null；Overpass remark 不计为完整成功。

首轮产物：[report.json](../data/probes/20260914T041841Z/report.json)。本轮脚本共 5 次请求：3 个 Overpass 成功、1 个 504、1 个天气成功。此前另有一次天气连通性成功请求，以及一次沙箱 DNS 失败尝试；文档浏览和人工搜索不含在脚本请求数内。

快照属于原始观测，不是独立核验过的 Replay 基准。抓取时间不等于事实有效期。

TfNSW 最终交通产物：[report.json](../data/probes/tfnsw-20260914T044205Z/report.json)。最终采样含 7 次顺序路段请求；本次会话另有 11 次鉴权、结构检查及修正前采样，总计 18 次 TfNSW 请求。修正前报告保留为探针开发记录，不用作结论。API Key 只从 `.env` 读入 Authorization header；密钥泄漏扫描确认最终报告及七份原始响应均不含 Key。

## 地点覆盖实测

请求：`nwr[tourism~"^(museum|gallery)$"]` 与 `nwr[leisure=park]`，返回对象中心和标签。边界框详见脚本和报告。统计分母包括未命名对象，不能当作可用推荐数量。

| 区域 | HTTP | 对象数 | 同 ID 重复 | 同名多余对象* | 缺名称 | 缺坐标 | 缺营业时间 | 缺 fee 标签 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CBD | 504 | 未测得 | — | — | — | — | — | — |
| Harbour | 200 | 48 | 0 | 0 | 6/48 | 0/48 | 39/48（81.3%） | 40/48（83.3%） |
| Glebe 周边 | 200 | 89 | 0 | 8 | 29/89 | 0/89 | 85/89（95.5%） | 86/89（96.6%） |
| Surry Hills 周边 | 200 | 66 | 0 | 2 | 11/66 | 0/66 | 59/66（89.4%） | 62/66（93.9%） |
| 成功查询合计 | — | 203 | 0 | 10 | 46/203 | 0/203 | 183/203（90.1%） | 188/203（92.6%） |

*同名多余对象仅为复核线索，并非已确认重复地点；公园可能由多个分区组成。合计是查询对象观测数，不宣称全城唯一地点数。way/relation 的 center 不等于可到达入口。fee=yes 不提供具体金额，fee 缺失不能推断免费。永久地点的活动日期／结束时间不适用；本轮没有测出活动缺失率。

| 分类（按标签独立计数） | 对象数 | 有营业时间 | 有 fee |
|---|---:|---:|---:|
| Museum | 9 | 6/9 | 7/9 |
| Gallery | 23 | 9/23 | 6/23 |
| Park | 171 | 5/171 | 2/171 |

据此，博物馆值得先做人工核验的小目录。不能把这些标签直接当作今天可用的证据；例如样本中的 Justice & Police Museum 标签为周末开放，Susannah Place 为周四至周六，均不能直接用于本次周一／周二样本。

## 天气

Open-Meteo 返回 HTTP 200，timezone=Australia/Sydney，daily 日期为 2026-09-14 和 2026-09-15。原始响应见同目录 weather.response。只证明本次访问和日期覆盖；天气暂作展示，不影响 Replay 验证。

## TfNSW 多站往返实测

Trip Planner API 鉴权成功。最终样本对每一段依次查询：下一段的请求时间由上一段计划到达时间、场馆开放时间和探针停留时间共同产生，并向上取整到 API 支持的分钟。解析器将 product class 100 识别为步行，排除 School buses，并验证计划出发不早于请求时间。

| 样本 | 出发 | 停留点与访问窗口 | 返回 | 全程步行 | 换乘 | 结果 |
|---|---|---|---|---:|---:|---|
| 2026-09-14 两站 | Town Hall 14:47 | The Mint 15:01–15:21；Museum of Sydney 15:33–16:03 | 16:18 | 24 分钟 | 0 | observed_feasible |
| 2026-09-15 三站 | Town Hall 10:00 | Museum of Sydney 10:17–11:02；The Mint 11:13–11:43；Australian Museum 11:54–12:54 | 13:05 | 30 分钟 | 0 | observed_feasible |

三处开放和普通入场费用由场馆官方页面复核：Museum of Sydney 每日 10–17 时、免费；The Mint 周一至周五 9–16 时、免费；Australian Museum 每日 10–17 时、普通入场免费。20／30／45／60 分钟停留均为系统探针假设，不是场馆规定。坐标取自本日 OSM 对象中心，TfNSW 会把坐标端点吸附到附近地址或站点。

独立检查覆盖严格时间连续性、场馆窗口、17:00 回程截止、公众路线、请求数量和原始响应哈希。TfNSW 自 2023-10-16 起不再支持 `/trip` 的 Opal fare，因此明确预算下仍须标记费用未知或另接票价来源。数据集页面将 Trip Planner API 标为 Creative Commons Attribution；使用时署名 Transport for NSW，不暗示官方背书。技术文档建议路线数据不缓存或只短时缓存，以免呈现陈旧信息。

历史评测快照曾位于 `evals/provider-m0.json`（已删除），包含两条路线的摘要、原始响应路径与哈希，以及独立期望值。它明确标记 `live: false`，预算请求的期望状态为 `conditional`。生成命令为 `.venv/bin/python scripts/freeze_m0_replay.py`。

## DDGS 搜索与活动页定量探针

最终产物曾位于 `data/probes/ddgs-20260914T051842Z/report.json`（已删除）。使用 DDGS 9.16.0，固定 Bing 后端、`au-en`、moderate safe search；4 组悉尼市中心／Glebe／Surry Hills 日期查询各保留 Top 5。DuckDuckGo 和 Mojeek 后端在诊断中返回 No results，Bing 与 Brave 后端可返回结果；为避免依赖 Brave，正式探针选择 Bing。

| 指标 | 结果 | 缺失／失败 |
|---|---:|---:|
| 查询执行 | 4/4 成功 | 0/4 |
| 结果 URL | 20 次，18 个唯一 URL | 2/20 重复 |
| 落地页读取 | 17/18 成功 | 1/18（404 或请求失败） |
| 目标日期由 JSON-LD 覆盖 | 7/20 | 9/20 明确不匹配，4/20 未知 |
| 页面文本出现价格或 Free | 13/18 | 5/18 |
| 页面文本出现时间范围 | 17/18 | 1/18 |
| JSON-LD 包含结束日期 | 16/18 | 2/18 |
| JSON-LD 包含坐标 | 16/18 | 2/18 |
| JSON-LD 包含结构化价格 | 0/18 | 18/18 |

结果会随上游索引变化：前一轮同配置得到 19 个唯一 URL 和 9/20 日期匹配，最终轮得到 18 个唯一 URL 和 7/20 日期匹配。精确到 2026-09-14 的最终 Top 5 没有可靠覆盖目标日；这直接否定了“仅凭 DDGS 返回结果规划今天活动”。搜索层只保存标题、URL、排名、摘要哈希和字段标志，不保存完整摘要或网页正文。

结论：DDGS 可保留用于宽泛发现和人工研究；进入候选池前必须读取 allowlist 落地页，解析 JSON-LD 日期和坐标，并逐字段标记未知。价格只能从正文另行类型化提取，无法核实则为 unknown。首版 Live 不支持定时活动，因此不需要把这一脆弱链条接入 Validator。

## 活动与搜索人工抽查

使用本次助手浏览工具做定性发现，**不是项目搜索 API 基准**。查看了带 2026 年 9 月日期的展览／活动查询及以下落地页。只记录事实摘要与链接，不存储或再发布页面全文。

| 页面 | 核对结果 | 产品含义 |
|---|---|---|
| [Our World of Textiles](https://whatson.cityofsydney.nsw.gov.au/events/our-world-of-textiles) | 9 月 10–16 日，每日 11–18 时，免费；9 月 12 日 13 时是开幕活动 | 搜索摘要突出 12 日会遗漏展览持续窗口；今天／明天的参观应按展览窗口判断 |
| [The Big Design Market](https://whatson.cityofsydney.nsw.gov.au/events/the-big-design-market) | 2026 年 9 月 18–20 日，逐日时间不同；成人 $8 另加 booking fee | 不能推荐给 14／15 日；票面价格不等于已知完整费用 |
| [Australian Museum school holiday program](https://www.nsw.gov.au/visiting-and-exploring-nsw/nsw-events/fang-tastic-school-holiday-fun-at-australian-museum) | 页面列 7 月 3 日至 10 月 11 日及每日 10–16 时，并要求向主办方核对 | 标题中的 school holiday 不足以推断日期；需核对具体场次与参观含义 |

该人工抽查用于解释错误类型；定量结果以上述固定 Top-K 探针为准。多日随到随看的展览不能被现有 event=true（要求全程参加）语义直接承载。

## 来源访问、成本与使用条件

以下为官方文档核对记录；账户实际授权与合同条件优先。

| 来源 | 访问与成本／限流 | 署名、缓存、快照边界 | 本轮决定 |
|---|---|---|---|
| OSM / Overpass | 无 Key；共享公共实例。4 次中 3 次成功 | OSM 数据 ODbL，署名 OpenStreetMap contributors 并链接许可；数据库再发布遵循 ODbL。服务建议小规模、缓存和限流，公共实例不提供本项目 SLA | 保留原始开放数据样本；只作候选目录 |
| Open-Meteo | 无 Key 免费接口限非商业用途；低于 10,000/日、5,000/小时、600/分钟 | 数据 CC BY 4.0，保留署名；免费访问权限不等于商业服务权限。项目缓存 TTL 尚未确定 | 天气配套来源 |
| TfNSW | Key 鉴权与 7 个最终路段成功；账户实际配额未从响应头测得；Opal fare 已停止支持 | Trip Planner API 资源标为 CC Attribution；署名 TfNSW。路线高频变化，文档建议不缓存或只短时缓存 | 用作 Live 路线候选；预算验证需其他票价来源 |
| Ticketmaster Discovery | 无 Key 未调用；文档默认 5,000/日、5/秒，分页受限 | 条款限制缓存为提供服务所需的合理期间，不能据此宣称允许永久公开冻结原始内容；署名与费用待所选方案确认 | 活动备选，尚未选定 |
| DDGS 9.16 / Bing 后端 | 无 Key；4/4 查询成功，但 DuckDuckGo、Mojeek 诊断失败且重复运行结果变化 | DDGS 代码为 MIT；该许可不覆盖上游搜索结果或网页内容。只保存最小元数据和哈希 | 开发期发现工具；不进入首版 Live 验证链 |
| City of Sydney What's On | 18 个唯一页面中 17 个成功读取；多数有 JSON-LD 日期／坐标，结构化价格为 0/18 | 页面可读不等于获准批量缓存或全文再发布；本探针不保存正文 | 仅作 allowlist 落地页核对；定时活动退出首版 |

来源（核对日 2026-09-14）：

- [OSM copyright / ODbL](https://www.openstreetmap.org/copyright)
- [Overpass 实例及使用说明](https://wiki.openstreetmap.org/wiki/Overpass_API)
- [Open-Meteo terms](https://open-meteo.com/en/terms)
- [TfNSW Trip Planner dataset](https://opendata.transport.nsw.gov.au/dataset/trip-planner-apis)
- [TfNSW documentation](https://opendata.transport.nsw.gov.au/developers/documentation)
- [Ticketmaster Discovery](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/)
- [Ticketmaster terms](https://developer.ticketmaster.com/support/terms-of-use/)
- [Brave Search API / pricing / storage FAQ](https://brave.com/search/api/)
- [DDGS repository / MIT licence](https://github.com/deedy5/ddgs)

## 数据契约需补充的内容

后续 Live Adapter 应保留 provider、source_id、raw snapshot 引用、提取方式、字段状态与 source_updated_at；source_updated_at 和 valid_until 未知时必须允许未知，不能以 fetched_at 自动制造有效期。

金额至少区分 free / known_total / from_price / unknown，附 currency 和 booking fee 是否包含。时间应区分 fixed_session 与 visit_window，另存时区、日期范围、例外日期与最后入场时间。路线应区分 scheduled / realtime，保存入口坐标、换乘、步行、提醒和票价未知项。

现有 Candidate 的单一 open_at/close_at、event 布尔值和 Evidence 的强制有效期属于 Replay 简化模型；先形成真实样本，再新增 Live 契约，避免把未知事实填成演示默认值。

## 剩余验收关卡

- [x] 3 个区域真实地点响应、字段统计与失败记录；第 4 个 CBD 请求失败保留。
- [x] 今天／明天天气访问与日期覆盖。
- [x] 来源条件初查、定性搜索落地页对照。
- [x] 三处真实场馆逐页核对收费和开放窗口；坐标端点已由 TfNSW 解析。入口级坐标精度留给 Live Adapter。
- [x] TfNSW 鉴权、数据许可与缓存建议初查；完成今天两站和明天三站完整往返。
- [x] TfNSW 响应未暴露账户剩余配额；脚本按固定小批量运行。明确预算下因票价未知返回 conditional。
- [x] 完成 4 组日期／区域活动发现与字段统计；日期错误率过高，定时活动正式退出首版。
- [x] DDGS/Bing 定量探针完成；选为开发期发现工具，不作为首版 Live 依赖。
- [x] 冻结带来源哈希、署名边界和独立期望值的历史 Provider Replay。

