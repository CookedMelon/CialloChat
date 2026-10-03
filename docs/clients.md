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

房间播放器切换到直播 / AVPro 模式，粘贴 CLI 交付的完整 `vrchat_read_url`：

```text
rtspt://stream.example.com:8554/live/alice?read_key=<已编码的观看密钥>
```

服务端从第一次 RTSP 请求的 URL 校验该路观看密钥。有效 URL 直接返回 SDP，不要求播放器处理 `401 → Authorization: Basic` 挑战。旧的 `legacy_read_url` 仅供支持用户名密码认证的客户端使用；VRChat 应使用新的 URL。已有用户无需重置密钥，用原观看密钥执行 `user credentials alice --kind read` 即可交付新地址。

观看密钥不能用于推流、管理接口或别人的直播；重置、禁用和删除仍撤销旧观看连接。鉴权回调只在连接准入时工作，媒体不经过 Python。缺少 / 错误密钥或鉴权服务不可用时拒绝访问。

个人的“不可信 URL”选项与房间域名许可分别生效。Public / Group Public 实例还需要房间作者允许自有域名；切换 URL 认证方式不会绕过 VRChat 地址规则。Quest 的协议兼容性与房间内画面、声音、延迟仍需客户端验证，不把服务端无挑战握手测试当成 VRChat 全部验收完成。

## FFmpeg 发布

在受信任客户端读取完整推流 URL：

```bash
read -rs -p '推流 URL: ' CIALLOCHAT_PUBLISH_URL; printf '\n'
ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -re -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -c:v libx264 -preset ultrafast -tune zerolatency -bf 0 \
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
