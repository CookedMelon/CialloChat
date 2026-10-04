# CialloChat

面向 OBS 与 VRChat 的单机直播服务。每个用户有独立的推流密码、长期观看密码和 `live/<username>` 路径；创建、刷新密码和临近到期时通过邮件通知。每个推流密码可累计推流两小时，停推暂停计时，断线和重启不重置已用额度；额度耗尽会终止正在推流的连接。

当前推荐原生 systemd 部署，MediaMTX 锁定 **1.21.1**。OBS 输入 H.264＋AAC，服务不转码、不录制；公开 RTSP/TCP 入口执行鉴权并提供已验证的 **1000 ms 固定缓冲与时间戳匀速发送**。每位观看者有独立的 2 MiB 队列，持续慢连接会被关闭，避免无限积压。默认推流上限 4000 Kbps，适合视频 3200 Kbps＋音频 128–160 Kbps。

本地完整服务准备：

```bash
./setup.sh --backend systemd --mode local
# 按 example 配置填写 config/smtp.json、config/control-server.json，权限 600
.venv/bin/python scripts/build-test-video.py
.venv/bin/python scripts/deploy-local.py --test-video
source ~/.bashrc
cialloctl help
cialloctl list
cialloctl info cc
```

`deploy-local.py` 使用 eth0 内网地址、RTMP 1935、带鉴权的缓冲 RTSP 8554、TLS 控制 15347。首次创建 `cc` 并向 SMTP 配置中该用户的邮箱发信；重复执行保留账号与密码。可用 `--host`、`--user`、`--email` 指定本地接入参数。WSL 地址变化时先停止服务，重新配置本地地址与证书。

测试频道直接打开 `rtsp://<服务地址>:8554/test`，无需密码。提供有声 2K/60 fps、1600 Kbps 视频、运动弹幕和断连倒计时；每 IP 十分钟窗口、五分钟冷却、每日六十分钟。首次生成一次素材即可，已有素材无需重新生成。新建账户邮件包含测试频道 URL。

管理账号推荐统一使用 `cialloctl`，密码自动从权限 600 的 `config/control-client.json` 读取：

```bash
cialloctl add alice alice@example.com
cialloctl refresh alice push
cialloctl refresh alice pull
cialloctl refresh alice all
cialloctl del alice
cialloctl traffic
```

`list` 表格显示邮箱、当前有效密码、剩余时长、观看密码、最近成功推流登录及正在推流状态；`info` 给出完整 OBS 推流 URL 和播放器输入 URL。OBS 串流密钥留空，VRChat 使用直播/AVPro 模式。

服务管理：

```bash
./streamctl status
./streamctl doctor
./streamctl logs --tail 50
./streamctl down
./streamctl up
```

完整本地部署包含 auth、mediamtx、watchdog、buffer、control 五个用户级 systemd 服务。正式服务器使用系统级 systemd 与受信任 RTMPS 证书；管理 API、鉴权和内部 RTSP 均只监听本机。旧 Docker 后端保留兼容，不提供本轮验收的 RTSP 缓冲入口。

- [常驻测试频道](docs/test-channel.md)
- [部署与端口](docs/deployment.md)
- [OBS 与 VRChat 配置](docs/clients.md)
- [远程控制与邮件模板](docs/remote-control.md)
- [缓冲机制与延迟边界](docs/latency.md)
- [码率预算](docs/bitrate.md)
- [两小时密码与 SMTP](docs/publisher-lifetime.md)
- [账号运维与备份](docs/accounts.md)
- [当前验证与待验收事项](docs/validation.md)

开发检查：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh streamctl scripts/*.sh control-scripts/*.sh
PYTHONPATH=src .venv/bin/python scripts/validate-buffer.py --mediamtx runtime/bin/mediamtx
PYTHONPATH=src .venv/bin/python scripts/validate-control.py
```

媒体验证需要 FFmpeg/FFprobe；单元测试使用 unittest。Ubuntu 24.04/26.04、Python 3.12–3.14，依赖锁见 `requirements.lock`。普通直播无需 FFmpeg；测试频道用 FFmpeg 复制预编码视频，不实时转码。`cialloctl traffic` 绘图额外使用客户端 Pillow。

`./scripts/package.sh` 生成小型源码包与 SHA256 清单，排除 runtime、工具缓存、虚拟环境、账号、SMTP、控制密码和证书。只提交配置样例。远程升级复用服务器原生程序和依赖，避免上传镜像或测试视频。
