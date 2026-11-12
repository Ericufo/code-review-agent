# Coding agent instructions

## 项目与代码入口

这是 Python 代码审查工具，支持本地 Git diff、GitHub PR、LangGraph 审查流程、
FastAPI 任务 API 和可选本地向量检索。安装、使用与部署细节见 `README.md`。

| 改动范围 | 主要入口 |
| --- | --- |
| CLI 与输入准备 | `main.py`、`git_utils.py`、`github_*.py` |
| 共享审查逻辑 | `review_service.py`、`review_pipeline.py`、`review_graph.py` |
| 仓库上下文、工具与引用 | `repository_context.py`、`review_tools.py`、`context_review.py` |
| 切块、embedding 与向量存储 | `repository_chunks.py`、`rag_embedding.py`、`repository_rag.py` |
| HTTP、执行器与数据库 | `review_api/`、`migrations/` |
| 冻结案例、评测与评分 | `evals/` |

## 环境配置

从仓库根目录执行命令，优先复用 `.venv`。以下命令中的 `python` 指虚拟环境解释器；
Windows PowerShell 可用 `.\.venv\Scripts\python.exe`，Linux/macOS 可用 `.venv/bin/python`。
按 CI 验证环境，核心开发推荐 Python 3.11，API/RAG 开发推荐 Python 3.13；
修改核心代码时保持 3.11 兼容。以下安装命令直接使用虚拟环境解释器，无需激活。

Windows PowerShell：

```powershell
py -3.11 -m venv .venv  # 仅在尚无虚拟环境时；API/RAG 使用 -3.13
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

POSIX shell（Linux/macOS）：

```sh
python3.11 -m venv .venv  # 仅在尚无虚拟环境时；API/RAG 使用 python3.13
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

- API/数据库开发安装 `requirements-api.txt`，它包含核心依赖。
- RAG 开发安装 `requirements-rag.txt`，它包含 API 依赖。真实 RAG 测试还需要 Docker
  和本地模型；用 `python scripts/download_embedding.py` 显式下载并校验模型。
- 保持 CLI 核心路径不依赖 API/RAG 的可选包；不要在公共入口顶层引入这些可选依赖。

## 实现约定

- 遵循所改模块现有的 Python 风格：四空格缩进、snake_case 函数名、PascalCase 类名。
  公共接口使用类型标注；沿用已有 Pydantic 模型、dataclass 和依赖注入方式。
- CLI 与 API 的共享行为放在 service/pipeline 层，避免在入口中复制审查逻辑。
  外部调用通过已有 provider/client 边界注入，方便离线验证。
- 资源由创建方负责关闭；数据库、文件及客户端的异常路径也要释放资源。
  不把未经处理的外部异常、连接串或源代码写入面向用户的错误信息。
- 数据库结构变更添加 Alembic 迁移，并验证现有数据的升级路径。
- 保持预算、版本与证据约束：源码读取绑定固定 revision，引用必须来自实际返回且验证过的源码。
  工具错误、未处理文件或预算耗尽应保留失败/不完整状态，不能转成“无问题”。
- 向量检索保持可选；模型、预处理或切块身份变化要隔离索引，不能混用。
  索引完整构建后事务发布，检索候选要回读固定版本源码验证。
- 冻结评测案例或标签的有意变更需要版本化；不要重生成 lock 文件来绕过校验失败。

## 验证命令

Python 代码改动先跑相关测试，再跑核心套件。测试使用标准库 `unittest`：

```sh
python -m unittest discover -s tests -p test_main_context.py -v
python -m unittest discover -s tests -v
git diff --check
```

第一条的文件名按改动替换。测试应验证可观察行为与故障处理；离线单测可替换外部模型/GitHub，
数据库持久化和真实检索行为需用下面的集成测试验证。不要把 mock 通过当作模型质量证据。

| 改动涉及 | 补充验证 |
| --- | --- |
| 评测数据、runner 或审查流程 | `python -m evals.dataset`；`python -m evals.runner --split all` |
| API、存储、执行器或迁移 | `python scripts/run_postgres_tests.py --env-file .env.pgtest` |
| embedding、向量存储或检索接入 | `python scripts/verify_rag.py --probe-guards` |
| 基础 Docker/Compose | `python scripts/verify_compose.py` |
| RAG Docker/Compose | `python scripts/verify_compose.py --rag` |
| 依赖文件 | `python -m pip check` |

- PostgreSQL 测试需要本地专用 `review_test_admin` 数据库；配置方法见 README。
  runner 创建独立临时数据库，不能传入生产库。RAG/Compose 验收需要可用的 Docker 引擎。
- 默认 `unittest discover -s tests` 不代替 `tests/postgres/`、`tests/rag/` 的集成验收。
- `evals.runner` 的故障注入预期产生 INCOMPLETE；检查 execution checks 是否符合预期。
- 纯文档改动核对命令、路径、链接并运行 `git diff --check`，不必重跑应用测试。
- CI 具体配置见 `.github/workflows/tests.yml`。交付时报告本次运行的命令与结果；
  环境不满足时说明未验证的范围，不引用历史成功来代替本次验证。

## Git、凭据与外部副作用

- 开始修改前检查 `git status --short --branch` 和相关 diff，保留用户已有工作。
- 不提交真实 `.env*`、模型、虚拟环境或运行报告。共享配置使用 `.env.example`
  与 `.env.compose.example`；遵循 `.gitignore`，不强制加入本地生成物。
- `docs/`、`reports/`、`experiments/` 被忽略，新克隆可能没有；必需开发指令应放在
  受版本控制的文档中。阶段状态、PR 进展与历史测试成绩不要写入本文件。
- 模型调用可能收费，GitHub 审查可能发布评论。离线检查不需要真实凭据；
  执行 live 评测或发布前确认当前任务授权覆盖其范围与预算。
- 推送前检查 `.github/workflows/review.yml`：符合条件的非 draft PR 会在更新时自动调用
  模型并发布评论。仅离线任务不应无意触发该流程。
- 集成测试仅清理脚本确认归属的临时数据库、容器与卷；保留已有环境的数据。
- 提交说明描述具体行为及原因；PR 说明包含改动、验证及限制，遵循已有 `feat:` / `fix:` / `docs:` 风格。
