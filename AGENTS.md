# Repository Guidelines

## 仓库定位与运行形态

Polaris 是覆盖文献、研究想法、实验、写作与评审流程的 AI 科研平台。同一套 React 前端和 FastAPI 后端以两种形态交付：

- **Server**：Docker Compose 运行 `frontend`、`api`、`worker`、`kernel`、PostgreSQL/pgvector 与 Redis。前端通过 REST、SSE 和 WebSocket 访问后端。
- **Desktop**：Electron 主进程托管 `@polaris/kernel`，由内置 `legacy-engine` 启动同一 FastAPI 后端；使用 SQLite、进程内队列和本地会话。不要为桌面端复制业务逻辑。

运行数据写数据库或数据卷，不写源码树。确定性工作（抓取、解析、去重、指标提取）用普通代码完成；仅评分、综合、生成等判断性工作调用 LLM。

## 目录与模块地图

| 路径 | 职责 |
| --- | --- |
| `src/backend/app/main.py` | FastAPI 工厂；挂载 `/api`、`/ws`、`/mcp`。 |
| `src/backend/app/api/` | 薄路由：鉴权、参数解析和 HTTP 映射；总入口为 `router.py`。 |
| `src/backend/app/services/` | 可复用业务逻辑，不得导入 FastAPI。 |
| `src/backend/app/models/`、`schemas/` | SQLAlchemy 2 持久化模型与 Pydantic v2 请求/响应模型。 |
| `src/backend/app/core/` | 配置、数据库、Redis、队列、事件、安全与统一 `llm/` 边界。 |
| `src/backend/app/agents/`、`worker/` | 对话 Agent、持久化 Voyage 状态机及 ARQ 后台任务。 |
| `src/backend/app/tools/`、`mcp/` | 内部 Agent 与外部 MCP 共用的工具注册表、HTTP/stdio 服务和自检。 |
| `src/backend/alembic/versions/`、`src/backend/tests/` | 数据库迁移、pytest 测试和 `tests/golden/` 协议快照。 |
| `src/frontend/src/` | React 应用；`main.tsx`/`App.tsx` 为入口，`app/routes.tsx` 管路由，`features/` 按产品域拆分，`lib/` 放 API/SSE/WS/宿主桥接，`styles/` 放全局样式和设计令牌。 |
| `src/desktop/`、`src/kernel/` | Electron 主进程/预加载/IPC，以及必须保持 Electron-free 的 Cordis 插件内核。 |
| `plugins/`、`market/` | 官方示例插件和插件市场索引；插件入口必须是自包含的单文件 bundle。 |
| `integrations/` | 浏览器扩展、DeepSeek Harness 插件和 BYO runner；它们使用各自的依赖与测试命令。 |
| `docker/` | Dockerfile、基础 Compose、开发/生产覆盖层和 nginx 配置。 |
| `docs/`、`website/` | 英中项目文档、RFC/素材，以及以 `docs/` 为源的 VitePress 站点。 |
| `vendor/`、`spikes/` | vendored Cordis runtime/契约测试与 P0 原型；非对应任务不要顺手重构。 |

静态资产分别放在 `src/frontend/src/assets/`、`src/backend/app/assets/` 和 `docs/assets/`。内置流程/学科数据位于 `src/backend/app/packs/`、`src/backend/app/disciplines/`，论文模板位于 `src/backend/app/assets/templates/`。

## 架构边界与改动落点

后端遵循 `route → service → model/core`。长任务必须由 route/service 建立任务记录后入队，再由 `worker/tasks.py` 驱动 Voyage 或领域 service；不得占用请求线程。新增队列函数时，同时登记 `app/core/queue.py::WORKER_FUNCTIONS` 与 `worker/settings.py::WorkerSettings.functions`，并更新 `src/backend/tests/test_worker_registration.py`。

所有模型请求只能经过 `app/core/llm/`，业务代码不得直接导入供应商 SDK。模型路由来自数据库，调用必须保留用户、课题、Voyage/文献库等归属信息。新增 LLM stage 时同步后端路由、超时分类与前端 stage 清单。

工具 handler 只包装 `services/*`。在 `app/tools/` 用 `@tool` 注册后，还要在 `app/tools/__init__.py` 导入；默认 `scope="project"` 并校验 `project_id`，仅真正的用户级发现工具使用 `scope="user"`。新增必填参数时同步 `app/mcp/selfcheck.py` 的样例和缺失原因，否则自检会长期显示 `skipped`。

所有资源查询都必须保持租户边界：默认依赖 `current_active_user`，课题内对象复用 `services/projects.py::in_my_projects` 等统一 guard。越权对象返回 404/`None`，不要泄露其是否存在；不能因为用户能访问另一个课题，就放宽当前请求携带的对象 ID。

前端页面放入对应 `features/<domain>/`，跨域基础设施放 `lib/`，共享展示组件放 `components/` 或 `features/shared/`。服务端状态统一使用 TanStack Query；不要在组件中用 `useEffect` 手写请求。新页面同时更新 `app/routes.tsx`，颜色使用 `styles/tokens.css` 的令牌。

桌面宿主能力要保持 `src/desktop/src/shared/contract.ts`、preload 暴露和 main IPC handler 一致。`src/kernel/` 不得导入 `electron`；该约束由 `src/kernel/tests/electron-free.test.ts` 固定。插件安装与启用是两个阶段：安装后默认禁用，入口哈希会在启动和启用时复验。

## 安装与开发命令

CI 基线为 Python 3.12、Node.js 22；根 `package.json` 固定 pnpm 版本。首次准备：

```bash
cp .env.example .env
corepack enable
pnpm install --frozen-lockfile
make venv
```

常用运行方式：

```bash
make dev          # 构建 TeX 基础镜像并启动热重载全栈
make logs         # 跟踪 Compose 服务日志
make down         # 停止开发栈
make backend-dev  # 本地 FastAPI，http://localhost:8000
make frontend-dev # 本地 Vite，http://localhost:5173
make migrate      # 使用本地 venv 执行 alembic upgrade head
make desktop-dev  # 构建前端并启动 Electron
make desktop-shell # 不重建前端，直接启动 Electron 壳
make desktop-dist # 生成当前平台的未签名安装包
```

需要单独运行服务端 worker 时使用 `cd src/backend && .venv/bin/arq worker.settings.WorkerSettings`。Docker 开发态的 `arq --watch` 只重载 settings；修改 worker 已导入的 `app/*` 模块后，执行 `docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml restart worker`。

Docker 首次启动及拉取含新迁移的版本后，显式执行：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml \
  exec api alembic upgrade head
```

质量检查：

```bash
make test    # 后端 pytest + 前端 Vitest + 前端构建
make lint    # 后端 Ruff + 前端/桌面 tsc
pnpm test    # 递归运行 pnpm workspace 中定义的 Node 测试（含 kernel）
pnpm lint    # 先生成 vendor 类型，再运行各 workspace lint
make build   # 构建生产 Compose 镜像
```

`make test` 不包含 kernel 或 desktop；完整 Node 回归需额外运行 `pnpm test`，桌面端再按改动风险运行 `pnpm --dir src/desktop run smoke` 或 `e2e`。同样，`make lint` 不覆盖 kernel/plugin，应以根目录 `pnpm lint` 补齐。

`website/` 和 `integrations/` 不在 pnpm workspace 中；在各自目录使用其 lockfile 和脚本，例如 `npm --prefix website ci && npm --prefix website run build`、`npm --prefix integrations/deepseek-harness run check`、`npm --prefix integrations/polaris-browser-extension run check`。

## 编码风格与命名

- Python 使用 4 空格、100 字符行宽及 Ruff `E/W/F/I/UP/B/SIM`；模块、函数、fixture 用 `snake_case`，类和 Pydantic/SQLAlchemy 模型用 `PascalCase`，常见 schema 后缀为 `Create`、`Update`、`Read`。异步 I/O 使用 `async def` 与 `AsyncSession`。
- TypeScript 开启严格模式，使用 2 空格；组件/文件用 `PascalCase.tsx`，变量和函数用 `camelCase`，Hook 以 `use` 开头。仓库没有统一 Prettier/ESLint 配置，修改时跟随相邻文件的引号、分号和导入顺序，并以 `tsc --noEmit` 为准。
- Alembic 迁移用 `.venv/bin/alembic revision -m "description"` 生成随机 revision id；不要手写递增编号，也不要为格式化而改动历史迁移。

## 测试规范

后端使用 pytest，`asyncio_mode=auto`，测试命名 `test_*.py`；默认以 SQLite、fakeredis 和 fake LLM 隔离运行，不选择 `docker_integration`。前端/内核使用 Vitest，命名 `*.test.ts[x]`，通常与功能同目录放在 `__tests__/`。没有统一覆盖率数字门槛；每个行为变化必须有最小回归测试。

```bash
cd src/backend && .venv/bin/pytest -q tests/test_target.py
pnpm --dir src/frontend exec vitest run src/features/path/example.test.ts
pnpm --dir src/kernel test
pnpm --dir src/desktop run e2e
```

Golden 测试按字节固定外部协议行为。失败时先读 `docs/golden-policy.md`；只有有意的 wire change 才运行 `make golden-record`，逐 hunk 审查 JSON 差异，然后在非 record 模式连续验证稳定性。迁移 PR 还需验证 upgrade、downgrade roundtrip 与 `alembic heads` 只有一个 head。

## 配置、安全与生成物

`.env`、数据库、日志、桌面 staging 资源均被忽略；只提交 `.env.example`。应用变量使用 `POLARIS_` 前缀。不要提交 API key、集成 token、邮箱授权码、SSH 凭据或真实数据。生产环境必须设置独立的 `POLARIS_SECRET_KEY`、`POLARIS_ENCRYPTION_KEY` 和 `POLARIS_KERNEL_TOKEN`；更换 Fernet key 会使既有加密凭据无法解密。

Server 的 `kernel` 端口不得对外发布，其身份校验依赖 API 侧 owner guard 与共享 token。远程实验命令只能来自 `services/ssh_exec.py` 的白名单模板：不要让 LLM 拼接 shell，变量必须验证 UUID、整数或受限相对路径，并对日志/异常执行 secret scrub。

插件 `permissions` 在 v1 仅展示、不提供沙箱隔离，应把第三方插件视为受信任代码。一般构建产物不入库；插件 bundle 是例外，当前官方 seed 插件的 `plugins/polaris-plugin-hello/dist/index.js` 被刻意跟踪，修改源码时要同步重建并审查 bundle。不要提交 `src/desktop/resources/`、`vendor/**/lib/` 或运行时 `data/`。

## 提交与 Pull Request

Issue、提交、分支语义和 PR 文案使用英文。先建 Issue；一项工作对应一个分支、worktree 和 PR，分支使用 `feat/`、`fix/`、`chore/` 或 `docs/`。提交与 PR 标题遵循 Conventional Commits，例如 `feat(reading): add zoom controls`。

`main` 是 `origin/main` 的只读镜像：不要直接提交，也不要把 feature branch merge 进 `main`；更新功能分支时执行 `git fetch origin && git rebase origin/main`，随后用 `--force-with-lease` 推送。PR 先以 Draft 创建，说明 what/why、实际运行的测试并写 `Closes #N`；UI 改动附截图。禁止 AI attribution 或 `Co-Authored-By` 尾注。数据库、协议、插件 bundle 或 golden 有变化时，在 PR 中明确列出对应验证和人工审查结果。
