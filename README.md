# Sidequest

Sidequest 根据当前位置、可用时间和一句偏好，生成当天可完成的悉尼小行程。当前版本包含确定性 Replay 和收缩范围的 Live Adapter：CBD 常设场馆、公园与 TfNSW 路线。它支持 1–3 个站点、完整往返、Google Maps 导航、时间与活动预算验证、局部修改、偏好存储、Trace 和 SSE 进度。

> 当前地点名称用于界面演示，开放时间、费用和交通时长均为合成数据。`verified` 只表示方案通过冻结回放场景的约束检查，不可作为真实出行依据。

配置 API key 后界面默认使用 Live 模式，并以当前悉尼时间作为可编辑的出发时间。用户点击“使用当前位置”后，浏览器才请求定位权限；坐标作为起点参与附近候选排序和完整往返验证，并保存在本地行程记录中。TfNSW 密钥不会进入请求参数、日志、缓存或结果。

预算默认只约束门票和活动费用。公共交通费另计，也不会仅因 TfNSW 缺少 Opal 票价而把方案标成 `conditional`；用户可以主动选择把交通费纳入预算。每个结果都提供无需 Google API key 的 Google Maps 整条路线链接。

## 本地运行

需要 Python 3.12+ 和 Node.js 20+。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm install --prefix frontend
npm run build --prefix frontend
PYTHONPATH=backend .venv/bin/uvicorn sidequest.api:app --reload
```

打开 <http://127.0.0.1:8000>。首次访问会创建仅保存在浏览器 Cookie 中的随机会话标识；行程和偏好写入项目目录下的 `sidequest.db`。

开发前端时可分别运行：

```bash
PYTHONPATH=backend .venv/bin/uvicorn sidequest.api:app --reload
npm run dev --prefix frontend
```

Vite 会把 `/api` 转发到端口 8000，前端地址是 <http://127.0.0.1:5173>。

## 验证

```bash
.venv/bin/pytest -q
.venv/bin/ruff check backend
npm run build --prefix frontend
PYTHONPATH=backend .venv/bin/python scripts/run_eval.py
```

最后一条命令运行冻结的 16 个开发场景和 8 个保留场景，将逐案例结果写入 `reports/latest-eval.json`。它是确定性软件评测，不包含模型质量或真实数据准确率。

## 主要目录

- `backend/sidequest/`：请求模型、回放数据、规划器、验证器、持久化与 API。
- `frontend/`：React + TypeScript 单页界面。
- `evals/scenarios.json`：24 个冻结场景和独立期望条件。
- `reports/`：评测产物。
- `docs/architecture.md`：状态、证据、缓存和修改语义。
- `docs/data-feasibility.md`：真实数据接入前的已验证事实与待办。

## 现在能证明什么

- 多站行程按完整链条验证，单个地点各自可行不代表组合可行。
- `unknown` 不会被标成 `verified`；预算、营业时间或活动结束时间缺失会产生 `conditional`。
- 用户锁定的站点不会被静默替换，迟到结果不能覆盖取消状态。
- 路线缓存包含精确出发时刻；时间改变后会重新计算受影响路段。
- 每个会话独立读取行程和偏好。“换一个”的理由和“就这个”会记成经历；同一个意思在不止一次出门里反复出现，系统才提议记住，用户确认后才写入长期偏好。偏好抽屉里可以查看每条记忆的生效时段和依据，删除记忆或经历，或暂停记录。

TfNSW Live Adapter 已接入后端 Provider 契约，包含响应解析、学校巴士过滤、出发时刻约束、未知票价、类型化错误和短期路线缓存。前端可在 Live 与 Replay 间切换；Live 候选已加入 Royal Botanic Garden 和 Barangaroo Reserve。场馆页面的自动刷新仍是下一阶段。

M0 于 2026-09-14 通过：完成地点、天气、TfNSW 交通和 DDGS 活动发现实测（探针脚本与大部分原始快照已于 2026-09-15 收敛时删除，结论保留在文档中）。今天两站和明天三站的 7 个顺序路段通过时间连续性检查并冻结为历史 Provider Replay。DDGS 精确日期召回不足，故只保留为开发工具；首版 Live 范围收缩为悉尼 CBD 的少量常设场馆。查看 [探针报告](docs/data-feasibility.md)。
