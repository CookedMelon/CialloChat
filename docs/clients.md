# 客户端接入

服务直接转发 H.264 视频和 AAC 音频。先创建账号并保存 CLI 交付的 `publish_url` 和 `read_url`，服务启动后再使用客户端。Windows 上的 OBS、播放器和 Docker Desktop 设置由使用者完成。

## 从创建账号到播放

在 WSL 或服务器的项目目录执行：

```bash
umask 077
./streamctl user add alice > runtime/alice-credentials.json
./streamctl up --mode local
cat runtime/alice-credentials.json
```

如果账号已存在，使用已保存的凭据，或分别重置需要交付的密钥；不要重复创建。`user credentials` 必须输入现有密钥，不能找回遗忘的明文。

OBS → 设置 → 直播 → 服务选择“自定义”：

- **服务器**：完整复制 `publish_url`，包括路径和 `?user=...&pass=...`。
- **串流密钥**：留空。
- 视频编码选择 H.264，音频选择 AAC，然后开始直播。

本地 URL 形如 `rtmp://127.0.0.1:1935/live/alice?user=alice&pass=<已编码的推流密钥>`。正式 URL 使用 `rtmps://stream.example.com:1936/...`，需要客户端信任服务器证书及正确主机名。CLI 使用 runtime/settings.json 中的地址和端口生成实际 URL；当前开发实例的 RTSP 端口是 **18554**，新安装默认是 8554。

播放器打开完整 `read_url`，使用 RTSP/TCP。地址形如：

```text
rtsp://127.0.0.1:8554/live/alice?read_key=<已编码的观看密钥>
```

实际值直接复制凭据文件，不要用推流密钥替代。VLC 命令示例：

```bash
read -rs -p '观看 URL: ' CIALLOCHAT_READ_URL; printf '\n'
vlc --rtsp-tcp "$CIALLOCHAT_READ_URL"
unset CIALLOCHAT_READ_URL
```

没有观看密钥、只有用户名、错误密钥或跨路凭据均被拒绝。共享 `read_url` 等于共享该路观看权限。RTSP 在本版仍未加密；观看授权与链路加密是两个要求。

## VRChat 直播接入

2K 屏幕建议使用 2560×1440、H.264、60 fps、CBR 34000 Kbps，AAC 192 Kbps。服务端每路默认 45000 Kbps，持续超限会断开发布连接；完整 OBS 参数和断连条件见 [码率配置](bitrate.md)。

H.264 编码建议使用**每帧一个 slice（编码切片）**作为兼容性配置。本轮早期故障中，PotPlayer 完整播放，而 VRChat 只显示顶部一条且冻结；码流每帧有 16 个 slice，第一片覆盖顶部 96 像素。早期一次短采样未见服务端 RTP 丢弃，但后续完整会话日志出现持续丢弃；不能把短时间没有新增丢弃等同于整段观看正常。实际 OBS 改为单切片后，用户仍报告黑屏或结束播放，因此单切片不是已确认的完整修复方案，详见 [验证报告](validation.md)。

OBS 使用 x264 时，在“设置 → 输出 → 输出模式：高级 → 直播 → x264 选项”填入以下参数（各项以空格分隔）：

```text
sliced-threads=0 slices=1 slice-max-size=0 slice-max-mbs=0 bframes=0 rc-lookahead=0 sync-lookahead=0
```

可以保留 `zerolatency` 调优，但必须显式覆盖它默认开启的 `sliced-threads`。停止推流、录制和回放缓存后修改直播编码器的选项，保存后完全退出并重新打开 OBS，再开始推流，以确保重新创建编码器；在房间重新加载观看 URL，使播放器获取新的 SPS/PPS。保持 1440p、60 fps 和原码率，先只验证切片变化；不要同时改 Profile、分辨率和缓存。上述参数只适用于 x264，硬件编码器需要其对应的 slice 设置。选项文本不替代实际码流验证，本轮重新启动后已实测 121 帧均为一个 slice。

`slices=1` 仍允许一个编码切片拆成多个 RTP 网络包，不是把整帧塞进一个大包。服务器直接转发压缩视频，不能通过扩大网络包或更改 SDP 将 16 个编码切片无损变成一个；重新编码会带来额外处理成本和延迟。关闭切片线程后，x264 使用帧线程，编码负载和编码延迟需要重新确认，不能保证与原配置相同。

参数行为依据 [x264 的 zerolatency 实现](https://github.com/mirror/x264/blob/master/common/base.c) 和 [OBS 的 x264 自定义参数实现](https://github.com/obsproject/obs-studio/blob/master/plugins/obs-x264/obs-x264.c)。这里不将普通播放器解码成功等同于 VRChat 兼容验收完成。

房间播放器切换到直播 / AVPro 模式，粘贴 CLI 交付的完整 `vrchat_read_url`：

```text
rtspt://stream.example.com:8554/live/alice?read_key=<已编码的观看密钥>
```

服务端从第一次 RTSP 请求的 URL 校验该路观看密钥。有效 URL 直接返回 SDP，不要求播放器处理 `401 → Authorization: Basic` 挑战。旧的 `legacy_read_url` 仅供支持用户名密码认证的客户端使用；VRChat 应使用新的 URL。已有用户无需重置密钥，用原观看密钥执行 `user credentials alice --kind read` 即可交付新地址。

观看密钥不能用于推流、管理接口或别人的直播；重置、禁用和删除仍撤销旧观看连接。鉴权回调只在连接准入时工作，媒体不经过 Python。缺少 / 错误密钥或鉴权服务不可用时拒绝访问。

个人的“不可信 URL”选项与房间域名许可分别生效。Public / Group Public 实例还需要房间作者允许自有域名；切换 URL 认证方式不会绕过 VRChat 地址规则。Quest 的协议兼容性与房间内画面、声音、延迟仍需客户端验证，不把服务端无挑战握手测试当成 VRChat 全部验收完成。

若低码率合成流能播放而自己的直播黑屏，应同时检查观看线路。本轮 0.4 Mbps 视频的 1440p/60 fps 单切片测试流在房间中显示运动画面；实际单切片 OBS 流在另一观看会话中仍出现持续发送队列溢出。服务端每路 45000 Kbps 是发布准入上限，不能保证任一观看线路的吞吐；扩大队列也不能解决持续接收能力不足。

Windows 上 AVPro 的原生 RTSP 支持依赖 Media Foundation；Microsoft 文档规定，在未被应用或用户策略覆盖时，RTSP 默认绕过应用层代理，HTTP 默认使用浏览器代理设置。因此只开启系统 HTTP 代理，不能证明 RTSP 视频经过代理。TUN/VPN 路由和游戏加速器的底层连接重定向是另一层机制，仍可能接管原生 TCP 连接；按应用匹配且未排除视频目标的规则可能同时捕获游戏和视频连接。给梨与 FLYCLOUD 的具体捕获规则尚无本轮官方资料或客户端日志证明，不假定某一种客户端菜单、内核或默认行为。依据：[AVPro RTSP 支持](https://www.renderheads.com/content/docs/AVProVideo/articles/feature-streaming.html)、[Media Foundation 代理默认配置](https://learn.microsoft.com/en-us/windows/win32/medfound/proxy-support-for-network-sources)、[Windows 连接重定向](https://learn.microsoft.com/en-us/windows-hardware/drivers/network/using-bind-or-connect-redirection)。

本项目当前用户必须保持代理和加速器开启，并已确认 FLYCLOUD 开启 TUN/VPN/虚拟网卡模式；该客户端没有可供用户查看的连接日志。排查优先使用服务端已有的连接来源地址、会话 ID、请求 User-Agent、观看路径、握手状态、TCP 确认字节增量、发送队列和 RTP 丢弃计数，不要求用户查找不存在的日志。来源地址与测试时间可区分本轮本地播放器和房间播放器对应的会话，但来源 IP 本身不是客户端程序身份；User-Agent 也只能作为辅助证据。不同公网出口证明出口不同，不能显示客户端内部命中了哪个软件或规则。TUN 开启也不能单凭这一事实认定某个软件正在转发这条流。后续若需要客户端分流，按其实际能力只调整服务端 IP / 视频端口的线路，保留游戏及其他应用的规则；不能保证在一层添加豁免就能绕过另一层。配置后的新会话需再次核验。所有 Windows 查询和配置由使用者完成，完整房间播放仍待验收。

## FFmpeg 发布

在受信任客户端读取完整推流 URL：

```bash
read -rs -p '推流 URL: ' CIALLOCHAT_PUBLISH_URL; printf '\n'
ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -re -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -c:v libx264 -preset ultrafast -tune zerolatency -bf 0 \
  -x264-params sliced-threads=0:slices=1:slice-max-size=0:slice-max-mbs=0 \
  -pix_fmt yuv420p -b:v 2M -g 30 -c:a aac -b:a 128k \
  -f flv "$CIALLOCHAT_PUBLISH_URL"
unset CIALLOCHAT_PUBLISH_URL
```

生产 RTMPS 添加 `-tls_verify 1`；测试 CA 添加 `-ca_file /测试目录/ca.crt`。参数可能出现在本机进程列表，应在受信任客户端运行。测试 CA 不自动导入 Windows 或系统信任库。

## 连接行为与验证范围

同路可由多个观看者同时读取。第二个发布者被拒绝。停止发布会结束读取；重新发布后重新打开观看地址。禁用或删除账号撤销双方；重置观看密钥断开旧读者而保持发布者在线，新观看地址可立即重新接入。

真实音视频、双密钥权限、撤销、TLS 和重连由隔离自动测试覆盖，见 [验证报告](validation.md)。用户已确认 Windows OBS 发布和播放器画面正常，未明确确认声音、停止及重连的全部手工结果，不将其标为已执行。

项目延迟验收只测推流协议转发，不再要求切换播放器、调整缓存或反复秒表测试。测量方法与结果见 [服务端转发延迟](latency.md)。

接入方式参考：[OBS 发布](https://mediamtx.org/docs/publish/obs-studio)、[MediaMTX 认证](https://mediamtx.org/docs/features/authentication)。
