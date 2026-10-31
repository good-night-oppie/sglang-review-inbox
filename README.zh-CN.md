<div align="center">

<img src="web/assets/sgl-wordmark.png" alt="SGLang" height="56">

# SGLang Review Inbox

**让 coding agent 把一个 PR 从「能帮忙看下吗？」一路推进到「已合并」，只把真正需要维护者的 PR 留给你。**

[![English](https://img.shields.io/badge/README-English-D55816?style=for-the-badge)](README.md)
[![简体中文](https://img.shields.io/badge/README-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-A5300F?style=for-the-badge)](README.zh-CN.md)

</div>

Review Inbox 是一个跑在你自己电脑上的小网站，给被 review 请求淹没的 SGLang 维护者用。它负责两件事：

- **收请求**：把别人请你 review 的 PR 收进来，来源包括 Slack 里 @你的消息、私信、团队的 PR 频道，以及 GitHub 上 @你或指派给你的 PR。
- **跑流程**：交给 coding agent 跑完整个流程，包括 review、独立复核、CI、和作者来回讨论、小问题直接修复、approve、合并，最后在原来的 Slack 线程里回一句结果。

每个 PR 走到哪一步，都能在浏览器里的中英双语看板上看到。需要你亲自处理的只剩高风险的 PR。

它**以你的身份**在 GitHub 和 Slack 上操作，用的是你自己的 `gh` 登录和 Slack 用户 token。默认从只读的「影子模式」开始；每一种写操作都有单独的开关，同一个写操作最多执行一次。

## 能做什么

- **收集请求**：
  - Slack 里 @你的消息、私信（包括私信线程里的回复）；
  - 在 @过你的线程里后来才提出的请求；
  - 团队频道（比如 `#pull-requests`）里没有点名别人的 PR；
  - GitHub 上 @你或指派给你的 PR。

  「谢谢 / ok」这类短确认和抄送通知不算请求。
- **Review**：
  - codex agent 用最新版的 [sglang-humanize-review](https://github.com/BBuf/AI-Infra-Auto-Driven-SKILLS/tree/main/skills/sglang-humanize-review) skill 做 review，再由另一个 agent 逐条反驳阻塞问题，只留下站得住的。
  - **只有真正的 bug 才会挡住 PR**。「补测试、补 benchmark、在真机上验证」这类要证据的意见，只作为不阻塞的备注留在看板上，而且同一项证据不会重复要。
  - PR 按风险分级：T1/T2 没问题就自动 approve；T3（热点路径、大改动、高风险）等你来看。
- **CI**：
  - 发 `/tag-and-rerun-ci` 和定向的 `/rerun-test`，失败了按退避重试；批准 fork PR 被挂起的 CI run；
  - 读日志判断失败原因，改动没碰到的硬件档位不管；
  - 真是作者的问题，就带着关键报错行 @作者。
- **持续对话**：
  - 一直跟到合并为止，每条回复都会读。
  - 作者对意见有异议时，agent 会对照代码和提交记录核实，然后认可、说明还缺什么，或者（很少）交给你拍板。
  - **你自己**在 PR 上的回复（「ignore this」「LGTM」「麻烦再改一下 X」）会被当成决定直接执行，你不用再回看板。
- **代修（可选）**：不等作者，直接推修复，范围包括合并冲突、已确认的问题、改动导致的 CI 失败。只推允许维护者修改的分支，只做快进推送，从不 force-push。
- **合并（可选）**：达到合并标准就 squash 合并，并在 Slack 线程里回一句收尾。
- **看板**：`http://127.0.0.1:8765`。
  - 分栏：需要你、跑 CI、等作者、Slack 待回复、已合并。
  - 一键切换中英文，agent 写的英文内容会在后台翻译成中文。

## 一个 PR 怎么流转

```
请求（Slack / GitHub）─▶ 分诊 ─▶ agent review ─▶ 复核
                                                  │
        ┌────────────── 有真 bug ◀────────────────┤
        │  agent 直接修，或把问题发给作者，         │ 没问题
        │  一直沟通到解决                          ▼
        └─────────────────────────▶ approve（T1/T2）/ 需要你（T3）
                                                  │
                  CI：运行、重试、归因、@作者 ◀─────┘
                                                  │
                        合并 ─▶ Slack 线程里回「Merged …, thanks!」
```

## 安全设计

- **先影子模式**：影子模式下 agent 照常 review 和跟踪，但在你点按钮之前不往任何地方写东西。点顶栏的模式按钮切到「自动」；切回来也只要点一下，立刻生效。
- **最多一次**：每个对外写操作都在台账里按「PR + head 提交 + 动作」登记，包括评论、approve、CI 命令、Slack 回复、推送、合并。重启或重试都不会重复执行。
- **大动作有护栏**：
  - review、approve、合并、代修都有每日上限；
  - 有 WIP / do-not-merge 标记的不合并；
  - 从不用 `--admin`；
  - 推送只做快进，作者更新过的提交不会被覆盖。
- **只在本机**：服务只监听 `127.0.0.1`；所有写请求都要带页面里的防 CSRF token；Slack token 存在数据目录下权限为 `0600` 的文件里，存好后不再显示。
- **agent 在沙箱里**：
  - review 跑的是只读、无网络的 `codex exec`；
  - 代修在一次性的副本上用可写沙箱，git 操作由服务自己执行，不交给 agent；
  - 所有 prompt 都把 PR 文本、评论、日志当作不可信数据。

## 环境要求

- macOS 或 Linux，**Python 3.11+**，只用标准库，不需要 `pip install`。
- [`gh`](https://cli.github.com/) 已用维护者账号登录（`gh auth login`），对要 review 的仓库有写权限：approve、评论、打标签、合并。
- `git`，以及登录了一个或多个账号的 [`codex`](https://github.com/openai/codex)，见下文。
- 可选：[`pre-commit`](https://pre-commit.com/)，代修时用仓库自己的 hook 格式化代码。
- 一个能安装 App 的 Slack 工作区，只需要**用户** token。

## 部署并在浏览器里打开

**1. 拿到代码，生成你的配置**

```bash
git clone https://github.com/BBuf/sglang-review-inbox.git
cd sglang-review-inbox
cp config.example.json config.json
```

**2. 填写 `config.json` 里的占位符。** 只要还有 `<...>` 占位符没替换，服务就拒绝启动。

| 占位符 | 填什么 |
| --- | --- |
| `<YOUR_GITHUB_LOGIN>` | 你的 GitHub 用户名，也就是 `gh` 登录的账号。留空字符串也可以，会自动用 `gh api user` 读取。 |
| `<PATH_TO_CODEX_HOME_1>`、`<PATH_TO_CODEX_HOME_2>` | 你的 codex 账号目录，见下一步。只有一个账号就只留一项。 |
| `<CODEX_MODEL>`、`<CODEX_MODEL_STRONGEST>` | 你的 codex 账号能用的模型名，按任务分别配置：`T1`/`T2`/`T3` review、`verify`、`ci`、`fix`、`discuss`、`translate`。删掉某个 profile 就用 codex 的默认模型。 |

`repos` 是 agent 唯一允许操作的仓库列表。`sources.slack.watch_channels` 是「没 @ 你也收 PR」的频道，填 `[]` 表示关闭。

**3. codex 账号（示例配置里是占位符）。** `agent.codex_homes` 里每一项是一个 `CODEX_HOME` 目录，对应一个 codex 登录，按顺序使用。某个账号用量到顶或撞上 spend cap 时，任务会切到下一个账号，原账号休息 30 分钟。每个账号登录一次：

```bash
CODEX_HOME=~/.codex-personal codex login   # 首选
CODEX_HOME=~/.codex codex login            # 备用（CLI 默认目录）
```

然后把这两个路径填进 `codex_homes`。不要提交 `config.json`，它已经在 `.gitignore` 里。服务本身不读取 codex 的凭证：它只给 `codex` 进程设置 `CODEX_HOME`，并从 `auth.json` 里读账号邮箱，用来显示每个任务是哪个账号跑的。

**4. 检查并启动**

```bash
python3 -m unittest discover -s tests        # 80 多个测试，不需要联网
python3 server.py --config config.json
```

浏览器打开 **http://127.0.0.1:8765**，点顶栏的 **EN / 中文** 切换语言。

**5. 连接 Slack**

1. 打开 <https://api.slack.com/apps?new_app=1>，选择 **From a manifest**，选好工作区，粘贴 [`slack-app-manifest.json`](slack-app-manifest.json) 的内容。它只申请用户权限：搜索、读取你所在频道和私信的历史、查用户名，以及用来回复线程的 `chat:write`。
2. 把 App 安装到工作区，在 *OAuth & Permissions* 页面复制 **User OAuth Token**（`xoxp-…`）。
3. 在看板上点 **连接 Slack** 粘贴进去。服务会先用 `auth.test` 校验，再保存到 `<data_dir>/slack-tokens`（权限 `0600`）。有几个工作区就重复几次。

**6. 切到自动模式。** 先在影子模式下看几个 PR，确认 review 靠谱后，点模式按钮切到自动。准备好了再打开 `policy` 里风险较高的开关：`auto_merge`、`auto_fix`。

### 让它常驻

```bash
nohup python3 server.py --config config.json > ~/.cache/review-inbox/server.log 2>&1 &
```

macOS 可以用 launchd 开机自启，Linux 可以用 systemd 用户服务。配置示例见[英文 README](README.md#keep-it-running)。

**安全重启。** 重启会打断正在跑的 agent 任务。这些任务之后会重跑，但代修不能停在「已推送、还没留言」的中间状态。所以重启前先等 `curl -s http://127.0.0.1:8765/api/state` 显示 `"inflight": {}`。

## 配置说明

所有配置项在 [`inbox/config.py`](inbox/config.py) 里都有默认值，`config.json` 只覆盖你写了的项。

| 配置项 | 含义 |
| --- | --- |
| `mode` | `shadow`（什么都不写）或 `live`（自动），也可以在看板上切换。 |
| `repos` | agent 允许操作的仓库，其他仓库一律忽略。 |
| `data_dir` | 状态目录：SQLite 数据库、Slack token、页面 token、clone 下来的代码、运行日志。默认 `~/.cache/review-inbox`。 |
| `sources.*` | 请求来源和拉取频率：`slack`、`slack_inbox`（私信）、`github_mentions`、`github_assigned`、`github_review_requested`（默认关闭，因为 CODEOWNERS 会让它多得没法看）。 |
| `agent.codex_homes` | codex 账号，按优先顺序排列，见上文。 |
| `agent.profiles` | 每类任务用的模型和推理强度。 |
| `agent.service_tier` | 原样传给 codex 的 `service_tier`，比如 `priority` 更快但更贵。 |
| `agent.max_concurrent`、`agent.daily_review_limit` | agent 并发数，以及 24 小时内最多 review 多少次。 |
| `agent.windows`、`agent.window_minutes` | agent（codex）任务只在这些本地时间开始，比如 `["10:00", "15:00", "21:00"]`，每个时段持续 `window_minutes` 分钟；`[]` 表示随时。拉取、CI 命令、合并、Slack 回复随时进行。你在网页上操作过的卡片（重新 review、添加、恢复）一小时内不受时段限制。 |
| `agent.daily_token_budget`、`agent.token_reserve` | 每个自然日的 codex token 硬上限（`0` 表示不限），按每次运行输出的 `tokens used` 累计；正在跑的任务先按 `token_reserve` 预占，跑完换成真实用量。 |
| `agent.review_skill` | review skill 所在的 git 仓库、分支和目录，每 10 分钟同步一次。没按 skill 做的 review 会被作废重跑。 |
| `policy.auto_*` | 每一类自动写操作一个开关：CI 命令、发出问题、给作者留言、回复讨论、approve、合并、代修。 |
| `policy.max_*_per_day`、`max_agent_fixes_per_pr` | 各类操作的上限。合并上限只算 agent 自己合的；正在跑的代修也计入代修上限。 |
| `policy.min_merge_interval_minutes`、`policy.min_fix_interval_minutes` | 节奏：两次自动合并至少间隔这么久（默认 60 分钟）；代修一次只跑一个，两次推送至少间隔这么久（默认 15 分钟）。排队等空档的 PR 仍由 agent 处理，不会转给作者。 |
| `policy.slack_*` | 是否以及怎样回复 Slack 线程；回复以 `slack_prefix` 开头。 |
| `policy.github_prefix` | agent 在 GitHub 上发的 review、评论、回复都以它开头（CI 斜杠命令除外，保持原样）。行内评论不带 `[P1]`/`[P2]` 标签：必须修的 bug 直接写，小问题以 `Minor:`、`nit:` 或 `Non-blocking:` 开头。 |
| `refresh_seconds` | 进行中的 PR、等作者的 PR 分别多久检查一次。 |
| `server.host`、`server.port` | 默认 `127.0.0.1:8765`，请只监听本机。 |

## 节省 codex 用量

token 大头在 review 上，省的办法：

- **时段加每日上限**：agent 的活攒到 `agent.windows` 的时段里批量跑，用到 `agent.daily_token_budget` 就停。网页顶部显示今日用量和下个时段。
- **增量复审**：已 review 的 PR 推了新代码，不再从头 review。PR 自身的 diff 没变（只合了 main）就直接沿用结论，不跑 agent；有变化时，agent 只看上次 review 之后的改动并复查之前的问题，不跑语料 sweep。网页上的「重新 review」会做全量 review。
- **CI 规则先行**：汇总状态的 job、在准备步骤就失败的 job、已知的 CI 基础设施报错，用规则归类，不调 agent；拿不到日志的失败只重跑，不交给 agent。剩下的才在时段内交给 agent 读。
- **中文按需翻译**：只有在中文界面打开某张卡片时才翻译它。
- **降低推理强度**：`agent.profiles` 里可以给小 PR（`T1`）和复核调低 `effort`。

## 用在 SGLang 以外的项目

整体流程是通用的，但有几处写死了 SGLang 的约定：
- CI 档位和斜杠命令（`inbox/ci.py`）；
- 按路径划分的风险区域（`inbox/triage.py`）；
- review 用的 prompt 和 skill。

要接其他项目，从这几处改起。

## 目录结构

```
server.py            HTTP 服务和 JSON API（只用标准库）
inbox/pipeline.py    每个 PR 的状态机：收集、review、CI、讨论、代修、合并、Slack 回复
inbox/agent.py       codex 调用：review、复核、CI 归因、讨论、代修、翻译；账号切换
inbox/ci.py          SGLang 的 CI 策略（档位、重试、合并标准）
inbox/github.py      gh 封装；  inbox/slack.py  Slack Web API；  inbox/db.py  SQLite 存储和台账
inbox/fixer.py       代修用的 git 操作（合并、提交、快进推送）
prompts/             agent 的 prompt；  web/index.html  看板（单文件）
tests/               单元测试（python3 -m unittest discover -s tests）
```

## 许可证

[Apache-2.0](LICENSE)
