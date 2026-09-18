# sysu-clash-sub ｜ 中山大学校园网可用 Clash 订阅

把 10 个公开的 GitHub Clash/V2Ray 节点项目合并成一份可直接用的 Clash Meta 配置：

- **每天/每 15 分钟**由 GitHub Actions 抓取、清洗、去重、生成并提交（上游全挂时保留上一版可用文件）
- **输出自带完整分流规则**：内网/保留网段、Tailscale 直连，国内域名/IP 直连，广告拦截，其余走节点 —— 导入就能用，不用再手写规则
- **节点质量把关**：按协议校验必填字段（脏节点会让整份订阅在客户端加载失败，直接丢弃）、按类型+传输参数去重
- **公平取样**：单个上游最多占 `PER_SOURCE_LIMIT` 个名额，再按源轮流填充到 `MAX_NODES`，避免节点最多的那个源垄断整个订阅

## Clash Verge 导入

把下面的 `OWNER/REPO` 换成你的仓库路径，在 Clash Verge Rev 的订阅管理里粘贴：

```text
https://raw.githubusercontent.com/OWNER/REPO/main/output/clash.yaml
```

`output/source-status.json` 记录每个上游最近一次抓取的结果：收到多少、新增多少、丢弃多少（非法/重复/超配额）。

## 生成配置里有什么

| 部分 | 内容 |
|---|---|
| 规则 | 私网/保留网段直连 → `GEOSITE,category-ads-all` 拦截 → `GEOSITE,cn` + `GEOIP,CN` 直连 → `MATCH,PROXY` 兜底 |
| DNS | `fake-ip` + `ipv6: false`（IPv6 经 TUN 常挂超时才回落，关掉可免掉数秒首包延迟） |
| 分组 | `AUTO`（url-test，`lazy: true` 不测速就不发探测）+ `PROXY`（手动选择，含 AUTO/DIRECT/所有节点） |
| 其他 | `unified-delay`、`tcp-concurrent`，`allow-lan: false` |

## 可用性实测（节点不显示 Timeout）

公开节点的死亡率很高：**在本机（国内校园网）实测 400 个节点，只有 153 个能真的连上（38%）**，其余不是握手失败就是超时。测速用的不是手写探测脚本，而是 **mihomo 内核自己的 `/proxies/<名字>/delay` 接口** —— 和你客户端用的是同一套实现，结果与客户端一致（脚本见 `scripts/healthcheck.py`）。

| 在哪测 | 视角 | 用途 |
|---|---|---|
| GitHub Actions（工作流里默认开） | 境外机房 | 粗筛"彻底死掉"的节点；**无法模拟国内网络** |
| 你本机 `python3 scripts/local_healthcheck.py` | 你真实上网的那张网 | 真正决定"你能不能用"，产出 `output/clash-cn.yaml` |

**Actions 不可能模拟国内网络**：runner 在境外机房，出境线路、DNS、被墙情况都和你的校园网不同，境外能连不代表国内能连，反之亦然。真要"国内视角"，只有两条路：在你自己的机器上测（推荐，零成本），或者有一台国内 VPS 定时跑同样的脚本。

本地测速：

```bash
python3 scripts/local_healthcheck.py                     # 默认读 output/clash.yaml
python3 scripts/local_healthcheck.py --concurrency 32     # 网络好可以更激进
```

产物 `output/clash-cn.yaml`：只保留实测能连的节点，**按延迟升序**，节点名前带 `[xxx ms]` 方便挑。把它提交/推回仓库，手机和其他设备订阅它就等于订阅"你这张网实测可用"的列表。

```bash
git add output/clash-cn.yaml && git commit -m "chore: 本机实测可用节点" && git push
```

### 出口检测（连得上 ≠ 能翻墙）

筛选只解决"能不能连上"。实测发现**最快的几个节点出口就在国内**（例如 94ms 的 `🇨🇳_CN_中国`、196ms 的 `🇭🇰HK_1` 出口都是国内 IP），连得上但毫无用处；另外很多"不同"节点其实共用同一个出口 IP。

`scripts/exit_check.py` 补上这一步：给每个节点开一个本地入口（mihomo 的 `listeners` 支持把入口绑到指定节点），从那个入口访问回显服务拿真实出口。于是：

- **淘汰出口在封锁名单里的节点**（默认 `CN`，用 `EXIT_BLOCK_COUNTRIES=CN,HK` 这类可调）；
- **按出口 IP 去重** —— 实测 225 个可用节点只对应 147 个不同出口，去掉 77 个共用出口的冗余；
- 节点名前标注 `[延迟 XX国]`，`output/clash-cn-exits.json` 里能查到每个节点的出口 IP。

出口位置与"你在哪测"无关（节点从哪出去就是哪），所以 **Actions 也能做这一步**，CI 输出同样经过出口筛选。

### 自动跑（本机定时测速 + 推回仓库）

`deploy/` 里是现成的 systemd user 单元：装好后每 6 小时在本机实测一次，并把结果推回仓库（用 API，绕开校园网里会失败的 `git push`）。

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
install -m755 deploy/clash-cn-healthcheck.sh ~/.local/bin/
install -m644 deploy/clash-cn-healthcheck.service deploy/clash-cn-healthcheck.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now clash-cn-healthcheck.timer
systemctl --user list-timers clash-cn-healthcheck      # 看下次几点跑
tail -f ~/.local/state/clash-cn-healthcheck.log        # 看每次结果
```

令牌：`scripts/local_healthcheck.py` 会先读环境变量 `GITHUB_TOKEN`，读不到就从 `~/.git-credentials-hermes` 里取「用户名:令牌」（该文件权限保持 600）。

手机和其他设备订阅下面这条，拿到的就是"你那张网实测可用 + 按延迟排序"的列表：

```text
https://raw.githubusercontent.com/<你的账号>/<你的仓库>/main/output/clash-cn.yaml
```

### 手机用精简版（`output/clash-lite.yaml`）

手机所在网络和电脑不是一张网，"电脑测出来能用"未必搬得过去；而且移动端一次 url-test 扫上百个节点又慢又费电（实测手机端一份 146 节点的列表，真正能用的只有十个左右）。

所以本机实测脚本会额外生成一份**精简版**：按出口国家分组，每组只留延迟最低的几个（默认每国 3 个、最多 5 组、总共 ≤18 个），并保留 `AUTO`（懒测速）与 `PROXY` 选择组。

```text
https://raw.githubusercontent.com/<你的账号>/<你的仓库>/main/output/clash-lite.yaml
```

调节：`LITE_PER_COUNTRY=2 LITE_MAX_COUNTRIES=3 python3 scripts/local_healthcheck.py --from-repo --push`。

手机上如果某些节点仍不通，**先在分组里手动切换**（US/JP/SG/HK/TW 各一组，每组 3 个）—— 手机网络对线路的封锁和校园网不同，分组就是为了让你几秒内换个出口试，而不是让客户端去慢慢测。

### 手机端（Termux）自己测一遍

手机和电脑不是一张网，最好让手机按自己的网络测一次。Termux 里跑的是同一套脚本（Python + mihomo 内核），产出 `output/clash-phone.yaml` 与 `output/clash-phone-lite.yaml`。

```bash
pkg install git python curl
git clone https://github.com/Phirisyyds/sysu-clash-sub.git ~/sysu-clash-sub
cd ~/sysu-clash-sub
bash deploy/termux-setup.sh          # 装 PyYAML + 下载 Android 版 mihomo 内核
bash deploy/termux-healthcheck.sh    # 按手机网络实测
```

- 手机网络常拉不下 GitHub 仓库：`termux-setup.sh` 里已经带了兜底 —— `git clone` 失败就用 API 逐文件下载脚本。
- `pip install pyyaml` 慢或失败就换国内 PyPI 镜像。
- **测速前先关掉 FlClash 的 VPN**，否则测速流量会绕经它，结果不准。
- 想让它顺手推回仓库：准备令牌文件 `~/.git-credentials-hermes`（600 权限），然后 `PUSH=1 bash deploy/termux-healthcheck.sh`。之后 FlClash 直接订阅 `output/clash-phone-lite.yaml` 就是"手机网络实测可用"的列表。

### 测速前必须绕开本机 TUN（否则数字虚高）

本机开着 Clash 的 TUN 时，测试实例的出站流量会被 TUN 抓走、绕经你的代理 —— 等于"用代理去连节点"，结果虚高。同一批 40 个节点的 A/B 实测：

| 条件 | 可用 |
|---|---|
| TUN 开着测 | 22/40（55%） |
| TUN 关掉测（真实） | 7/40（17.5%） |

`local_healthcheck.py` / `update.py` 现在会**在测速期间自动临时关闭本机 TUN**（通过核心 API 改运行时配置，不动你的配置文件），测完自动恢复（`atexit` 兜底）。测试那几十秒里，只有依赖 TUN 的应用会短暂走直连；不需要这个行为就加 `--keep-tun`。

顺带一提：CI（GitHub Actions）里没有 TUN，不受影响；所以 `clash.yaml` 的数字一直是准的，被污染的是本机跑出来的那份。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_NODES` | `400` | 输出节点上限，太大影响客户端启动与测速 |
| `PER_SOURCE_LIMIT` | `150` | 单个上游最多贡献多少节点 |
| `FETCH_TIMEOUT` | `20` | 单个上游抓取超时（秒） |
| `FETCH_RETRIES` | `3` | 抓取失败重试次数（指数退避） |
| `MIXED_PORT` | `7890` | 输出配置的混合端口（导入客户端时通常会被客户端设置覆盖） |
| `HEALTH_CHECK` | `0` | 是否做可用性实测（CI 里设为 `1`） |
| `HEALTH_CONCURRENCY` | `16` | 测速并发（CI 24，本机可到 32） |
| `HEALTH_TIMEOUT_MS` | `5000` | 单节点超时（毫秒） |
| `HEALTH_MAX_TEST` | `1200` | 最多测多少个候选节点 |
| `HEALTH_MIN_ALIVE` | `60` | 可用节点少于这个数就回退到未筛选列表（避免视角不对时误杀） |
| `MIHOMO_BIN` | 自动找 | mihomo 内核路径 |
| `EXIT_CHECK` | `0` | 是否测出口 IP（CI 里设为 `1`） |
| `EXIT_BLOCK_COUNTRIES` | `CN` | 淘汰哪些出口国家（逗号分隔） |
| `LITE_PER_COUNTRY` | `3` | 手机精简版每国留几个 |
| `LITE_MAX_COUNTRIES` | `5` | 手机精简版最多几组 |
| `LITE_MAX_NODES` | `18` | 手机精简版总节点上限 |

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows 用 .venv\Scripts\pip
.venv/bin/python scripts/update.py
```

若本机被 `/etc/hosts` 钉死 GitHub 域名或开着 TUN 代理，直接跑会全部抓取失败（域名被解析到错误 IP）。这时用本地验证包装，它把域名解析交给本机 mihomo 核心（unix socket 在 `/tmp/verge/verge-mihomo.sock`）：

```bash
python3 scripts/local_run_mihomo.py
```

CI 不需要这个包装脚本。

## 安全与合规

这些节点来自未知的第三方公开服务，不能视为可信 VPN。不要通过它们登录银行、邮箱、代码仓库或传输敏感数据；请遵守所在地区法律和各上游项目许可证。上游可能随时删节点或改格式，工作流会在全部源失败时退出并保留上一版可用文件。

## 源的类型

`sources.yaml` 里的源分两类：

- **Clash YAML**（`proxies:` 列表）—— 默认类型。
- **分享链接订阅**（机场常见的 `ss:// vmess:// vless:// trojan:// hysteria2://` 列表，整体 base64 或一行一条）—— 标 `format: base64`，由 `scripts/sub_convert.py` 转成 Clash 节点。实测这类源能额外贡献 260+ 个候选节点（ZywChannel 232 个、Pawdroid 20 个、freefq 14 个）。

转换器还会拦掉上游的垃圾数据：保留地址（`127.0.0.0` 这种公告占位节点）直接丢弃。

## 上游项目

见 [`sources.yaml`](sources.yaml)。来源包括 PuddinCat/BestClash、Au1rxx/free-vpn-subscriptions、awesome-vpn/awesome-vpn、vxiaov/free_proxies、ermaozi/get_subscribe、anaer/Sub、ermaozi01/free_clash_vpn、peasoft/NoMoreWalls、NiceVPN123/NiceVPN、chengaopan/AutoMergePublicNodes。
