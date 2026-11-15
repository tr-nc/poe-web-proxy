# Handoff：请求失败不能使 Pi session 永久失去继续能力

> **未实现的修复交接，2026-11-15。** 调查基线：`30d2c6e`。本文不是当前功能说明。
> 本次交接只新增此文档；没有修改运行代码、数据库、Pi 历史或触发新的 Poe 生成。
> 完成修复后，将仍需保留的约束归入测试或必要操作说明，删除本文，避免维护平行实现清单。

## 1. 用户目标与授权边界

用户在 `/home/terence/code/conquer-sea-battle` 的 Pi session 遇到以下连续错误：

1. `409 upstream_incomplete_reply`：Poe 已结束，但没有可交付的答案正文。
2. 输入 `continue` 后，立即得到 `409 session_conflict`：上一请求未确认，要求完全相同的请求才能恢复。

用户要求的不是一次性清锁，而是 **fail-safe 的会话生命周期**：

- 允许一次请求失败；错误不应自动变成整个会话永久不可用。
- 已确认终止的失败之后，用户直接继续提问，应产生一个新的轮次，只追加新输入。
- 不能通过重复发送旧任务、旧工具结果，或把无效输出伪装成成功来实现“可继续”。
- 超时、断线、进程崩溃、恢复中断时仍要保留恢复证据；恢复操作自身应可恢复。
- 原上游聊天确实不可恢复时，应有保留本地 Pi session 的明确出路，而不是要求丢弃整个会话。

用户已允许必要的新生成，但随后明确要求：**本轮只写 handoff、commit、push，由其他人接手实施。**
接手者应先落实机制，再恢复实际会话；不要把上述授权扩张为自动重放历史或执行旧工具调用。

## 2. 已确认事实与仍未知的部分

### 实际会话证据

本机 Pi session 目录：

```text
/home/terence/.pi/agent/sessions/--home-terence-code-conquer-sea-battle--/
```

找到一个新 session，其末尾事件与用户报告完全一致：

- `2026-11-15T15:25:40.200Z`：空回复错误。
- `2026-11-15T15:27:38.053Z`：用户消息 `continue`。
- `2026-11-15T15:27:38.074Z`：会话冲突错误。

只读核对过对应的 Poe 聊天与 SQLite binding：

- 最新 user ID、user 内容哈希、reply ID 与 binding 一致。
- 页面有 request marker，回复在该 user 消息之后。
- `complete=true`、`generating=false`、`hasCompletionControls=true`。
- `text=""`、`agentText=null`，正文只有一个空的 Thinking 容器。
- 去除 Thinking 子树后检查 DOM，也没有被选择器漏掉的答案正文。
- 本地 binding 为 `status=uncertain`、`submitted=true`，同时保留原 user/reply 身份。
- 最后一次工具调用的结果已在 Pi 历史中；不能为了恢复而重新执行它。

**结论：这次确实是上游终止且没有答案，不是已证实的 DOM 漏读问题。**
为什么 Poe 生成空回复仍未知；修复会话生命周期不能承诺消除 Poe 自身的空回复。
未确认所有类似历史故障都有相同上游原因。

不在 Git 中复制真实聊天 URL、完整 transcript、认证状态或数据库备份。下节可在本机重新定位实际 binding。

### 实际会话定位（只读，不生成）

不要无条件选“最新文件”：用户可能又创建了新 session。匹配上述时间和错误，再人工确认。

```bash
uv run python - <<'PY'
import json
import sqlite3
from pathlib import Path
from poe_web_proxy.sessions import digest

root = Path.home() / '.pi/agent/sessions/--home-terence-code-conquer-sea-battle--'
path = Path.home() / '.local/share/poe-web-proxy/chats.sqlite3'
with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
    for file in sorted(root.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True):
        entries = [json.loads(line) for line in file.read_text().splitlines() if line.strip()]
        if not any('upstream_incomplete_reply' in entry.get('message', {}).get('errorMessage', '')
                   for entry in entries):
            continue
        identity = entries[0].get('id')
        row = db.execute('SELECT record FROM chats WHERE key = ?', (digest(identity),)).fetchone()
        record = json.loads(row[0]) if row else {}
        print(file)
        print(json.dumps({'session_id': identity, **{
            name: record.get(name) for name in (
                'status', 'model', 'chat_url', 'reply_id', 'response_id', 'user_id', 'submitted'
            )
        }}, ensure_ascii=False))
PY
```

## 3. 可运行的最小复现

在仓库根目录执行。只使用临时 SQLite 和模拟页面，走真实 Browser / Session / HTTP 调用链，不访问 Poe、不花费点数。

```bash
uv run python - <<'PY'
import asyncio
import runpy
import tempfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from poe_web_proxy.app import create_app
from poe_web_proxy.sessions import SessionStore

fixture = runpy.run_path('tests/test_active_recovery.py')
sleep = asyncio.sleep

async def tick(_):
    await sleep(0)

with tempfile.TemporaryDirectory() as directory:
    browser = fixture['LivePage'](empty=True)
    browser.timeout = 1
    store = SessionStore(Path(directory) / 'chats.sqlite3')
    with patch('poe_web_proxy.browser.EMPTY_REPLY_GRACE', 0), \
         patch('poe_web_proxy.browser.asyncio.sleep', tick):
        with TestClient(create_app(browser, 'local-key', (fixture['MODEL'],), store)) as client:
            first = client.post('/v1/agent/chat/completions',
                                headers=fixture['AUTH'], json=fixture['BODY'])
            following = {
                **fixture['BODY'],
                'messages': [*fixture['BODY']['messages'], {'role': 'user', 'content': 'continue'}],
            }
            second = client.post('/v1/agent/chat/completions',
                                 headers=fixture['AUTH'], json=following)
            print(first.status_code, first.json()['error']['code'])
            print(second.status_code, second.json()['error']['code'])
            print('upstream sends:', browser.sends)
            assert first.status_code == second.status_code == 409
            assert first.json()['error']['code'] == 'upstream_incomplete_reply'
            assert second.json()['error']['code'] == 'session_conflict'
            assert browser.sends == 1
PY
```

调查时已运行，结果：

```text
409 upstream_incomplete_reply
409 session_conflict
upstream sends: 1
```

这是**旧行为复现**，不是修复后的验收测试。实现前先加入要求“失败后追加新输入可成功”的回归测试并观察失败。
`LivePage` 目前只模拟一个固定 user/reply，需要扩展为能记录多轮发送的页面，才能验证新轮次成功与未重放输入。

## 4. 已确认的根因与代码入口

问题是状态模型混淆，不是“需要把 409 改成 200”。

- `src/poe_web_proxy/sessions.py`：`AgentTurn.__init__` 将非 ready 的旧记录限制为完全相同请求的 read-only recovery；新增 `continue` 改变 request digest，在访问浏览器之前就被拒绝。
- 同文件 `abort()`：只要已提交但没有 `commit()`，通常就写为 `uncertain`，没有区分“上游已终止但输出无效”和“发送/生成状态真的未知”。
- 同文件 `commit()`：将输入接收与有效 assistant 输出一起推进。于是“输入已存在于 Poe，但输出不能交付”没有可继续的表示。
- `src/poe_web_proxy/browser.py`：`generate()` 在空终止回复时设置 `generation_finished=True` 并抛出 `IncompleteReply`；非空 settled 回复也设置完成标志。当前 journal 没有持久化这一完成事实；`checkpoint()` 只保存部分身份/发送信息。
- `src/poe_web_proxy/app.py`：`respond()` 的 cleanup 会释放已终止回复的浏览器，但这不等于解开 session 状态；`agent_complete()` 在得到正文后才做 `body.reply()` 校验和 `plan.commit()`。JSON/工具校验失败同样会落入未确认记录。
- `src/poe_web_proxy/agent.py`：`reply()` 拒绝损坏 JSON、未声明工具和不符合 schema 的参数。此安全约束必须保留。
- `src/poe_web_proxy/turn.py`：`BrowserTurn` 是浏览器与会话事务共享的单轮证据。

历史：`30d2c6e` 引入空终止回复的明确错误；不完整回复导致会话受阻的设计在它之前就存在。
相关前序改动有 `6daee34`（同请求只读恢复）、`459bdb0`（operator-only checkpoint）。
**尚未通过历史行为测试定位“永久阻塞”的最早引入提交，不要把 `30d2c6e` 声称为完整问题的引入点。**

## 5. 修复方向（建议，尚未实施）

### 分开记录事实，而不是放宽全局保护

由 session transaction 模块权威拥有三个事实：

1. 哪些客户端输入已经被上游确认接收，后续只能发送其后的 delta。
2. 当前生成是运行中、已终止成功、已终止失败，还是结果未知。
3. 哪个经过校验的 assistant 回复已经提交给客户端交付路径。

具体字段/状态名由实施者确定，不要求复制此文的命名。最小 coherent change 即可，不必引入队列系统或新存储框架。
SQLite 中继续避免持久化明文 transcript 和图片；需要更多证据时先说明隐私和保留策略，不默认保存 raw reply。

### 用户可观察行为

| 情况 | 期望行为 |
| --- | --- |
| 确定发送前失败 | 回退本次 reservation；可再次提交，不留下 session 死锁。 |
| 已发送、仍在生成或状态未知 | 不重发；观察原请求，不能用导航打断活跃生成。 |
| 已确认终止、空回复 | 返回本轮错误；新增用户输入允许开启新轮次，只发送 delta。 |
| 已确认终止、JSON 或工具参数校验失败 | 不交付无效工具调用；本轮失败但后续用户输入仍可继续。 |
| 完全相同的失败请求被 SDK 重试 | 不当作新生成；返回/重读原结果，终止失败应明确不可自动重试。 |
| 完全相同的成功请求重试交付 | 不重新生成；工具调用 ID 稳定，不能因为代理重试制造新 ID。 |
| 重启后遇到旧 uncertain binding | 缺少新字段不能猜测“已完成”；能核实原请求与终止状态时安全迁移，不能核实时保持可解释的恢复路径。 |
| 上游聊天不可恢复 | 不丢弃 Pi 历史；提供显式的重建/切换路径及风险说明，不静默换聊天或重放完整历史。 |

“继续”是用户明确追加的新轮次，不等于授权自动重试旧轮次。第一条失败请求也不能被静默包装成成功回复。
可以在新轮次附带简短的传输层说明，明确上一条回复未交付，不把它当作已经执行的客户端工具。

### 必须在设计/实现中解决的细节

- **完整调用链持锁**：观察旧请求、确认终止、推进输入游标、reserve 新轮次应由同一浏览器所有权保护。不能在验证与新发送之间让另一请求抢占。
- **保留原 checkpoint**：只读恢复失败、登录失效、超时或取消时，不允许用尚未发送的新请求 history/request marker 覆盖原请求证据。
- **持久化时序**：完成证据须在有副作用的后续动作之前可靠落盘；覆盖生成完成后、输出校验前、commit 前后、响应送达前后等崩溃窗口。
- **崩溃中的新轮次**：一旦有可能执行 Enter，恢复必须识别这个新轮次，不能再次发送 delta；如果确认未发送，应仍保留上一轮失败 checkpoint 并允许继续。
- **旧 uncertain 的新输入**：如何先只读观察旧发送，再允许新输入，不能仅放宽 `status != ready` 条件。`held_request` 当前按 request key 限制访问，新输入产生不同 key；必须协调应用级浏览器 ownership，而非绕过。
- **有效但未交付的旧回复**：与“已校验拒绝的回复”分开定义。新用户消息到来时是先交付、显式放弃还是要求确认，需要明确；不要无声丢弃可能已交付/执行的工具调用。尤其覆盖 commit 已成功但客户端没有收到响应的情况。
- **输入前缀与角色**：失败后只接受经确认历史的延续；不能让伪造 assistant/tool 消息、模型切换、schema 变化或历史分叉变成隐式解锁。
- **双重交付限制**：代理可以保留稳定 call ID 和已提交记录，但未与客户端执行器协作就不能保证任意工具外部副作用的 exactly-once。
- **不确定状态的出路**：网页 Enter 与 SQLite 无法原子提交。不可判定时可以需要等待/确认，但不能承诺“任何故障都自动恢复且零重复计费”。原聊天损坏后的显式 fallback 需要设计，不要假装正常重试能解决。

## 6. 验证计划与完成标准

通过真实 HTTP 接口 + Browser/session 链断言行为，不能只测试新状态枚举。

### 必须新增/调整的回归覆盖

- [ ] 首次空回复仍报错；用户追加 `continue` 后成功，绑定同一聊天，发送的只有新 delta。
- [ ] 损坏 JSON、未知工具、schema 参数无效、`tool_choice` 不满足：均不交付工具；之后新用户消息可继续。
- [ ] 成功交付工具调用后发生下一轮失败：已执行工具及其结果不重放。
- [ ] SDK 对终止失败重试、连续多个失败轮次、失败后上下文/effort 更新，不产生隐式旧任务重发。
- [ ] 活跃生成超时后原请求恢复：同一 document，不 reload；其他 session 不得打断。
- [ ] 新用户输入到来时旧生成仍活跃：不导航、不发送第二次；超时或观察失败不覆盖旧证据。
- [ ] 生成结束到校验/提交/交付前后的中断和重启；明确验证新输入与原请求各自恢复语义。
- [ ] continuation 准备阶段失败：保留旧 checkpoint；Enter 之后中断：只观察新发送，不二次发送。
- [ ] 两个请求竞争同一 binding、CAS 失败、取消/断线，失败 owner 不回滚成功 owner。
- [ ] 原请求标记、user hash、reply ID 不匹配或外部修改时，不误认已结束、不发送到分叉历史。
- [ ] 旧 schema 的 uncertain 记录能按证据恢复；缺证据的记录有明确出路，不清锁猜测。
- [ ] 输出交付重试 call ID 稳定，损坏输出仍不能降级成可执行调用。
- [ ] 保留哈希存储与权限约束，不引入明文 transcript/图片泄漏。
- [ ] 原聊天不可访问的显式恢复路径保留本地历史，并对上下文丢失、重建发送和计费作真实说明。

现有相关测试：

- `tests/test_active_recovery.py`：空终止回复、活跃页面恢复。当前明确断言空回复记录为 uncertain，需随新契约调整，不能只删断言。
- `tests/test_agent_transport.py`：协议校验、损坏输出、绑定 session；当前损坏输出也断言 uncertain。
- `tests/test_recovery.py`：只读恢复、重启、不同请求禁止重放、CAS、operator checkpoint、稳定 tool-call ID。
- `tests/test_session_failures.py`：reservation 所有权、外部修改检测。
- `tests/test_disconnect.py`、`tests/test_generation_state.py`、`tests/test_sessions_images.py`：取消、settled 观察、历史 delta 和图片去重。

已执行的基线命令（修复前）：

```bash
uv run pytest -q tests/test_active_recovery.py tests/test_generation_state.py tests/test_recovery.py
# 23 passed；另有 Starlette/httpx 弃用警告。
```

这些通过的测试大多编码了当前行为，**不证明已经解决本问题**。交接期间未执行真实新生成、未运行付费 live smoke。
实施者还应执行全套测试和项目工具检查：

```bash
uv run pytest -q
uv run ruff check .
uv run pyright
```

## 7. 实际 session 的恢复与交付

1. 先完成可重复的失败/继续回归测试与修复，不先手工清理真实 binding。
2. 操作前重读实际 session 和 binding，确认用户没有继续操作；备份放在 Git 之外的私有目录，不输出密钥。
3. 浏览器使用专用诊断 session；认证只通过私有临时文件/既有认证状态传递，检查完成后清理。不得关闭服务正在持有的活跃浏览器页面。
4. 用修好的正常继续路径恢复原 Pi session，保留用户待处理输入。不必为了旧问题注入一个虚构 assistant 回复或偷偷删掉错误历史。
5. 若必须修改 Pi session 格式/使用 SDK，先读已安装 Pi 的 SDK、session 格式及相关文档和示例；遵守 session 的 parent/branch 关系。本文没有实施 Pi 集成。
6. 新生成可能返回工具调用；恢复验证本身不代表授权执行它。优先用 tools-forbidden 的受控验证，确保任何额外 checkpoint 与 Pi 历史一致，不能只改数据库宣称成功。
7. 报告真实原场景的结果、未覆盖/阻塞项、是否触发生成或工具执行；无法做现场验证时明确说明。

**最终验收：失败的是轮次，不是整个 Pi session；已证实的终止失败后可以自然继续，真正未知的状态只暂停必要动作且保留恢复路径。**
