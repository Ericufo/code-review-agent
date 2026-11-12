# 仓库交接与工作约定

最后核验：2026-11-12。新 session 先读本文，再执行 `git status --short --branch`
和 `git log -5 --oneline`；本文的提交号是核验快照，不替代最新 Git 状态。

## 当前开发状态

- Python 代码审查工具：CLI / GitHub PR 输入、LangGraph 审查流程、受限仓库工具、
  冻结评测、FastAPI + PostgreSQL 单实例任务服务、可选本地 RAG。
- 当前分支 `feature/v4.5-rag`，功能提交 `7bfe1ba`；`main` 为 `6ef12b3`（V4.4）。
  本次通过 HTTPS `git ls-remote` 实时核实远端两分支也分别是这两个提交。
- [PR #7](https://github.com/Ericufo/code-review-agent/pull/7) 仍 open、非 draft、未合并；
  GitHub API 报告 mergeable=true、mergeable_state=unstable。
- `7bfe1ba` 的 test / api-postgres / compose / rag 检查均成功；review 检查失败。
  [离线 CI](https://github.com/Ericufo/code-review-agent/actions/runs/41998719335)
  与 [模型审查](https://github.com/Ericufo/code-review-agent/actions/runs/41998719323) 是不同验收。
  历史本地记录说明审查仅完成 23/31 个范围内文件，7 失败、1 跳过，不能称全覆盖通过。
- 本次接手时遗留四个未提交文件，构成两项完整修复，已经核对并通过回归，随本次收尾保存：
  - `main.py` / `tests/test_main_context.py`：本地无变更时先退出，不初始化可选 RAG。
  - `evals/live.py` / `tests/test_eval_live.py`：配置、依赖、模型或数据库初始化失败时，
    安全退出、避免暴露异常内的敏感值、不发生成请求，并释放已创建的数据库引擎。
- V4.5 工程实现已完成，仍待修复提交的远程 CI 和 PR 合并。V4.6 / V4.7 仅有规划，
  不要把规划误报成实现，也不要在 V4.5 收尾任务中自动扩展开发它们。

## 本次验证及限制

使用现有 `.venv/Scripts/python.exe`（PowerShell 写作 `.\.venv\Scripts\python.exe`）：

| 命令 | 2026-11-12 结果 |
| --- | --- |
| `python -m unittest discover -s tests -v` | 293 项，292 通过、1 跳过、0 失败 |
| `python -m evals.dataset` | 20 个案例及冻结 hash 校验通过 |
| `python -m evals.runner --split all --output reports/handoff-20261112/offline` | 45 条 execution checks 全部符合预期 |
| `python -m pip check` | 无依赖冲突 |
| `git diff --check` | 通过 |

跳过项为现有 Windows 符号链接权限测试。离线 runner 包含预期 INCOMPLETE 的故障注入，
不能把 45 条检查称为 45 条真实模型审查成功。报告在忽略的 `reports/handoff-20261112/offline/`。
本次无 UI 变更，截图不适用，无付费模型调用。

Docker Desktop Linux engine 当前不可用，未重跑 PostgreSQL / RAG / Compose 集成验收。
历史真实数据库、ONNX、pgvector 和容器通过证据在本地 `docs/v4.5-closeout.md`、
`docs/v4.5-prove-it-audit.md` 和对应 reports 中；历史结果不冒充本次修复的远程 CI。

SSH fetch 因 github.com 解析失败，未更新 FETCH_HEAD；HTTPS 只读查询成功。
`gh` 当前未登录；PR / checks 使用无凭据的公开 GitHub REST API 核实。
网页搜索缓存较旧，不用于判断当前 PR 状态。本次没有推送、合并或发布评论。

## 下一步

1. 核对本次本地收尾提交与远端差异，恢复 GitHub 认证和 Git 网络连接。
2. 将修复交付到 PR #7，核验新 SHA 的四项离线 CI。现有 PR 非 draft，
   `.github/workflows/review.yml` 的 synchronize 会调用付费模型并发布评论；
   未明确授权这些副作用时，不直接推送触发它。可先与用户确定 draft / 仅离线交付方案。
3. 明确处理 review 失败后再合并，保留 INCOMPLETE 历史；不能用旧 SHA 的绿色检查替代新提交验收。
4. 同步 main，更新本文的交付状态，之后再按用户需求开启 V4.6。
5. V4.6：单实例、文件完成边界的显式恢复，复用成功结果，固定版本/配置，attempt 历史、
   累计预算和未知用量保守记账；不承诺外部模型请求 exactly-once。
6. V4.7：演示界面、浏览器验收与架构说明。独立 worker、分布式租约、节点级恢复和发布恢复留后续。

## 开发入口

- `main.py` → `review_service.py` → `review_graph.py` / `review_pipeline.py`：输入、配置、执行。
- `context_review.py` / `review_tools.py` / `repository_context.py`：固定版本的源码工具、预算与引用证据。
- `repository_chunks.py` / `rag_embedding.py` / `repository_rag.py`：不可变 Python 块、校验过的本地模型、pgvector。
- `review_api/` / `migrations/`：HTTP、执行器、事务存储、增量迁移。
- `evals/`：冻结案例、离线 runner、显式 live runner 和评分。
- `README.md`：安装和使用细节；`requirements.txt` 为核心依赖，API / RAG 有各自附加依赖文件。

Docker 可用时按改动范围选用集成检查；脚本创建并清理自己拥有的隔离资源：

```powershell
.\.venv\Scripts\python.exe scripts/verify_rag.py --probe-guards
.\.venv\Scripts\python.exe scripts/verify_compose.py --rag
# 独立 PostgreSQL/API 检查需要本地专用 review_test_admin URL：
.\.venv\Scripts\python.exe scripts/run_postgres_tests.py --env-file .env.pgtest
```

## 持续约定

- 先检查现有改动，不用 reset/clean/stash 掩盖未完成工作；本地检查与远程交付分开报告。
  本次用户要求整理仓库，允许将已验证修复与交接文档保存为本地提交；旧 docs 中
  “Git 全由用户操作”的记录是历史 session 状态，不作为永久禁止本地收尾的规则。
- 不打印密钥，不提交 `.env*` 实值、`.venv/`、`.rag-models/`、`reports/`、`experiments/`。
  `docs/` 也被忽略；新克隆没有这些文档，因此关键交接内容必须维护在本文。
- 不删除生产卷或已有数据以解决测试问题；仅操作测试脚本确认归属的隔离数据库/容器。
- 默认 literal 检索，向量检索保持 opt-in；更换模型/预处理必须建立新的索引身份。
- 检索相似度不是缺陷证据；只允许引用实际返回且经固定版本源码回读验证的内容。
  工具失败/预算耗尽不能伪装完整审查，不重写冻结评测锁来掩盖漂移。
- 历史 live 对照实际 `retrieve_code` 调用为 0，不能宣称 RAG 提升最终审查质量。
  历史剩余额度不是新付费评测授权；不为得到更好结果自动重跑。
- 交差时给出实际命令、通过/失败/跳过数及未验证范围；更新本文，不复制历史成功为当次结果。
