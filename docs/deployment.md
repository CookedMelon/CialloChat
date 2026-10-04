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

生产模式严格使用 RTMPS 1936，使用匹配 chat.v50to.cc 的受信任域名证书及完整链。RTSP 8554 和 TLS 控制 15347 对外；18554、9997、9000 不开放。TCP 80 用于域名证书签发/续期。保留证书续期和入口连接防护服务。SSH 使用 `ssh root@chat.v50to.cc`；RTMPS 与控制服务不能继续沿用只有 IP SAN 的证书。

升级时保留或一次生成 `runtime/test-video/test.mp4` 并启用 `test_video_enabled=true`；所有公开 URL 和通知均使用域名。

应用配置后由 root 执行 `./streamctl up`，统一安装/启动 auth、mediamtx、watchdog、buffer、control；使用 `systemctl` 而非 `systemctl --user` 查看正式服务器组件。控制证书每次连接重新加载；MediaMTX 证书重载导致媒体重连，但缓冲服务保持运行。

服务端代理使用权限 600 的私有签名密钥，将真实观看来源地址交给鉴权服务，客户端不能伪造来源；缓冲入口不会代填任何账号密码。

## 备份与源码交付

常规 `streamctl backup` 保存账号哈希与设置，完整恢复还必须停服备份 `runtime/leases`（两小时租约、当前明文凭据、邮件队列）和 `runtime/traffic`。不要恢复旧租约让过期密码重新有效。

`./scripts/package.sh` 只导出源码、公开配置样例和文档，排除整个 runtime、依赖、证书和实际 SMTP/控制配置。后端仍支持旧 Docker 无缓冲部署，但该路径不是本轮完整服务最终验收对象。
