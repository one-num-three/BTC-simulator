# BTC 模拟器 v0.2.0

这是一个教学型 BTC 模拟软件，不是真实 Bitcoin 钱包，不连接主网或测试网，也不兼容真实 BTC 地址。MVP 使用账户余额模型、SQLite 本地存储、ECDSA secp256k1 签名、TCP JSON Lines P2P 和本地 Web 控制台。

私钥在 MVP 中以明文保存到 SQLite，仅用于本地模拟教学，不能用于真实资产。

## 安装

```bash
python -m pip install -r requirements.txt
```

## 启动单节点

```bash
python main.py --config config.json
```

打开：

```text
http://127.0.0.1:8000
```

首次启动会自动创建配置、SQLite 数据库、默认钱包和创世块。

## 启动三个本地节点

分别开三个终端：

```bash
python main.py --config config_node1.json
python main.py --config config_node2.json
python main.py --config config_node3.json
```

Web 控制台：

```text
server1 http://127.0.0.1:8000  P2P 127.0.0.1:7464
server2 http://127.0.0.1:8001  P2P 127.0.0.1:7465
server3 http://127.0.0.1:8002  P2P 127.0.0.1:7466
```

## 初始化局域网节点

如果多台电脑在同一个局域网内，可以让每个人初始化自己的节点并加入网络。第一台电脑可以作为种子节点，后续节点只需要知道种子节点的局域网 IP 和 P2P 端口。

### 1. 第一台电脑创建种子节点

```bash
python scripts/init_node.py --name seed --config config_seed.json --no-prompt
python main.py --config config_seed.json
```

脚本会生成一个配置文件，默认：

```text
Web 控制台: 127.0.0.1:8000
P2P 节点:   0.0.0.0:7464
数据库:     ./data/seed.db
```

本机打开：

```text
http://127.0.0.1:8000
```

默认情况下，Web 控制台只允许本机打开，不会暴露给整个局域网。如果确实需要让其他电脑访问这个 Web 页面，需要显式指定：
```bash
python scripts/init_node.py --name seed --config config_seed.json --web-host 0.0.0.0 --no-prompt
```

此时局域网内其他电脑可以打开：
```text
http://种子节点局域网IP:8000
```

把种子节点的 P2P 地址告诉其他人，例如：

```text
192.168.1.23:7464
[2001:db8:1234::23]:7464
```

如果同一堂课需要隔离成不同网络，可以指定 `network_id`：

```bash
python scripts/init_node.py --name seed --network-id class-a --config config_seed.json --no-prompt
```

### 2. 其他电脑加入网络

在另一台电脑上任选 IPv4 或 IPv6 地址初始化节点：

```bash
# IPv4
python scripts/init_node.py --name alice --config config_alice.json --peer 192.168.1.23:7464 --no-prompt

# IPv6
python scripts/init_node.py --name bob --config config_bob.json --peer "[2001:db8:1234::23]:7464" --no-prompt

# 启动对应节点，例如
python main.py --config config_alice.json
```

启动后节点会自动连接 seed peer，并通过 `HELLO/PEERS/GET_BLOCKS/BLOCKS` 加入网络、同步区块。也可以在 Web 控制台“节点”页手动添加其他节点：

```text
IP:   192.168.1.23
Port: 7464
```

IPv6 在命令行和配置说明中使用标准的 `[IPv6]:port` 格式；Web 节点页的 IP 和 Port 是分开的，因此 IP 输入框直接填写 `2001:db8:1234::23`，不需要方括号。

### 3. 同一台电脑跑多个节点

同一台电脑上端口不能重复，需要分别指定不同端口：

```bash
python scripts/init_node.py --name node1 --config config_node1.json --listen-port 7464 --web-port 8000 --no-prompt
python scripts/init_node.py --name node2 --config config_node2.json --listen-port 7465 --web-port 8001 --peer 127.0.0.1:7464 --no-prompt
```

不同电脑上可以使用相同端口，因为它们的 IP 不同。

### 4. 局域网注意事项

- P2P 连接使用 `listen_ip/listen_port`。默认 `enable_ipv6 = true`，当 `listen_ip = 0.0.0.0` 时会同时尝试监听 `[::]:7464` 和 `0.0.0.0:7464`。
- Web 控制台使用 `web_host/web_port`，初始化脚本默认 `127.0.0.1:8000`，只允许本机访问；需要局域网访问时再手动加 `--web-host 0.0.0.0`。
- 如果连接失败，检查系统防火墙是否允许 Python 入站，或是否放行了 `7464` 和 `8000`。
- 连接其他电脑时不要填 `127.0.0.1`，要填对方的局域网 IP，例如 `192.168.1.23`。
- 使用 IPv6 链路本地地址时需要带网卡作用域，例如 `fe80::1234%en0`；命令行形式为 `[fe80::1234%en0]:7464`。
- 可以用 `python scripts/init_node.py --no-ipv6 ...` 关闭 IPv6。若 Web 控制台也要仅通过 IPv6 访问，可把 `web_host` 设置为 `::`，浏览器地址写成 `http://[IPv6地址]:8000`。
- VPN、代理或虚拟网卡可能让自动检测选中不可供同学访问的地址。此时初始化时使用 `--advertise-ip 真实局域网IPv4 --advertise-ipv6 真实局域网IPv6`，或直接修改配置中的 `advertise_ip` / `advertise_ipv6`。
- “入网”页会显示本机 Web 地址、P2P 地址、Network ID 和参数 Hash，可直接复制给其他同学。
- 本项目没有登录鉴权，只建议在可信局域网内演示，不要暴露到公网。

## 课堂功能

Web 控制台内置多个课堂辅助页：

- “交易”：本机钱包的收支记录，含待确认、确认数、方向，可按地址查询、导出 CSV。
- “图表”：出块间隔与目标对比、难度曲线、估算算力、累计发行量、每块奖励与手续费、内存池费率分布。
- “入网”：显示本机 Web 控制台地址、P2P 地址、Network ID、参数 Hash，并提供复制按钮和**可以真正扫出来的二维码**。
- “实验”：任务清单由服务端下发，覆盖钱包、入网、连接节点、挖矿、成熟期、内存池、同步等关键概念。
  在配置文件同级目录放一个 `lab_tasks.json` 就能整体替换，不用改代码。
- “安全”：显示被拒绝的非法交易、非法区块和异常同步来源，可直接封禁来源节点。
- “教师”：只有可信会话（本机，或带 admin token）才会显示，自动刷新，
  用于查看本机和已知学生节点的高度、目标值、挖矿状态、连接状态和参数 Hash。

“浏览器”页的搜索框同时支持区块高度、区块 hash、交易 ID 和钱包地址；
每笔交易旁边的「验证」按钮会展开 Merkle 包含性证明，逐步显示这笔交易怎么用
log₂(n) 个哈希走到区块头里的 merkle_root——这就是 SPV。

节点连接时会检查：

```text
network_id
chain_params_hash
```

`chain_params_hash` 由初始难度、自动难度规则、目标出块时间、挖矿奖励、区块交易上限等共识参数计算得到。若某台电脑的参数不同，教师页会显示“参数不匹配”，该节点不会加入当前课堂网络。

## 使用流程

1. 打开节点 Web 控制台。
2. 在“接收”页复制某个节点地址。
3. 在另一个节点“发送”页填写接收方地址、金额和手续费。
4. 交易验证通过后进入本地内存池并广播到已连接节点。
5. 在“挖矿”页点击“开始挖矿”。
6. 挖到新区块后，coinbase 奖励是 `当前区块补贴 + 区块手续费`，区块会保存到 SQLite 并广播。
   补贴按 `halving_interval` 减半，所以它会随高度下降。
7. 刚挖到的奖励要过了成熟期才能花，控制台会分开显示「已确认」和「未成熟」。
8. 在“交易”页查看这笔交易的确认数，或用交易 ID 在“浏览器”页搜索它。
9. 在“图表”页观察难度怎么跟着出块速度调整、发行量怎么逼近上限。
10. 在“入网”页复制 P2P 地址或让同学扫二维码。
11. 在“实验”页按任务清单完成课堂实验。
12. 在“教师”页查看全班节点状态。

## 配置

核心字段：

```json
{
  "network_id": "btc-sim-classroom",
  "listen_ip": "0.0.0.0",
  "enable_ipv6": true,
  "advertise_ip": null,
  "advertise_ipv6": null,
  "listen_port": 7464,
  "web_port": 8000,
  "difficulty_mode": "binary_leading_zero",
  "difficulty": 12,
  "auto_difficulty": true,
  "target_block_seconds": 60,
  "difficulty_adjustment_interval": 10,
  "difficulty_adjustment_tolerance": 0.25,
  "min_difficulty": 0,
  "max_difficulty": 255,
  "mining_reward": 50.0,
  "halving_interval": 20,
  "coinbase_maturity": 5,
  "mempool_max_bytes": 314572800,
  "mempool_expiry_seconds": 3600,
  "min_relay_fee": 0.0,
  "sync_interval_seconds": 10,
  "peer_connect_timeout_seconds": 5,
  "trust_loopback_admin": true,
  "require_admin_for_writes": true,
  "servers": [["127.0.0.1", 7464], ["2001:db8:1234::23", 7464]],
  "storage": {"type": "sqlite", "path": "./data/blockchain.db"}
}
```

### 发行与成熟期

`halving_interval` 决定区块奖励每隔多少个区块减半。真实比特币是 210000 个（约四年），
这里默认 20，一节课就能看到好几次减半，以及总量收敛到上限。
上限刻意不是整数：创世块不发币，深层纪元还有精度截断——
比特币的 20999999.9769 也是同样的原因。

`coinbase_maturity` 决定挖矿奖励要被埋多少个区块才能花。真实比特币是 100。
这条规则存在的原因是重组：如果奖励可以立刻花掉，一次链重组就能把发钱的那个区块撤销，
而钱已经花出去了。控制台会把余额拆成「已确认」和「未成熟」两部分显示。

这两个都属于共识参数，会进入 `chain_params_hash`，同一课堂网络必须一致。

### 手续费与内存池

内存池满了会淘汰费率（每字节手续费）最低的交易，而不是一律拒收新交易；
矿工也按费率从高到低打包。这样「网络拥堵时手续费会涨」才演示得出来。
`mempool_expiry_seconds` 控制交易等待多久后被丢弃，
`min_relay_fee` 是最低中继手续费。发送同样金额给同一地址但手续费更高的交易，
会替换掉内存池里那一笔（RBF）。

默认使用 `difficulty_mode = "binary_leading_zero"`。`difficulty` 表示区块 hash 的二进制前导 0 位数，例如 `difficulty = 12` 表示 hash 的前 12 个二进制位必须为 0，对应的初始 target 形如 `000ffff...`。新区块仍携带完整 256 位十六进制 `target`，只有当区块 hash 的数值小于等于 target 时才有效；target 越小，挖矿越难。

默认开启 `auto_difficulty`，目标出块时间为 `target_block_seconds = 60`，即约 1 分钟一块。模拟器每 `difficulty_adjustment_interval = 10` 个区块评估一次最近出块速度：

- 如果出块持续偏快，下一次难度增加 1 个二进制前导 0 位。
- 如果出块持续偏慢，下一次难度减少 1 个二进制前导 0 位。
- 如果出块速度仍在容差范围内，难度保持不变。

这种方式每次只让难度约变化 2 倍，避免十六进制前导 0 带来的 16 倍跳变。

真实 BTC 是每 2016 个区块、约 14 天调整一次。这里把窗口缩短到 10 个区块，是为了本地教学演示能看见调整效果。同一个模拟网络里的所有节点必须配置成相同的难度规则，否则会拒绝彼此挖出的新区块。

初始前导零或精细目标前缀可以在管理员模式下调整：访问 `/?administrator=true` 后，“挖矿”页会显示设置。保存后会写回当前节点的配置文件；如果节点正在挖矿，系统会先暂停。已有区块时，修改初始目标后需要重置到创世块才能从新目标开始。多节点演示时请把每个节点设置成相同规则。

管理员模式下也会显示“重置到创世块”按钮。该操作会保留钱包和节点记录，清空本节点的历史区块、交易和内存池，只留下创世块。需要重置整个本地网络时，请分别在每个节点执行一次。

## 用 Docker 起一个课堂网络

不想装 Python 依赖时：

```bash
docker compose up
```

会起三个已经互相连好的节点：

```text
http://127.0.0.1:8000   http://127.0.0.1:8001   http://127.0.0.1:8002
```

单个节点：

```bash
docker build -t btc-simulator .
docker run --rm -p 8000:8000 -p 7464:7464 -v "$PWD/data:/app/data" btc-simulator
```

## 开发

```bash
python -m pip install -r requirements.txt httpx ruff
python -m pytest tests -q      # 170 个测试
python -m ruff check app tests scripts main.py
python scripts/smoke_test.py   # 起节点、挖块、发交易的端到端检查
```

测试分层：

| 文件 | 覆盖 |
|---|---|
| `test_core.py` | 钱包、签名、区块、难度调整、内存池基础 |
| `test_consensus.py` | 累计工作量选链、孤块与分叉自愈、MTP 时间戳 |
| `test_economics.py` | 减半、总量上限、coinbase 成熟期 |
| `test_merkle_proof.py` | 包含性证明的生成与校验 |
| `test_mempool_policy.py` | 费率淘汰、RBF、过期、最低中继费 |
| `test_api.py` | HTTP 鉴权、查询接口、WebSocket 增量推送 |
| `test_two_node_network.py` | **两个真实节点跑在真实 socket 上**：同步、挖矿竞争后自愈、参数不匹配拒绝、封禁 |
| `test_btc_format.py` | 真实 80 字节区块头与 nBits，用比特币创世块做基准 |
| `test_frontend_safety.py` | 静态防回归：禁止 innerHTML 拼接、二维码必须是真编码器 |
| `test_network_address.py` / `test_init_node.py` | IPv4/IPv6 地址处理与初始化脚本 |

`test_two_node_network.py` 是这轮改动里最该保留的一个文件：
「挖矿竞争导致网络永久分叉」这类问题只有在两个节点交互时才会出现，
单节点单元测试永远测不到。

## 查看数据库

```bash
sqlite3 data/blockchain.db
```

常用查询：

```sql
SELECT height, hash, timestamp FROM blocks ORDER BY height;
SELECT type, sender, receiver, amount, fee FROM transactions;
SELECT tx_id, sender, receiver, amount, fee FROM mempool;
```

## 当前限制

- 不实现真实 Bitcoin 主网协议，也不兼容真实 BTC 地址格式。
- **仍是账户余额模型，不是 UTXO**，因此没有找零和 Script。
- 区块头的**共识哈希**是 canonical JSON 的双 SHA256，不是真实的 80 字节序列化。
  这是有意的：这样区块头可读，学生能直接看到哈希的输入。
  「浏览器」页的「看 80 字节区块头」按钮会把同一个区块按真实比特币规则序列化出来，
  逐字段展示小端序、反转字节序和 nBits 压缩编码——但那一面板只用于教学展示。
- 分叉处理是「整条链替换」，不保存多个分支，也没有部分重组。
- **钱包私钥以明文保存在 SQLite 里，并且可以明文导出。** 这是有意的教学取舍，
  用来演示「私钥即所有权」，绝不能用于真实资产。
- 鉴权只有一个节点级 admin token，没有多用户、没有 HTTPS。
  只建议在可信局域网内使用。

## 安全模型

读接口对局域网开放，这正是课堂互相观察需要的。写接口需要「可信」：

- 请求来自节点自己这台机器（回环地址），或
- 请求带上本节点的 admin token（`X-Admin-Token` 头，或网址后加 `?token=...`）。

token 在首次启动时生成，保存在数据库同级目录的 `admin_token` 文件里，并打印在终端。
老师要远程控制某个节点时，用 `http://某节点:8000/?token=那个节点的token` 打开控制台即可。

这条界线不是摆设：`POST /api/transactions` 会用本节点私钥签名，
没有它的话，局域网上任何人都能把你钱包里的币转走。

两个开关可以调整这个行为：

```json
{
  "trust_loopback_admin": true,
  "require_admin_for_writes": true
}
```

把 `require_admin_for_writes` 设为 `false` 就回到完全开放的模式。
