# OBS 与播放器接入

通过 `cialloctl info <user>` 获取“OBS推流URL”和“播放器输入URL”。两个 URL 分别包含独立的推流密码和观看密码，不能互换。

OBS 选择自定义直播服务，将完整 OBS推流URL 填入服务器，串流密钥留空。本地完整服务使用 `rtmp://<内网地址>:1935/live/<user>?user=<user>&pass=<推流密码>`；正式服务器使用 `rtmps://chat.v50to.cc:1936/...`。

播放器输入 URL 始终为 `rtsp://<服务地址>:8554/live/<user>?read_key=<观看密码>`，通过 RTSP/TCP 播放。VRC 房间选择直播/AVPro 模式，并允许不可信 URL。VLC 可直接打开该地址，或使用 `vlc --rtsp-tcp <观看URL>`。

播放器若先请求 UDP，缓冲入口返回 461 并保留协商连接，使 VRC 能在同一连接回退到 TCP。

缓冲入口只支持带查询参数的上述观看地址；旧 URI 用户名密码形式仅适用于未经过缓冲的原生兼容入口。

## 已验证的编码建议

- 视频 H.264，2560×1440，按需求选择 30 或 60 fps，CBR 3200 Kbps。
- 音频 AAC，48 kHz，128–160 Kbps。
- x264 `veryfast`，调优 `zerolatency`，关键帧一秒，无 B 帧，单切片。
- OBS x264 选项：`bframes=0 sliced-threads=0 slices=1 keyint=60 min-keyint=60 scenecut=0`（60 fps）；30 fps 时将两个 keyint 改为 30。

若使用其他硬件编码器，同样需确认 H.264、无 B 帧和关键帧间隔，但此前完整房间验证使用的是 x264。VRC 的硬件/软件解码开关属于播放器行为，服务端无法替代房间实际显示验收。

本地 WSL 与 Windows 共用设备时使用 WSL 的 eth0 地址，例如 `172.23.64.247`。它不是公网地址，也不一定能从其他物理局域网设备访问；WSL 重启后地址可能改变。公网升级前本地测试仍使用独立密码，不复用公网账号的推流和观看凭据。
