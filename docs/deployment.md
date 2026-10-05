# 完整服务部署

推荐使用原生 systemd，不上传 Docker 镜像或测试视频。MediaMTX 固定为 1.21.1，Python 依赖锁定在 `requirements.lock`。本地测试完成前不更新远程实例。

## 本地 WSL / Ubuntu

```bash
./setup.sh --backend systemd --mode local
# config/smtp.json 填 SMTP 授权码和 recipients，接受 smtp_host/smtp_port/smtp_ssl
# config/control-server.json 填独立随机管理密码
chmod 600 config/smtp.json config/control-server.json
.venv/bin/python scripts/build-test-video.py
.venv/bin/python scripts/deploy-local.py --test-video
source ~/.bashrc
cialloctl info cc
```

`deploy-local.py` 完成本地证书、私有管理配置、SMTP 安装、账号创建与邮件、五个用户级 systemd 服务、控制脚本软链接和 PATH 配置。`--test-video` 启用已经生成的测试频道，创建通知增加测试 URL。它只部署本地，不连接远程。账号首次创建后重复部署不更换密码或清空租约；已有其他后端实例需先停服并迁移。

本地控制证书由本地 CA 签发，客户端使用生成的 `ca_file` 验证，不安装到系统信任库。证书一年有效；内网地址改变时应先停服并备份旧证书，再重新生成。RTMP 本地输入保持未加密，适合内网最终验证。

| 端口 | 用途 | 监听范围 |
| --- | --- | --- |
| 1935 | 本地 RTMP 推流 | 指定内网地址 |
| 8554 | 带用户鉴权、一秒缓冲的 RTSP/TCP | 指定内网地址 |
| 15347 | TLS 控制命令 | 指定内网地址 |
| 18554 | 内部原生 RTSP | 127.0.0.1 |
| 9997 | MediaMTX 管理 API | 127.0.0.1 |
| 9000 | 推流/观看鉴权 | 127.0.0.1 |

```bash
./streamctl status
./streamctl doctor
./streamctl down
./streamctl up
systemctl --user status ciallochat-{auth,mediamtx,watchdog,buffer,control}
```

`up` 管理所有组件，`down` 停止并禁用自动启动。用户级服务随用户 systemd 会话恢复；WSL 本身是否启动由 Windows 管理。

## 正式服务器 chat.v50to.cc

在本地最终验收通过后进行更新。复用现有 `/opt/ciallochat`、原生 MediaMTX、Python 虚拟环境和有效证书，增量上传源码。更新前停服保存账号、租约/凭据库、流量库、SMTP、控制配置、证书及生成配置；升级不得把本地账号覆盖到公网。

原生设置可参考 [完整设置样例](../config/native-settings.example.json)：`service_backend=systemd`、`systemd_scope=system`、`mode=production`、`rtsp_buffer_ms=1000`、`rtsp_internal_port=18554`、`control_enabled=true`。保留每路 4000 Kbps 与两小时租约。安装实际控制配置到权限 600 的 `runtime/control-server.json`，SMTP 到 `runtime/notifications/smtp.json`，设置 `native_runtime` 为该实例 runtime 的绝对路径。

正式实例使用 Nginx 媒体反向代理：`rtmps://chat.v50to.cc` 通过公网 TCP 443 转到本机 1936；`rtsp://watch.v50to.cc` 通过公网 TCP 554 转到本机 8554，再由一秒缓冲入口连接内部 18554。1936、8554、18554、9997、9000 仅本机监听；TLS 控制 15347 保持独立。RTMPS 使用匹配 chat.v50to.cc 的受信任证书及完整链。TCP 80 留给现有 standalone Certbot，不启用 Nginx 默认网站。SSH 使用 `ssh root@chat.v50to.cc`。

`reverse_proxy_enabled=true` 需要 production/systemd 和 RTSP 缓冲。`hostname` 设置推流域名，`read_hostname` 设置观看域名；`public_rtmps_port` 与 `public_rtsp_port` 分别设置公开入口，默认 443 和 554，生成 URL 时省略这两个默认端口。关闭反向代理时继续使用原有地址生成和监听方式，不影响本地部署。

安装发行版的 `nginx` 和 `libnginx-mod-stream` 后，以 root 执行 `PYTHONPATH=src .venv/bin/python scripts/install-media-proxy.py`。安装脚本在 `/opt/ciallochat-backups/media-proxy-*` 备份配置、校验 Nginx 和 MediaMTX，再停服切换；失败自动恢复配置。它保留账号、密钥、租约、流量库、SMTP 和测试视频。Nginx 配置为顶层 `stream` 中包含 [媒体路由](../config/nginx-media.conf)，不配置网站。443 按 SNI 转发，仅接受 chat.v50to.cc；未知域名不转发到媒体服务。554 转到现有观看缓冲入口，由它校验 watch.v50to.cc。新增 TLS 服务可增加 SNI 路由；普通 RTSP 的多个域名不能直接用 Nginx stream 区分。

Nginx 向后端发送 PROXY v1；MediaMTX 仅信任 127.0.0.1/32，观看缓冲入口仅从本机接收代理头。连接限制、观看签名、测试频道配额与日志均使用原客户端 IP。媒体连接防护规则随 `up` 更新为公开端口，避免旧规则把后端连接全部按代理 IP 限制。证书重载的内部 TLS 探测也发送代理头。Nginx 日志仅记录地址、字节数和会话耗时，不记录含密钥的媒体 URL。

升级时保留或一次生成 `runtime/test-video/test.mp4` 并启用 `test_video_enabled=true`；所有公开 URL 和通知均使用域名。

应用配置后由 root 执行 `./streamctl up`，统一安装/启动 auth、mediamtx、watchdog、buffer、control；使用 `systemctl` 而非 `systemctl --user` 查看正式服务器组件。控制证书每次连接重新加载；MediaMTX 证书重载导致媒体重连，但缓冲服务保持运行。

服务端代理使用权限 600 的私有签名密钥，将真实观看来源地址交给鉴权服务，客户端不能伪造来源；缓冲入口不会代填任何账号密码。

## 观看链路拥塞恢复

RTSP 缓冲入口为每个观众保留独立队列。默认 `rtsp_max_buffer_bytes=8388608`（8 MiB），
`rtsp_write_timeout_seconds=10`（连续无写入进展），`rtsp_write_max_wait_seconds=30`（单次写入等待上限）。
这些设置位于 `runtime/settings.json`；未填写时采用默认值，也可使用缓冲进程的
`--max-buffer-bytes`、`--write-timeout`、`--write-max-wait` 覆盖。队列可配置为 64 KiB–16 MiB，
停滞超时为 0.1–30 秒，总等待上限不得小于停滞超时且不超过 60 秒。

短时拥塞时，入口保留完整音视频包，传输恢复后按原来的播放时间追赶，不追加固定延迟。
判断进展依据是 Python 传输缓冲中的待写字节减少；经过 Nginx 时反映的是本机代理连接，
不能当作观众已经收到数据的确认。接收保持活动或 RTSP keepalive 不会重置停滞计时。
持续低于直播码率时，队列达到上限、连续没有写入进展或单次等待到达上限仍会关闭该观众连接，
避免无限内存和无限延迟。实际追赶速度还取决于网络和播放器。

`runtime/reports/rtsp-buffer.jsonl` 的 `write_blocked`、`write_recovered`、`write_timeout` 事件记录
方向、等待耗时与积压，超时区分 `no_progress` 和 `total_wait`；`closed` 记录出错任务。
周期指标包含 `write_blocked_ms`、`pending_write_bytes` 和累计超时次数，最长写入耗时也包含失败写入。
这些日志不包含观看密钥。此策略用于减少短时网络波动导致的断线，不改变 VRC 自身的重连等待，
也不能修复运营商、路由器或客户端网络的持续丢包。

## 备份与源码交付

常规 `streamctl backup` 保存账号哈希与设置，完整恢复还必须停服备份 `runtime/leases`（两小时租约、当前明文凭据、邮件队列）和 `runtime/traffic`。不要恢复旧租约让过期密码重新有效。

`./scripts/package.sh` 只导出源码、公开配置样例和文档，排除整个 runtime、依赖、证书和实际 SMTP/控制配置。后端仍支持旧 Docker 无缓冲部署，但该路径不是本轮完整服务最终验收对象。
