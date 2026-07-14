# Codex 异常自愈机制

这套机制用于处理两类信号：代码抛出异常，以及任务预期结果没有发生。它会先停止本批后续自动化并释放游戏资源，再保存现场、去重，最后异步启动 Codex。主任务线程不会同步等待 Codex。

## 能力与边界

首版包含四种能力：

1. **自发现**：任务队列和后台调试器捕获异常、资源启动失败、明确的非成功返回值；GUI 启动边界会直接上报启动崩溃；日志监视器只扫描启用后 `debug.log` 新增且尚未被结构化捕获的 `ERROR`、`CRITICAL` 和 traceback。
2. **自调试**：Codex 读取结构化事故、近期脱敏日志、Git revision 和同指纹历史，默认在只读沙箱中分析根因。
3. **自优化**：第二开关明确授权后，Codex 在独立 Git 工作树中形成最小候选修改，并可在同一操作系统沙箱内运行相关测试；宿主运行器不会自动执行候选代码。
4. **自学习**：本地经验库累计稳定指纹、出现次数和修复结果，供下次诊断参考。这是可审计的经验复用，不是在线训练模型，也不会自行改写策略。

首版能发现“返回失败”与“抛出异常”。如果任务死循环、长期卡住且没有返回，当前线程无法进入清理和上报阶段；这需要后续增加独立进程 watchdog、任务心跳和协作式停止。

## 开关

在“设置 → Codex 自愈”中：

- **启用 Codex 自愈智能体**：默认关闭。开启后，事故会触发 Codex 诊断，同时本批队列熔断，避免后续任务在未知状态下继续操作游戏。
- **允许生成隔离修复**：默认关闭。只有第一开关和本开关同时开启，Codex 才能修改隔离工作树。它仍不会把修改应用到当前分支。

即使第一开关关闭，任务边界仍可把事故作为本地诊断记录保存，但不会启动 Codex，也不会改变原有“失败后继续后续任务”的行为。

## 处理流程

```text
异常 / 非成功结果 / 新日志异常
                │
                ▼
        缓存结构化事故现场
                │
                ▼
       释放游戏和模拟器资源
                │
                ▼
  脱敏落盘 + 稳定指纹 + 经验计数
                │
       冷却去重 / 单实例派发
                │
                ▼
   Codex 只读诊断 ── 第二开关 ──► 隔离工作树候选修复
                                      │
                                      ▼
                              人工审阅、决定是否采用
```

同一队列批次的业务失败和 cleanup 失败共享 `batch_id`，只允许第一条非 cleanup 事故派发 Codex，其余仅保留为关联证据。用户停止、窗口关闭和 `StopExecution` 不作为待修复事故。

## 本地产物

默认位于 `logs/self_healing/`，该目录已被 Git 忽略：

- `incidents/<id>.json`：脱敏后的单次事故现场；
- `experience.json`：同指纹出现次数、来源和修复结果；
- `log_cursors.json`：增量日志游标，首次启用默认从文件末尾开始；
- `dispatch/<fingerprint>.json`：同指纹一小时冷却声明；
- `dispatch/pending/` 与 `dispatch/global-runner.json`：原子发布的持久待处理队列和带启动租约的全局单运行器声明；不同故障会串行诊断，不会同时启动多个 Codex；
- `runs/`：Codex 运行状态、输出和待人工验证的候选状态。

诊断快照和候选修复工作树保留在仓库同级的 `.HeiYue_Auto_Resonance_self_healing/`，不会自动删除；它位于主工作树之外，便于权限配置整体拒绝主目录读取。

密码、token、cookie、授权头、API key 等敏感键和值会在递归序列化时脱敏。现场序列化限制为 8 层、每个集合 50 项、单段文本 2 万字符和总文本预算 10 万字符；日志每轮最多读取 1 MB、生成 100 条候选事故，避免异常输入耗尽内存。

## 安全设计

- 运行器使用 Codex 0.143+ 自定义 permission profile，而不混用旧式 `-s` 沙箱：继承官方 `:workspace` 基线后把文件系统根目录和通用临时目录重新设为拒绝，只开放最小运行时、主仓库 `.venv` 的只读执行依赖、隔离 worktree 和专用临时目录；主仓库其余内容显式拒绝，诊断 worktree 只读，候选修复 worktree 可写，网络关闭。
- 隔离 worktree 中的 `.git`、`.codex` 和 `AGENTS.md` 始终只读。宿主在候选生成后、运行任何 Git 检查前，还会验证 linked-worktree `.git` 指针内容与目标未变化，防止候选注入 Git 配置或命令。
- Codex 以 `-a never`、`--ephemeral` 和 `--ignore-user-config` 运行，不允许交互批准、联网搜索或额外可写目录。
- Codex 子进程只继承运行所需的环境变量白名单，不继承 API key、token、cookie、代理凭据、SSH agent 或 askpass 配置；`HEIYUE_TEST_PYTHON` 只指向只读的仓库虚拟环境，供沙箱内验证候选。
- 子进程携带 `HEIYUE_CODEX_REPAIR=1` 和主工作树的绝对 `HEIYUE_RUNTIME_DIR`。ADB、NEMU、MuMuManager 与截图/点击入口在该标记下会拒绝执行。
- 运行器不在宿主机自动执行模型生成或修改的测试，也不执行自动提交、合并、推送、补丁应用、任务重启或工作树删除。候选统一标记为 `candidate_unvalidated`，必须人工审阅后再验证。
- 候选一旦触碰 ADB/NEMU guard、自愈策略、运行锁、`AGENTS.md` 或安全测试等受保护路径，会直接标记为策略违规，不会显示为“验证通过”。
- 主工作树脏、事故 revision 不匹配、Git/Codex 缺失或超时时，生成修复会安全失败并留下状态记录。

这些 Python guard 属于纵深防御，不是操作系统级的绝对隔离。被授予 `workspace-write` 的智能体理论上能修改代码或直接调用外部程序，因此第二开关默认关闭，候选结果始终要求人工审阅。

## 手动检查

使用仓库虚拟环境：

```powershell
.\.venv\Scripts\python.exe self_heal_runner.py status
```

对已落盘事故只做诊断：

```powershell
.\.venv\Scripts\python.exe self_heal_runner.py process logs\self_healing\incidents\<id>.json --enabled --mode diagnose
```

明确允许生成隔离候选修复：

```powershell
.\.venv\Scripts\python.exe self_heal_runner.py process logs\self_healing\incidents\<id>.json --enabled --mode repair
```

`--enabled` 是命令行防误触门槛；`repair` 仍只生成隔离候选，不代表自动采用。

## Codex 运行依据

运行器按 OpenAI 官方的非交互模式和沙箱能力调用 Codex：

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/)
- [Codex SDK and programmatic control](https://developers.openai.com/codex/sdk/)
