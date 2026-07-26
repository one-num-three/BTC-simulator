# BTC-simulator 增强计划

> 基于 commit `fbec0b4` 的完整代码走查（3986 行 Python + 1935 行前端 / 36 个文件 / 34 个测试）。
> 所有结论都带 `文件:行号`，方便直接跳转确认。

---

## 0. 先说结论：现在的底子

这个项目该有的骨架都有了，而且有几处比一般教学项目做得好：

- **签名和哈希是真的**：ECDSA secp256k1 + 确定性签名（`wallet.py:47`）、canonical JSON（`serialization.py:11`）、双 SHA256（`crypto.py:17`）。tx_id 从「去掉 tx_id 和 signature 的 body」算出来，签名覆盖同一份 body，这个设计是对的，没有循环依赖。
- **难度机制思路正确**：二进制前导零 + 每 10 块 ±1 bit + 容差带（`blockchain.py:80-95`），比十六进制前缀的 16 倍跳变好得多，README 里的解释也写清楚了为什么。
- **共识参数指纹**：`chain_params_hash` 让参数不同的节点连不上（`config.py:66`、`node.py:363-373`），这是很多教学项目想不到要做的。
- **Merkle 奇数补位有配套防护**：`merkle.py` 复制最后一个哈希（CVE-2012-2459 的经典坑），但 `blockchain.py:332-333` 检查块内 tx_id 重复，正好堵住了这个洞。
- **SQLite 有自动迁移**（`sqlite_store.py:117`）、**IPv4/IPv6 双栈**（`node.py:110-162`）、**安全事件页**都已就位。

下面按「现在就是坏的 → 教学概念缺口 → 课堂体验 → 工程化」四层排。

---

## P0 · 现在就是坏的（正确性 / 安全）

### P0-1 挖矿速度被拖慢了 209 倍 ⚠️ 实测

`miner.py:90` 在**每个 nonce** 后面 `await asyncio.sleep(0.001)`。实测：

| | 哈希率 | 难度 20 出一块 |
|---|---|---|
| 现在（每 nonce sleep 1ms） | **835 H/s** | ~25 分钟 |
| 去掉 sleep 后 | **174,757 H/s** | ~23 秒 |

下面第 111 行的 `if nonce % 1000 == 0: await asyncio.sleep(0)` 才是正确的让步写法，那个 `sleep(0.001)` 是多余的。

**副作用**：自动难度会收敛到 2^d ≈ 835×60，也就是难度只能停在 **~16**，学生把难度调到 20 以上课就上不下去了；而且「多几台机器一起挖会更快」这件事也演示不出来（每台都被人为卡在 835 H/s）。

**改法**：删掉 `miner.py:90`，把让步频率改成每 2000 个 nonce 一次。工作量 1 行。

---

### P0-2 分叉后网络永久裂开，不会自愈 ⚠️ 最严重

链路：

1. `blockchain.py:294` — `add_block` 要求 `prev_hash == tip_hash`，否则报 `block does not connect to current tip`。
2. 没有孤块池（orphan pool），拒绝就是彻底丢弃。
3. `node.py:449-475` — BLOCK 被拒绝后只记一条安全事件，**不会回头发 GET_BLOCKS**。
4. `node.py:359-361` — 只有在 HELLO 握手且对方更高时才会请求同步。
5. `config.py:36` 有 `sync_interval_seconds: 10`，五个配置文件里都写了，但**整个代码库没有任何一处读它** —— 定时同步压根没实现。

**后果**：A、B 两个节点同时挖出同高度的块，互相拒绝对方 → 之后 A 挖出 N+1，B 因为 prev_hash 对不上继续拒绝 → 两条链永久分叉，高度一样，谁也不会主动去同步。唯一出路是人工点「同步区块」按钮。

对一个教学模拟器来说这是双重损失：既是 bug，又恰好把「分叉与重组」这个最该演示的概念演示反了。

**改法（三选一，递增）**：
- (a) 最小：BLOCK 被拒且理由是 `does not connect` 时，自动向该 peer 发 GET_BLOCKS。+ 真正实现 `sync_interval_seconds` 定时同步。~50 行。
- (b) 中等：加孤块池，缓存 prev_hash 未知的块，父块到了再串起来。~120 行。
- (c) 完整：多分支存储 + 真正的重组（见 P2-C 路线）。

---

### P0-3 最长链规则用的是「高度」而不是「累计工作量」

`blockchain.py:459-460`：

```python
remote_height = len(blocks) - 1
if remote_height <= self.height():
    return False, "replacement chain is not longer"
```

开了自动难度以后这条规则是**错的**：11 个难度 8 的块会顶掉 10 个难度 20 的块。Bitcoin 用的是 cumulative work（Σ 2^difficulty），而这正好是学生最需要理解的一点。

**改法**：加 `chain_work(blocks) = Σ 2^difficulty`，比较这个而不是长度。~20 行，顺便可以在浏览器页把每块的 work 显示出来。

---

### P0-4 前端 XSS：注入源是别的节点 ⚠️

`app.js` 全文用 `innerHTML` 拼字符串，**零转义**。而下面这些字段是**别的节点通过 P2P 送过来的**，不是本机数据：

| 位置 | 未转义字段 |
|---|---|
| `app.js:244` 日志 | `item.message` |
| `app.js:260-268` 安全页 | `type`、`peer_name`、`source`、`reason` |
| `app.js:293-302` 节点表 | `ip`、`port`、`name`、`status`、`direction` |
| `app.js:341-350` 教师页 | `name`、`status`、`mismatch_reason` |
| `app.js:345` / `app.js:611` | `title="${target}"` ← 属性注入，值里带 `"` 就能逃逸 |
| `app.js:661-668` 区块详情 | `tx.type` |

一个同学把自己节点名改成 `<img src=x onerror=...>`，就能在全班每个人的控制台上执行脚本 —— 而且**最先中招的就是「安全」页**，那个本来是用来看攻击的页面。

**改法**：加一个 `escapeHtml()`，所有插值（含属性）过一遍；或者改成 `textContent` + `createElement`。~1 小时。顺带课堂上这本身就是一个很好的演示。

---

### P0-5 Web API 零鉴权，`?administrator=true` 只是前端障眼法

`app.js:9` 读 URL 参数，`app.js:86-90` 只是把 `.admin-only` 的 `hidden` 属性摘掉。`api.py` 全文**没有任何 Depends / 中间件 / token**。

于是同一个局域网里任何一个学生：

```bash
curl -X POST http://同学IP:8000/api/chain/reset          # 清空别人的链
curl -X POST http://同学IP:8000/api/settings/difficulty \
     -H 'Content-Type: application/json' -d '{"difficulty":255}'   # 让别人再也挖不出块
```

`/api/mining/start`、`/api/transactions`、`/api/peers` 同样全裸。

README 里写了「未做生产级认证授权」，但 `?administrator=true` 的存在会让人以为它是个开关。

**改法（按需选）**：
- 最小：启动时生成一个随机 admin token，打印在控制台，写操作校验 header。~40 行。
- 或者：把 `/api/chain/reset` 和 `/api/settings/difficulty` 限制成只能从 `web_host` 回环访问。
- 或者：明确文档化 —— 「这是故意的，请只在可信局域网使用」，并把 `?administrator=true` 改名成 `?teacher_ui=true` 免得误导。

---

### P0-6 其他确定的 bug

| # | 问题 | 位置 |
|---|---|---|
| a | `seen_message_ids` 是无上限 `set`，长时间跑内存单调增长 | `node.py:57` |
| b | `targetPreview` 对远程数据做 `BigInt(0x${target})` 无 try/catch，一个畸形 target 就让 `renderStatus` 抛异常、控制台半渲染卡死 | `app.js:29-36`、132、345 |
| c | WebSocket `JSON.parse` 无 try/catch；重连固定 1500ms 无退避无上限，服务器关了就无限重试且界面没有任何提示 | `app.js:684-689` |
| d | **二维码是假的** —— `drawJoinCode` 画三个定位点然后用 FNV 哈希喂 xorshift PRNG 填格子，扫不出任何东西，但标题写着「入网码」、`aria-label="P2P 入网码"` | `app.js:186-229` |
| e | 分页 off-by-one：`blocks.length < limit` 才禁用「下一页」，正好 30 条时会翻到空页 | `app.js:588` |
| f | `setupActions()` 是一整个无保护的顺序函数，任何一个元素缺失就会让**后面所有**监听器（挖矿、发送、加节点）静默失效 | `app.js:402-538` |
| g | 区块时间戳只校验「不能超前 2 小时」，没有 MTP（中位时间过去）下界 —— 往回填时间戳可以把 `actual_span` 压到 1 秒，直接操纵难度调整 | `blockchain.py:297-299` + `blockchain.py:87` |
| h | `set_difficulty` 的 fallback 是 `max_difficulty", 12`，但 `DEFAULT_CONFIG` 是 255，两处不一致 | `runtime.py:204` vs `config.py:32` |
| i | 版本号三处对不上：`app/__init__.py` = 0.1.0，`config.py` = 0.1.0，README 标题 = v0.1.1 | — |
| j | README:145 有个 PowerShell 转义符漏出来了：`` 概念。`r`n- "安全" `` | `README.md:145` |

---

## P1 · 教学概念缺口（BTC 最核心的东西还没有）

这一层是「补全」最值钱的地方 —— 都是学生一定会问、而现在答不出来的。

### P1-1 没有减半，没有 21M 上限 🔥

`mining_reward` 是配置里的固定 50.0（`blockchain.py:36`），永远不变。**比特币最标志性的两个机制完全不存在**：

- 减半（每 210,000 块奖励折半）
- 总量上限 2100 万
- 由此推出的通缩模型和「矿工最终靠手续费活」

**改法**：加 `halving_interval`（教学场景设成 10 或 20 块，一节课就能看到几次减半）+ `subsidy(height) = reward >> (height // interval)`，接进 `create_candidate_block` 和 `validate_block` 的 coinbase 校验。前端加一个「距下次减半还有 N 块」和累计发行量曲线。~150 行，**性价比最高的一项**。

### P1-2 没有 coinbase 成熟期

挖到的币立刻可花（`sqlite_store.py:331` 直接把所有 `receiver = address` 的都算进余额）。真实 BTC 要等 100 个确认，原因正是防止重组后花掉不存在的钱 —— 而这个项目 P0-2/P0-3 修好以后**恰好会真的触发这个问题**。

**改法**：`confirmed_balance` 排除 `height > tip - maturity` 的 coinbase。配置 `coinbase_maturity`，教学值设 5~10。~40 行。

### P1-3 没有 UTXO / 找零

现在是账户余额模型（README 已声明）。这是「比特币到底是什么」的核心，也是和以太坊的分水岭。

**代价很大**：要改 transaction 结构、签名内容、余额计算、块校验、mempool、前端。是一次架构级重写（见路线 C）。

**折中方案**：保留账户模型作为默认，另加一个 **UTXO 可视化演示页** —— 用同一批交易数据同时用两种模型渲染，让学生对比。成本只有几百行，教学效果接近。

### P1-4 没有 Merkle 证明 / SPV

`merkle.py` 只有 26 行，只算根。加一个 `merkle_proof(tx_ids, index)` 和 `verify_proof()`，前端在区块详情页每笔交易旁边放个「验证包含性」按钮，把 log₂(n) 条路径哈希画出来 —— **这是投入产出比第二高的教学项**（~80 行后端 + 一个前端面板）。

### P1-5 没有真实区块头序列化 / nBits

现在块哈希是 canonical JSON 的双 SHA256（`block.py:14`），不是 BTC 的 80 字节头。README 已列为限制，但作为「进阶模式」开关加上很有价值：让学生亲眼看到 version(4) + prev(32) + merkle(32) + time(4) + bits(4) + nonce(4) 是怎么排的，以及 nBits 的尾数/指数压缩。

### P1-6 mempool 没有费率市场

`mempool.py:24-27` 满了就直接拒收新交易，而不是淘汰费率最低的。也没有：过期清理、RBF（手续费替换）、按 sat/byte 排序展示、拥堵时的费率曲线。前端的「自动手续费」是写死的 `0.01`（`app.js:469`）。

对「为什么手续费会涨」这个必问问题，现在完全演示不了。~150 行。

---

## P2 · 课堂体验

### P2-1 交易历史完全没有 🔥
发完交易只有一个 2.8 秒的 toast，**tx_id 就永远消失了**（`app.js:515`）。`GET /api/mempool` 存在但前端从来没调用过。而且：

- 所有表格里的哈希都被 `shortHash()` 截断，**没有 `title`，没有复制按钮**（`app.js:663-665`、298、265-266）——学生物理上拿不到完整 tx_id。
- 浏览器页只能按高度或块哈希搜（`app.js:620-631`），**不能按 tx_id 搜，不能按地址搜**。

也就是说：学生发了一笔交易，然后没有任何办法找到它。这是体验上最大的洞。

### P2-2 钱包只有一个
「生成地址」直接顶掉默认钱包（`app.js:493` 写死 `{name:"default"}`），旧钱包的余额还在 DB 里但 UI 里彻底消失，也没有确认弹窗。`list_wallets()` 在 store 里写好了但**没有对应的 API 端点**。另外没有私钥导出 / 导入 / 备份，对一个教「私钥即所有权」的项目来说很违和。

### P2-3 一张图表都没有 🔥
出块时间、难度曲线、算力估算、mempool 深度、累计发行量、余额历史 —— 全部没有。对教学模拟器来说这是最大的表达缺口。数据全都已经在 SQLite 里了，只差一个图表层。

### P2-4 看不见分叉
没有区块 DAG / 分叉可视化。学生永远看不到一次分叉长什么样 —— 而这本该是这个项目的招牌演示。

### P2-5 其他

| 项 | 说明 |
|---|---|
| 教师页要手点刷新 | 全班状态是最需要实时的一屏，却只有按钮刷新（`app.js:391-397`） |
| 实验任务硬编码 | 6 个任务写死在 `app.js:10-17`，老师改不了，应该出配置或 API |
| 没有深链接 | tab 不进 URL hash，刷新就回挖矿页，也没法把某个区块的链接发给同学 |
| 移动端只有一个 980px 断点 | 9 个 tab 无 `flex-wrap` 无 `overflow-x`（`styles.css:175-180`），手机上直接挤爆；表格 `min-width:820px` |
| 无暗色模式 | `color-scheme: light` 写死（`styles.css:2`），但 CSS 变量已经就位（1-19），加起来很便宜 |
| 节点只能加不能删 | 没有断开 / 移除 / 封禁 |
| 安全页只记录不处置 | 没有 peer 评分、没有自动断开恶意节点 |
| 无导出 | 区块 / 交易 / 节点 / 安全事件都不能导 CSV 或 JSON |
| 中文串当状态枚举 | `"参数不匹配"`（`node.py:331`）、`"未挖矿"` 存进 DB 再拿来比较（`runtime.py:326`），很脆，也堵死了 i18n |
| 无障碍 | tab 不是 `role="tablist"`；748 行 CSS 里唯一的 `outline` 是 `input:focus`，按钮全无焦点框；区块详情行只能鼠标点，键盘到不了 |

---

## P3 · 工程化

| # | 项 | 说明 |
|---|---|---|
| 1 | **无 CI** | 没有 `.github/workflows/`。加个 pytest + ruff 的 workflow，一次性 |
| 2 | **3 个测试在无 IPv6 环境直接崩** | `test_peer_metadata_and_classroom_status`、`test_classroom_status_counts_only_parameter_mismatches`、`test_network_info_formats_ipv6_web_and_p2p_addresses` 抛 `OSError: Address family not supported`，应该是 `pytest.skip` 而不是 fail |
| 3 | **P2P 和 API 层零测试** | 34 个测试全在 core / init_node / address。没有双节点同步的集成测试 —— 而 P0-2 这种 bug 正是集成测试才能抓到的 |
| 4 | **无 LICENSE** | 公开仓库没许可证，别人不敢用 |
| 5 | **更新日志塞在 README 里** | 应该拆成 `CHANGELOG.md` |
| 6 | **无 Docker / 一键包** | `main.py:22` 的注释说明在用 PyInstaller `--noconsole`，但仓库里没有打包脚本。课堂场景「一个 exe 双击就能跑」价值很高 |
| 7 | **FastAPI `on_event` 已废弃** | `api.py:40,44` 应改 `lifespan` |
| 8 | **WebSocket 是伪事件流** | `api.py:138-146` 每秒推**全量** status（含 300 条日志 + 安全事件），不管有没有变化；前端每秒重建日志列表、安全表、实验列表，还重画 ~800 格的假二维码（`app.js:183`） |
| 9 | **同步永远传全链** | `GET_BLOCKS` 永远 `from_height=0`（`node.py:361,528`），每次同步整条链。没有 headers-first、没有增量。加上 64MB 的 readline 上限（`node.py:21`），链长了会直接炸 |
| 10 | **P2P 无限流** | 单个 peer 可以无限量发 64MB 消息，没有速率限制 |

---

## 建议的四条路线（选一条或组合）

### 🅰️ 路线 A —「先止血」 · 约 1–2 天
把 P0 全做掉：删 sleep（1 行，速度 ×209）、分叉自愈 + 定时同步、累计工作量最长链、XSS 转义、admin token、seen_message_ids 上限、修 6 个前端崩溃点、真二维码、修 README/版本号。

> **代码量最小、收益最大。不管选哪条路，这条建议先做。**

### 🅱️ 路线 B —「教学补全」 · 约 3–5 天
减半 + 21M 上限 + coinbase 成熟期 + MTP 时间戳校验 + Merkle 证明演示 + 图表面板（出块时间/难度/算力/发行量）+ 交易历史页 + tx_id/地址搜索。

> **对「上课好不好用」提升最大**，且不动架构。

### 🅲 路线 C —「架构升级」 · 约 1–2 周，破坏性大
UTXO 模型重写 + 多分支存储 + 真正的分叉与重组 + 分叉可视化 + 真实 80 字节区块头 + nBits。

> 会改变数据库结构和 P2P 消息格式，**必须先做路线 A**（尤其是累计工作量那条，是重组的前提）。

### 🅳 路线 D —「工程化」 · 约 1 天
CI + 修 IPv6 测试 + 双节点集成测试 + LICENSE + CHANGELOG + Docker/PyInstaller 打包脚本 + lifespan 迁移。

> 可以和任意一条并行，独立性最强。

---

## 我的建议顺序

```
A（止血，必做）
  └─> B（教学补全，收益最高）
        └─> D（工程化，锁住成果）
              └─> C（架构升级，选做）
```

**如果只做一件事**：删掉 `miner.py:90` 那行 `await asyncio.sleep(0.001)`（挖矿快 209 倍）。
**如果只做三件事**：加上分叉自愈（P0-2）和减半机制（P1-1）。
