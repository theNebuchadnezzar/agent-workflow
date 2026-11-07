# aiwf

> **AI 开发工作流：上下文打包器 + 状态机 + 验证门禁 + 验收条件兑现检查。**
>
> 不生成代码——生成交给模型。它解决模型做不了的三件事：**上下文供给、状态推进、结果验证。**

纯 Python 标准库，单文件 47KB，无依赖。

---

## 它解决什么问题

用 AI 写代码，卡点通常不在"写"，而在三处：

| 卡点 | 表现 | aiwf 的做法 |
|---|---|---|
| **上下文断了** | 新开 session 就失忆，反复让 agent 重新探索代码库 | `context` 把需求/决策/计划/交接指针打成一份可直接喂给任何 agent 的 Markdown |
| **过程不可追溯** | 改了一大堆，说不清哪处服务于哪条需求，更说不清有没有漏做 | 每条改动必须挂它兑现的**验收条件（AC）**，`acs` 随时看哪条还没人认领 |
| **验证不可信** | 让同一个 agent 自评"做完了吗"，它永远说做完了 | 三个检查点：规则门禁 + 独立 agent 对账 + 独立 agent 评审 |

**核心判断：AI 的可靠性不是靠模型变强解决的，是靠把「不可验证的部分」变成「可验证的」。**

---

## 为什么不做成 agent

| 做成 agent | 做成工具链 |
|---|---|
| 要自己封装 LLM、维护对话 | 继续用 Codex / Claude / 任何 agent，换工具不换流程 |
| 难评测、难复现 | 每步产出是文件，可 diff、可回放 |
| 和现有习惯冲突 | 只是把 story 目录规范化 + 加校验 |

编排能力必须落在**工具（Tool Use）**上，不能靠提示词——这是照抄 DeepSeek Super-agent 的结论。

---

## 目录协议

```
<dataDir>/                          # 默认 ./stories，可用 AIWF_DATA 指到仓库外
  2026-11-04-SHIP-1024-fix-login-npe/
    meta.json                       # 状态、tracker 号(key)、仓库列表、handoff 交接指针
    requirement.md                  # 需求 + 澄清问答 + 编号化的验收条件（AC-1、AC-2…）
    decisions.md                    # 决策记录：结论 ← 被什么驱动 ← 何时可推翻
    plan.md                         # 实施方案：改哪些文件、为什么
    changes.jsonl                   # 每次改动一行；每条挂它兑现的 AC
    ac-check.jsonl                  # 验收条件兑现检查（可多次）
    defects.jsonl                   # 缺陷事件流（open/fix/verify/wontfix）
    verify.json                     # 自检：编译/测试结果
    review.json                     # 独立评审结论（必须换 session）
    archive/                        # reopen 时归档的旧 verify/review/ac-check（留档但失效）
```

---

## 状态机

```
new → clarifying → planned → implementing → verifying → reviewing → reviewed → done
                                                    ↑                        │
                                                    └──── review fail ───────┘

任意阶段 ──(被卡住)──→ 打 ⏸ 标记（不是阶段，解除后回到原阶段）
```

> **`blocked` 是标记，不是阶段**：`record --blocked "等需求方"` 只打标记——`status` 显示 `⏸`、`context` 带原因、`gate` 给提示，**阶段不变**。
> 理由：被卡住是**原因**，不是**进度**；做成阶段会让「这个 story 到底在实施还是被卡住」变成二选一。
>
> **`reopen` 可以从任意阶段退回 `clarifying`**（需求追加时用）：旧的 `verify` / `review` / `ac-check` 会移到 `archive/` 归档失效，历史保留但不能再当证据。

### 三个检查点，各管一段

| 检查点 | 谁做 | 看什么 | 落地文件 |
|---|---|---|---|
| **gate** | 程序（规则） | 每条 AC 有没有人认领、有没有改动没挂 AC | 无（只读文件，零成本） |
| **ac-check** | **独立 session 的 agent** | **每条 AC 是不是真的被兑现了**（挂编号但说明对不上 = 未兑现） | `ac-check.jsonl` |
| **review** | 独立 session 的 agent | **代码质量**：规范、边界、可维护性 | `review.json` |

**三者不可互相替代**：

- `gate` 抓不到"挂了个编号但没真做"（它只看字段）
- `ac-check` 抓不到"代码写错了"（**它不读代码**）
- `review` 抓不到"漏做了一条需求"（**它只看已写出来的代码，没有的东西看不见**）

一句话：**`ac-check` 查「做对的事」，`review` 查「把事做对」。**

### 每阶段门禁（累积的）

| 阶段 | 必须满足 |
|---|---|
| clarifying 起 | 澄清问答非空，且**验收条件至少有一条 `- [ ] 实质内容`**（空复选框不算） |
| planned 起 | plan.md 的「改哪些文件」表有真实数据行 |
| implementing 起 | changes 至少一条；**每条 code/test 改动都挂了 AC**；**每条 AC 都有人认领**（转交的除外）；**AC 文本改过的旧认领不算数**；最近一次 ac-check 不是 `block` |
| verifying 起 | **不能有未修（open）的缺陷**；**必须做过一次 ac-check（pass/warn）**；**verify 最近一次结果必须是 `pass`**；**若 verify 记了 `commit`，必须与 handoff 的提交一致** |
| reviewed 起 | **不能有「修了但没复测」的缺陷**；review.json 存在、`independent=true`、`verdict=pass`；**若 review 记了 `commit`，必须与 handoff 的提交一致**；**评审之后不能再有 code/test 改动**（否则评审过期，要重评） |

---

## 验收条件兑现检查（核心机制）

**AC = Acceptance Criteria（验收条件）**——这条需求"做完了"的可验证判定标准，二值（兑现 / 未兑现）。

```bash
# 0) requirement.md 里给验收条件编号
#    - [ ] AC-1 手机号为空时返回 400，不再抛 NPE
#    - [ ] AC-2 非空时行为不变（回归）

# 1) 每改一处，都挂上它兑现的哪条
python aiwf.py record fix-login-npe --repo order-service \
        --file src/LoginService.java --criterion AC-1 --why "根因在入参未校验"

# 2) 随时看余额：哪条还没人认领
python aiwf.py acs fix-login-npe

# 3) 生成对账提示词，粘给一个新 session 的 agent
python aiwf.py ac-check fix-login-npe > ac-prompt.md

# 4) 把它的 JSON 结论记回来
python aiwf.py ac-record fix-login-npe --verdict warn --issue "AC-2 只有测试没有代码改动"

# 忘了挂？事后补
python aiwf.py link fix-login-npe --target 2026-11-01T09:45:43 --criterion AC-1
```

**为什么这不需要"事前预测改哪些文件"**：你不需要知道要改哪几个文件，只需要在**改的时候**说一句"这一处在做 AC-1"。漏掉的 AC 会在 gate 里被直接点名。**范围调查仍交给 agent，人只审 plan 里的「为什么」。**

> 挂 AC 这个动作本身就是自我检查：**说不清挂哪条，就说明这处改动可能不该做。**

### 真实场景的处理

**① 前后端分工，部分 AC 不归你**

```bash
python aiwf.py defer SHIP-1024 AC-1 AC-2 --owner frontend --reason "前端仓库另有人负责"
```

转交的 AC 不再要求本 story 认领，gate 不再拦；`acs` 会标 `→`，`ac-check` 提示词里也会注明「不在本 story 范围」。拿回来用 `undefer`。**这是有记录地转交，不是无痕忽略。**

**② 需求多轮讨论，AC 结论变了**

认领时会把 AC 的**文本哈希**一起记下。AC 文本一改，旧认领自动判为过期：

```
x AC 文本改过了，这些旧认领已失效，需要重新 record：AC-9
```

按新结论重新 `record --criterion AC-9` 即可恢复。**避免用旧方案当证据。**

**③ 对账什么时候做**

工具不能替你跑外部 agent，所以设了提醒：**自上次对账起每 3 条改动**提示一次：

```bash
python aiwf.py ac-check SHIP-1024 > ac-prompt.md
```

**④ 需求追加 / 变更：不用重开一个 story**

小需求追加、AC 变更时，**不新建 story，也不重走全部流程**——用 `reopen`：

```bash
python aiwf.py reopen HKEI-52251 --reason "追加：统一字段长度 + prospect 类别"
```

它会：

1. 把 stage 退回 `clarifying`（可用 `--stage planned|implementing` 指定）
2. **把 `verify.json` / `review.json` / `ac-check.jsonl` 移到 `archive/`**（留档但失效，防止旧结论掩盖新代码）
3. 记一条 reopen 流水

之后的路线和第一次一样，但**历史全部保留**：requirement / decisions / plan / changes / 旧 AC 认领都在。只需要：

```bash
# 1) 在 requirement.md 追加新 AC  →  - [ ] AC-11 字段长度统一为 32
python aiwf.py record HKEI-52251 --file DDL.sql --kind config --criterion AC-11 --why "ALTER 列长"
# 2) 重新对账 → 验证 → 评审（旧的已失效，gate 会拦）
python aiwf.py ac-check HKEI-52251 > ac-prompt.md
python aiwf.py verify HKEI-52251 --cmd "mvn test" --result pass --commit <sha>
python aiwf.py review HKEI-52251 --independent --verdict pass --commit <sha>
```

**判断标准**：不是看改动大小，而是看「原来的 AC 还要不要重新保证」。

| 情况 | 做法 |
|---|---|
| 同一需求的补充（加枚举值、补字段） | **同 story + `reopen`** |
| 影响面独立（DDL 跨端、要 DBA 评审、要单独排期） | **新建 story** |
| 纯 bug 修复（原 AC 没兑现） | 不 reopen，直接 `record` 修 + 重跑 verify |

---

## 命令

```bash
python aiwf.py init "fix-login-npe" --title "修复登录空指针" --key SHIP-1024 --repo order-service
python aiwf.py status [--all]              # 进行中的 story（含"缺几条 AC 没认领"）
python aiwf.py acs fix-login-npe           # 验收条件与认领情况（含转交、过期认领）
python aiwf.py where                       # 数据目录在哪、有没有"分裂"
python aiwf.py selftest                     # 端到端自检（改门禁后必跑）
python aiwf.py scan <目录>                 # 扫描明文凭据（命中退出码 8）
python aiwf.py scan <目录> --fix --yes     # 拆成脱敏模板 + .local，并自动补 .gitignore
python aiwf.py adopt [--yes]               # 把仓库内 ./stories 搬到当前数据目录
python aiwf.py context fix-login-npe       # 打包上下文，可直接粘给任何 agent

python aiwf.py record fix-login-npe --stage implementing --repo order-service \
        --file src/LoginService.java --criterion AC-1 --why "根因在入参未校验"
python aiwf.py link fix-login-npe --target <ts> --criterion AC-2   # 事后补挂
python aiwf.py handoff fix-login-npe --branch fix/npe --pr 128 --commit abc1234 \
        --todo "补一条空值回归用例"          # 交接指针：给指针，不复制历史
python aiwf.py ac-check fix-login-npe > ac-prompt.md   # 生成对账提示词
python aiwf.py ac-record fix-login-npe --verdict pass --fulfilled AC-1 --fulfilled AC-2
python aiwf.py defect open fix-login-npe "日志级别写错" --severity minor --source review \\
        --ac AC-2 --repro "导出日志应为 warn，实际 info"
python aiwf.py defect fix fix-login-npe D-1 --commit abc1234 --note "根因：常量写错"
python aiwf.py defect verify fix-login-npe D-1 --result pass --by tester-a
python aiwf.py defect list fix-login-npe
python aiwf.py defer fix-login-npe AC-3 --owner frontend --reason "前端仓库负责"  # 转交 AC
python aiwf.py undefer fix-login-npe AC-3   # 把转交的 AC 拿回来
python aiwf.py reopen fix-login-npe --reason "追加需求"   # 退回 clarifying，旧结论失效
python aiwf.py verify fix-login-npe --cmd "mvn -q test" --result pass --commit abc1234
python aiwf.py review fix-login-npe --independent --reviewer reviewer-a \
        --verdict pass --issue "异常处理建议统一包装" \
        --commit abc1234 --range 200c8d49..abc1234   # 记住评的是哪份代码
python aiwf.py gate fix-login-npe          # 检查门禁（失败退出码 2）
```

### 五个刻意的设计

1. **`review` 不带 `--independent` 会被拒绝**（退出码 3）——强制换 session 评。
2. **`review --verdict fail` 自动打回 `implementing`**，修完必须再评。
3. **`gate` 退出码非零**，可直接塞进 CI 或 pre-push hook。
4. **`verify` / `review` 绑定被评审的 commit**——评审或验证之后代码又变了，gate 会拦，**旧结论不能掩盖新代码**。
5. **`reopen` 归档而不是删除**——需求追加时历史可审计，但旧结论自动不能再当证据。
6. **验证和评审都看「最新一次」和「时间顺序」**——`verify` 失败不算通过，评审之后又动过代码就算过期。**结论有时效，不是一次性通行证。**

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 通过 |
| 1 | 找不到 story |
| 2 | gate 失败 |
| 3 | review 未声明独立 |
| 4 | 挂了不存在的 AC |
| 5 | link 的 target ts 不存在 |
| 6 | tracker 号已有 story / 目录已存在 |
| 7 | 这条 AC 已转交出去，不能在本 story 认领（需先 `undefer`） |
| 8 | `scan` 发现疑似明文凭据 |

---

## 多 repo 编排

跨 repo 有并发冲突，`implementing` 阶段**一次只处理一个 repo**：

```json
"repos": [
  {"name": "order-service", "path": "G:/...", "status": "pending"},
  {"name": "user-service",  "path": "G:/...", "status": "pending"}
]
```

逐个推进，每次 `record` 带上 repo 名——把"开多个 IDE 逐个做"的手工顺序固化下来。

---

## 数据隔离（三层）

| 层 | 机制 | 作用 |
|---|---|---|
| 物理 | `AIWF_DATA` 指向仓库外 | 工作数据根本不在 git 工作区里 |
| 规则 | `.gitignore` 里的 `stories/` | 即使忘了设环境变量也不会被 add |
| 操作 | 只读部署可把 `--push` 地址设为无效值 | 手滑 push 也会失败 |

**建议**：工作数据目录一律放仓库外。仓库里只留代码和 md。

### 向后兼容（升级不用迁移数据）

新版本对旧数据**全部兼容**，不需要转换、不需要重建 story：

| 旧数据缺什么 | 新版本怎么处理 |
|---|---|
| `meta` 没有 `key` | 回退到 `jira` 显示；两者都没有就显示 `-` |
| `meta` 没有 `deferred` | 视为「没有转交」 |
| `changes.jsonl` 没有 `acHashes` | 视为「无法校验版本」，认领仍然有效（不会被误判过期） |
| JSON 文件带 BOM | 自动兼容（UTF-8 BOM 会被吃掉） |
| `meta` 的 `id` 是短号（如 `2026-11-01-001`） | 只用于展示，不影响定位 |

**唯一的变化**：只有升级后新 `record` 的改动才带版本哈希，才会参与「过期认领」检查。老记录要享受这层保护，重新 `record` 一次即可（旧的自动失效）。

### 数据目录在哪，以及"分裂"排查

`AIWF_DATA` 是**环境变量**，只在设了它的终端里生效 —— 所以很容易出现"一半 story 在仓库内、一半在外部"：

```bash
python aiwf.py where     # 打印代码目录、当前数据目录、仓库内 ./stories 各自的 story
python aiwf.py status    # 末尾会显示当前数据目录；发现分裂会提示
python aiwf.py adopt     # 把仓库内 ./stories 搬到当前数据目录（先预览，加 --yes 执行）
```

**注意 `setx` 的坑**：`setx AIWF_DATA "<路径>"` 只对**之后新开的终端**生效，当前窗口要用 `$env:AIWF_DATA = "<路径>"`。

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| v0 | init / status / context / record / gate | ✅ |
| v1 | 独立 review、PR 指针交接、验收条件硬门禁 | ✅ |
| v1.1 | 独立复核（初版，后更名 ac-check） | ✅ 已被 v1.2 取代 |
| v1.2 | AC 编号化 + 改动挂 AC + 兑现检查（ac-check）+ link 补挂 | ✅ |
| v1.2.1 | tracker 号命名、数据目录自检、AC 转交（defer）、**AC 版本绑定**、verify/review 绑 commit、reopen、BOM 兼容 | ✅ |
| 真实项目验证 | 一个真实 Jira 单跑完 clarifying → reviewed（含前后端分工与需求追加） | ✅ |
| v2 | `verify` 自动跑命令；`status` 显示 token/成本 | 待做 |
| v3 | watchdog 自动化（headless agent 直接跑 ac-check） | 待做 |

---

## 开发注意事项

### 改门禁或状态机，先跑自检

`bash
python aiwf.py selftest
`

21 个断言，在临时目录里跑完整流程，逐阶段验证拦截行为：未挂 AC、AC 未认领、
AC 改写后旧认领失效、verify 失败、commit 变更后验证过期、有 open 缺陷、
缺陷没复测、复测不写 --by、评审后改动、非法阶段、阻塞、reopen。

**为什么会有这条规矩**：连续三次回归都是自伤——

| 事故 | 怎么发生的 |
|---|---|
| implementing 阶段门禁被吞 | 用字符串锚点插入 `if rank >= 4:` 块时，把 `if rank >= 3:` 那行替换掉了，四条检查被并进了 verifying |
| `wontfix` 崩溃 | 新命令引用 `args.note`，但那个子命令没定义 `--note` |
| `blocked` 静默无效 | `record --stage blocked` 打印"成功"，但不在 STAGES 里所以阶段根本没变 |

**共同原因**：用脚本批量替换代码、靠字符串锚点改结构。**改完必须跑自检，不能等下一个功能做完才发现。**

### 另外三条

- **改完看 diff 的缩进**：字符串替换最容易吃掉 `if`/`for` 这一行，导致整块被并进上一个分支。
- **测试用临时目录**：`selftest` 自己在工作区建 `.selftest-<随机>`，跑完删除；不要拿真实 story 试。
- **文档跟代码一起改**：状态机图、门禁表、退出码，这三处最容易漂移（`blocked` 就是文档写了、代码没有）。

---

## 边界（现在不做什么）

- ❌ 不封装 LLM 调用（用你现有的 agent）
- ❌ 不自动提交 / 不自动合并（必须人工 gate）
- ❌ 不做代码语义索引（先用 grep / 文件名）
- ❌ 不做多 agent 编排（先把单 story 单 repo 做扎实）
- ❌ 不做 Trigger / Thread / Subscription（那是"主动型 agent"的基础设施）

---

## 设计来源

- **DeepSeek Super-agent**（[dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)）：
  借鉴了「编排必须是工具不是提示词」「Ticket 状态不跟随 Session」「独立 Watchdog」三点，
  但**没有抄它的调度复杂度**——它解决 30 个 agent 并发，本项目解决 1 个 agent 可追溯。
- **BDD / 敏捷**：AC 与 DoD 的区分（本项目 `ac-check` 查 AC，`review` 查的更像 DoD）。

---

## 一句话

**你只在三个点说话：定 AC、审方案的理由、决定合并。中间由 agent 自己记录，gate 自动查，独立 agent 对账。**

