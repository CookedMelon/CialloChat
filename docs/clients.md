# OBS 与播放器接入

通过 `cialloctl info <user>` 获取“OBS推流URL”和“播放器输入URL”。两个 URL 分别包含独立的推流密码和观看密码，不能互换。

OBS 选择自定义直播服务，将完整 OBS推流URL 填入服务器，串流密钥留空。本地完整服务使用 `rtmp://<内网地址>:1935/live/<user>?user=<user>&pass=<推流密码>`；正式服务器使用 `rtmps://chat.v50to.cc/live/<user>?user=<user>&pass=<推流密码>`。

正式播放器输入 URL 为 `rtsp://watch.v50to.cc/live/<user>?read_key=<观看密码>`；本地仍使用 `rtsp://<服务地址>:8554/live/<user>?read_key=<观看密码>`，通过 RTSP/TCP 播放。VRC 房间选择直播/AVPro 模式，并允许不可信 URL。VLC 可直接打开该地址，或使用 `vlc --rtsp-tcp <观看URL>`。

播放器若先请求 UDP，缓冲入口返回 461 并保留协商连接，使 VRC 能在同一连接回退到 TCP。

缓冲入口只支持带查询参数的上述观看地址；旧 URI 用户名密码形式仅适用于未经过缓冲的原生兼容入口。

## 已验证的编码建议

- 视频 H.264，2560×1440，按需求选择 30 或 60 fps，CBR 3200 Kbps。
- 音频 AAC，48 kHz，128–160 Kbps。
- x264 `veryfast`，调优 `zerolatency`，关键帧一秒，无 B 帧，单切片。
- OBS x264 选项：`bframes=0 sliced-threads=0 slices=1 keyint=60 min-keyint=60 scenecut=0`（60 fps）；30 fps 时将两个 keyint 改为 30。

若使用其他硬件编码器，同样需确认 H.264、无 B 帧和关键帧间隔，但此前完整房间验证使用的是 x264。VRC 的硬件/软件解码开关属于播放器行为，服务端无法替代房间实际显示验收。

本地 WSL 与 Windows 共用设备时使用 WSL 的 eth0 地址，例如 `172.23.64.247`。它不是公网地址，也不一定能从其他物理局域网设备访问；WSL 重启后地址可能改变。公网升级前本地测试仍使用独立密码，不复用公网账号的推流和观看凭据。

## 当前 Windows 设备的 TLS 连接兼容转发

2026-10-05 的 OBS 日志出现 `TLS_Connect failed: -0x7280`。同时抓包发现，携带 `v50to.cc` 主机名的完整 TLS ClientHello 之后立即出现与正常连接 TTL 不同的 TCP RST；同一服务器用其他 TLS 主机名或拆分握手记录可以连接。刷新后的 OBS 密码与服务端一致，故障发生在鉴权之前。

`scripts/local-tls-relay.py` 为这台设备提供兼容转发，仅监听 Windows `127.0.0.1:19443`，要求独立的 SOCKS5 密码，并仅允许连接 `chat.v50to.cc` 的推流端口 443 和控制端口 15347。它将首次 TLS 握手记录拆成较小记录，随后原样传输。TLS 仍由 OBS／控制客户端与服务器完成，原有证书校验保留。OBS 继续使用原来的无端口域名 URL；观看服务按已有直连规则处理。

在 Windows PowerShell 中执行 `scripts/install-local-tls-relay.ps1` 安装。默认使用本机 Anaconda Python 和 FLYCLOUD 的 Mihomo 控制接口，可用参数指定其他 Python、配置文件、控制接口或服务器 IP。安装文件、私有配置和滚动日志存放于 `%LOCALAPPDATA%\CialloChat\tls-relay`，并在当前用户登录 Windows 时启动。该方案需要现有 Mihomo／FLYCLOUD 的 TUN 保持开启。

转发进程通过本机控制接口在内存中添加两条域名与端口规则，保留当前代理节点选择及 TUN 状态。客户端重新加载订阅或启动代理后，会在五秒内重新应用规则，不改订阅文件、代理数据库或观看路由。它使用服务器直连链路，不消耗代理节点流量。

执行 `scripts/uninstall-local-tls-relay.ps1` 可停止转发、取消登录启动并恢复代理客户端原配置。服务器 IP 改变时需要重新安装并传入新的 `-UpstreamAddress`。

### 下次无法连接时快速恢复

双击 Windows 桌面的 **CialloChat-Repair**。入口先检查 TUN、规则模式、服务路由、本机转发，以及推流／控制入口的 TLS 连接和证书。全部正常时不重启；转发进程停止或路由缺失时启动进程、重新安装内存路由并再次检查。不会刷新账号密码，也不会开始推流或发送邮件。

如果提示启动 FLYCLOUD、开启 TUN 或切换规则模式，先按提示调整，再运行入口。若本机检查通过但 TLS 入口仍失败，需要继续检查网络或服务器；重复修复不能保证消除新的链路干预。检查通过而 OBS 仍提示鉴权失败时，用 `cialloctl info <user>` 核对地址、密码和账户有效期。

命令行入口为 `scripts/repair-local-tls-relay.ps1`，加 `-CheckOnly` 仅检查。只需停止／重新启动兼容转发时不必重新安装，更无需刷新推流密码。

### 降低对本机兼容转发的依赖

当前 Nginx 的 TLS 主机名转发是官方支持的配置；已有证据指向网络链路对握手的干预，更换反向代理软件无法保证解决相同问题。保留无端口 RTMPS 地址时，可先验证其他主域名的直连，再考虑切换证书和域名；这也需要在实际使用网络中测试。

如果长期优先考虑推流可用性，可评估在现有服务之外增加加密 SRT 备用入口。OBS 和 MediaMTX 支持 SRT，它采用不同于 RTMPS 的握手机制；需要独立 UDP 端口、适配现有账号与推流额度，并接受 URL 中显式端口。当前项目尚未部署这条备用入口，也没有完成其网络与鉴权验证。观看入口及未来网站可继续使用现有 Nginx。
