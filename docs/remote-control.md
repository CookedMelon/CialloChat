# 远程用户控制

控制服务默认监听 TCP **15347**，使用与推流服务相同的受信任 TLS 证书。当前目标为 `chat.v50to.cc`；通知中的地址始终来自服务端 `settings.json`，观看地址统一为 `rtsp://`。

客户端和服务端各自从权限 `600` 的配置文件读取相同的随机管理密码，无需在命令行输入。TLS 校验证书；请求携带以该密码生成的 HMAC 签名、60 秒时间窗和一次性 nonce，服务器持久保存 nonce，重启也不能重放有效窗口内的请求。两端需保持时钟同步。错误签名、超时请求、重复请求、过大消息直接关闭连接，不返回应用层响应；TCP/TLS 握手和关闭仍会产生网络报文。

每个请求最多 8 KiB，服务器最多并行处理四个连接，握手和请求读取限时四秒，每个来源最多每分钟 30 次连接。客户端不会自动重发修改命令；响应丢失时先 `list` 确认实际状态，避免重复刷新。

## 配置与安装

公开样例为 [客户端配置](../config/control-client.example.json) 与 [服务端配置](../config/control-server.example.json)。客户端文件是 `config/control-client.json`，服务端模板填写到 `config/control-server.json` 并安装至私有 `runtime/control-server.json`，已排除出 Git 和源码包。部署时生成相同的随机密码；需要修改密码时同步修改两端的私有配置，服务端在下一次连接读取新值。

```bash
chmod 600 config/control-client.json config/control-server.json
# 服务器原生部署；使用现有虚拟环境，不上传镜像或大文件
# 在 runtime/settings.json 启用 control_enabled；配置 runtime/control-server.json 后：
./streamctl up
# 客户端本机，保留 InstallSpace/scripts 中已有脚本
bash control-scripts/install-path.sh
source ~/.bashrc
```

安装脚本创建 `~/InstallSpace/scripts/ciallochat` 指向项目的 `control-scripts` 文件夹，并创建 `~/InstallSpace/scripts/cialloctl` 命令软链接，将 `~/InstallSpace/scripts` 加入 `.bashrc` 的 PATH。命令通过软链接运行也能定位项目内的配置。有项目 `.venv` 时优先使用其中的 Python，避免 Conda 等已激活环境改变 TLS 运行环境；没有项目虚拟环境时使用当前 Python。普通命令仅需 Python 标准库，流量图表需要安装在实际运行环境中的客户端 Pillow。

阿里云安全组需允许客户端访问 TCP 15347。现有 SSH、推流和观看端口保持原用途；管理 API 9997 和鉴权 9000 仍仅监听本机。TLS 控制进程与媒体进程独立，统一由 `streamctl up/down` 管理；正式服务器用 `systemctl status ciallochat-control`，本地用 `systemctl --user status ciallochat-control` 查看状态。证书每次连接重新加载，现有证书续期机制同时适用于控制服务。完整缓冲服务使用原生 systemd 后端；容器内不启动此控制进程。

## 命令

```bash
cialloctl help
cialloctl help refresh
# 也支持 cialloctl --help、cialloctl -h、cialloctl refresh --help
cialloctl list
cialloctl info alice
cialloctl add alice alice@example.com
cialloctl del alice
cialloctl refresh alice all
cialloctl refresh alice push
cialloctl refresh alice pull
cialloctl refresh alice time
# 可选机器可读输出及替代配置
cialloctl --json list
cialloctl --json info alice
cialloctl --config /安全目录/control-client.json list
```

查看帮助不读取管理密码，也不连接服务器。

`info <user>` 在终端输出该用户的“OBS推流URL”和“播放器输入URL”，包含当前有效密码且完成 URL 编码。它沿用 `list` 对当前推流密码的判断，自动续期后的新密码也能查询。查询不会刷新密码、发送邮件或开始推流计时；推流密码已过期且无已交付的下一代时，提示先刷新，仍返回可用的长期观看 URL。`cialloctl help info` 查看命令帮助。

`list` 以表格按账号顺序输出用户名、邮箱、当前有效推流密码、剩余时长、长期观看密码、最近登录时间及是否正在推流，中文列头自动对齐；`--json list` 提供原始字段、剩余秒数和 `last_login` UTC Unix 时间戳。最近登录指该用户最近一次**成功推流认证**，重连会更新；观看连接、管理操作、密码刷新及认证失败不会更新。表格按客户端本地时区展示日期、时间及 UTC 偏移，无记录显示“从未登录”；升级前的历史登录无法还原，从启用记录后开始统计。登录记录在重启和密码刷新后保留，删除用户时清理。

“是否正在推流”按 MediaMTX 当前直播路径的就绪状态显示“是 / 否”，独立于密码是否有效；媒体服务停止时为“否”，状态查询失败时为“未知”。JSON 对应 `is_streaming` 为 true、false 或 null。

新账号拥有两小时累计推流额度，仅实际发布连接消耗额度，停止推流暂停计时，观看不计入。正在推流时报告当前代；到期且下一代已成功发信时报告未使用的新密码，表格显示“待续期（2小时）”，JSON 状态为 `renewal_ready`。到期但无可用下一代时显示已过期，不把旧密码列为有效。

`add` 创建互不相同的随机推流和观看密码，保存用户邮箱并发送创建邮件。`del` 删除账号，撤销推流及观看连接，清理私有凭据、租约和待发通知。

`refresh all` 更换两类密码；`refresh push` 仅更换推流密码。这两种操作立即废止旧推流代及待续期代，将累计推流额度重置为 7200 秒，未推流时不消耗，撤销旧发布连接。`refresh pull` 仅更换观看密码并撤销旧读者，推流密码和剩余使用额度保持不变。禁用的账号刷新后仍为禁用状态。

`refresh time` 保留当前推流密码和观看密码，将剩余累计推流额度恢复至 7200 秒，正在推流的连接继续使用。未使用的自动续期候选密码被取消；如果额度已经耗尽且 `list` 显示已通知的待续期密码，则保留该显示密码并恢复额度；已过期且没有待续期密码时，恢复原推流密码的额度。禁用状态保持不变。此操作也发送邮件，说明“推流密钥使用时长已被管理员重置为两小时。”。

创建和每次刷新都通知对应用户邮箱。创建邮件标题为“CialloChat用户创建”，手动刷新及自动续期统一为“CialloChat密码刷新”，不显示刷新范围。标题与用户名之间，自动续期添加“当前密钥接近使用上限，自动刷新。”，管理员刷新密码添加“密码已被管理员更新。”。通知完整显示两类密码以及带凭据的推流、观看 URL，例如：

```text
CialloChat用户创建
用户名：alice
推流密码：<随机推流密码>
观看密码：<随机观看密码>

OBS推流URL：rtmps://chat.v50to.cc/live/alice?user=alice&pass=<推流密码>
播放器输入URL：rtsp://watch.v50to.cc/live/alice?read_key=<观看密码>
测试频道URL：rtsp://watch.v50to.cc/test

推流密钥可用时长2小时
```

客户端结果区分 `email_status=sent` 和 `queued`。SMTP 失败不回滚已生效的密码，通知保存在数据库并至少间隔 60 秒重试，重启也不丢失。重试时从当前设置生成 URL，迁移到域名后不会沿用旧 IP。已被后续操作替代的密码通知不会再发送。SMTP 接受邮件与最终到达收件箱是不同阶段，超时重试可能重复邮件，使用固定 Message-ID 辅助去重。

## 凭据与备份

媒体鉴权继续使用 Argon2id 哈希；一秒缓冲入口转发客户端提供的观看密码，不代填服务端密码。为满足管理端查询当前明文密码，新增私有 `credential_vault` 表，与租约、待发通知一起保存在权限 `600` 的 `runtime/leases/leases.sqlite3`；目录权限 `700`。自动续期的新密码成功发信后仍保留在私有库，首次使用时同步当前凭据。运行日志不输出密码；`list` 的输出本身含敏感信息，不应贴到公开位置。

本地完整实例创建独立的 `cc`，不与公网共用媒体密码；公网升级必须保留原账号及租约。旧的仅有哈希、没有原始交付密码的账号无法反查密码，可以用 `refresh all` 生成并存档新的两类密码；新建/刷新账号推荐统一使用本控制脚本。本地 `streamctl user reset-*` 不更新控制凭据库，列表不会返回与当前鉴权不匹配的旧密码。

迁移时停服备份整个 `runtime/leases`，并连同 accounts/settings/SMTP/控制配置一起安全保存。普通 `streamctl backup` 不包含明文凭据库和通知队列。不要清空租约数据库或恢复过时租约，否则可能恢复旧密码。

## 服务流量记录与邮件报告

```bash
cialloctl traffic
cialloctl help traffic
cialloctl traffic --start "2026-10-04 09:00" --end "2026-10-04 12:00"
cialloctl traffic --start "2026-10-04T09:00+09:00" --end "2026-10-04T12:00+09:00" --output ~/traffic.png
cialloctl --json traffic
```

默认显示最近六个已经结束的十分钟区间。起止时间必须同时指定、按整十分钟对齐，并位于最近七天已经结束的区间内；未带偏移的时间使用客户端本地时区。文字和图片都显示实际起止时间。长区间自动合并柱子，全部记录仍计入总和；每个柱子按账号的推流接收量和观看发送量堆叠。PNG 在客户端生成并保存（默认 `runtime/reports/traffic-<编号>.png`），随后发送至管理员邮箱；终端同时输出每个账号的推流、观看与合计，以及全局三类总计。`--json` 也会生成并发送报告，另输出图表路径、原始十分钟数据和邮件状态。

普通管理命令只需标准库，`traffic` 额外需要 **客户端** Python 的 Pillow，服务器无需安装绘图库。有项目 `.venv` 时用 `.venv/bin/python -m pip install Pillow` 安装客户端绘图库。其他电脑可在运行脚本的同一 Python 环境安装 Pillow；Linux 若需中文账号字体，可安装 Noto CJK。图中 push 为推流、view 为观看；灰色区间表示记录不完整。图片上限为 256 KiB、1600×1200，上传图片前先完成小报文鉴权，非法密码不会触发大报文读取；上传限时五秒。每分钟最多六份报告，最多保存 64 份近期报告/待发邮件。管理员邮箱由服务器私有 SMTP 配置的 `admin_recipient` 指定，客户端不能指定收件人。每次调用发送一封报告，采集本身不定时发邮件。邮件失败写入独立持久队列，至少间隔 60 秒重试；SMTP 接受不保证立即抵达收件箱，响应丢失时可能需人工检查邮件。

watchdog 复用已有每秒媒体 API 查询，以连接 ID 的字节差累计，只统计 `live/<已配置账号>` 的发布接收和播放发送。每分钟及跨十分钟边界做小型 SQLite 检查点，数据保存在权限 `600` 的 `runtime/traffic/traffic.sqlite3`，不与鉴权库共用事务。历史记录每分钟清理，保留七天；新部署无法补回启用前历史。`traffic_enabled` 设置默认为 true，可改为 false 后应用配置停用采集。

多个观看连接的流量合并到直播账号；共享观看密码无法区分每个实际观众。账号删除后，其七天内的历史流量仍保留。无推流且无观看数据时累计量为零；采集缺失不等于零，报告会标注缺失区间。重新连接、更换连接 ID、计数回零及 watchdog 重启有对应处理。无法保证采集到断连前最后约一秒的字节；异常掉电可能丢失至多最近一分钟检查点尚未持久化的信息，新检查点有机会从仍存活的连接累计计数恢复字节。跨边界的差值按时间比例分配，采集间隔过长会标记不完整。应用层计数不包含完整的 TLS/TCP/IP 开销、重传或无效鉴权报文，不能用来代替阿里云流量计费。其他服务器服务的流量不计入。


流量报告正文只保留时间段、每个账号一行的推流/观看/合计、全局三类总计与预计人民币价格。图表通过 HTML 正文中的 CID 图片内嵌，邮件同时提供纯文本正文。价格配置为服务器 `runtime/traffic/pricing.json`，参考 [计价配置](../config/traffic-pricing.example.json)。

当前报告按固定单价计算：**预计价格 = 观看出站字节数 ÷ 1024³ × 0.80 元**，推流入站单价为 0。报告不扣除月免费额度，不根据月累计量切换阶梯。`model` 为 `flat`，`bytes_per_gb`、`push_cny_per_gb`、`pull_cny_per_gb` 分别配置计价单位的字节数及两个方向的人民币单价。图中和正文使用同一个预计金额。
