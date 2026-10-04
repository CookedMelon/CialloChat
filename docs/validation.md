# 当前验证状态

收尾部署日期：2026-10-04；累计推流计时修正与远程上线日期：2026-10-05。本轮使用 Ubuntu 26.04 WSL、Python 3.14、锁定 MediaMTX 1.21.1。本地完整服务由五个用户级 systemd 单元运行。此前实验脚本、测试视频、旧报告和临时播放入口已删除；保留针对现有实现的回归检查。

## 已完成

- 基线全量 105 项单元测试：102 项通过，3 项可选测试跳过；随后新增的 TCP 回退和素材暂缺两项随相关模块通过，现有测试共 107 项。覆盖账号与 URL 角色隔离、配置事务/恢复、期限、简洁邮件与域名迁移、控制协议、码率监控、流量统计、缓冲计时、代理签名及测试频道持久配额。
- `validate-buffer.py` 使用真实 MediaMTX 和 2560×1440 / 60 fps / 3200 Kbps H.264＋160 Kbps AAC。正确 URL 第一次 DESCRIBE 成功，音视频经一秒缓冲后实际解码；错误/跨用户观看密码返回 401。
- 观看密码刷新终止旧缓冲观看连接，原推流保持；旧观看密码拒绝，新密码可用。
- 临时测试租约到期终止真实发布连接，媒体/缓冲重启后旧推流密码仍拒绝。业务账号期限保持两小时。
- `validate-control.py` 使用真实 TLS 控制连接和真实 MediaMTX 热更新，验证 add/list/del、push/pull/all 刷新、两小时重置、观看刷新保留推流期限，以及四次通知捕获。测试邮件未发往真实邮箱。
- 非法管理密码收到零应用层响应数据。
- 完整本地用户 `cc` 创建邮件已由实际 SMTP 接受，正文使用 OBS推流URL、播放器输入URL 和“推流密钥可用时长2小时”。
- 实际已安装的完整 systemd 实例接收动态 2K/60 fps、3200 Kbps 视频＋160 Kbps 音频，经正式缓冲入口成功解码 300 帧且无音视频解码错误，正常推流未被踢掉。临时验证账号随后删除。
- 本地管理 API、鉴权和内部 RTSP 仅绑定 127.0.0.1；临时 8555–8558 入口已关闭。
- 唯一测试视频实测十分钟、36000 帧、2560×1440 / 60 fps、H.264 1600247 bit/s、600 个间隔一秒的关键帧及双声道 AAC。画面与运动文字已截图核对。
- `validate-test-channel.py` 经真实媒体服务成功解码音视频，验证同 IP 重复连接拒绝、重连保留期限、到期关闭媒体与文件发布进程、重启保留冷却，以及普通鉴权直播不受影响。每日六十分钟与跨日累计由可控时钟测试验证。
- 实际本地 systemd 测试入口 `rtsp://172.23.64.247:8554/test` 成功解码 301 帧，无解码错误；观看结束后测试媒体进程退出，`cc` 推流密码仍未开始使用。
- VRC 首次请求 UDP，收到 461 后需要在同一连接重试 TCP；代理已保留该协商连接，实际日志确认双轨 TCP SETUP、PLAY 及持续音视频发送，用户已确认画面和声音恢复。新增协商回归连同缓冲模块共 11 项通过。

当前回归结果写入私有 `runtime/reports/buffer-validation.json`、`control-native-validation.json`、`local-service-validation.json`、`test-channel-validation.json`、`local-test-channel-validation.json`、`test-video-asset-validation.json`；运行缓冲指标见 `rtsp-buffer.jsonl`。这些文件不进入源码包。

## 累计推流计时修正

此前实现把两小时解释为首次使用或手动刷新后的墙钟窗口，停推仍消耗时间并触发续期通知，与累计使用时长的要求不符。2026-10-05 已改为累计实际发布时间并部署到 chat.v50to.cc，停止推流暂停；剩余十分钟且仍在发布时触发续期，停推也暂停失败通知的重试。观看不计入，重连和重启不清空已用额度。旧数据库升级保留切换时剩余额度，不复活已过期密码。

- 全量 115 项测试：112 项通过，3 项可选测试跳过。覆盖离线多日、短断线、同时发布、单调时钟、服务器重启、剩余十分钟通知、失败重试暂停、手动刷新及旧数据库迁移。
- `validate-publisher-lifetime.py` 实际本地媒体连接验证：停推额度不减少、重连继续使用、额度耗尽终止发布和观看、重启拒绝旧密码、已交付新密码获得完整额度。
- `validate-buffer.py` 实际 2K/60 fps 音视频解码及鉴权、观看刷新隔离、到期断开和重启拒绝全部通过，一秒缓冲保持原策略。

- 远程隔离目录计时及控制回归共 33 项：32 项通过，1 项可选测试跳过。只传输约 149 KB 源码，没有上传视频或工具。
- 远程累计计时已上线，鉴权、watchdog 和控制服务完成重启；MediaMTX 和一秒缓冲进程 PID 不变。账号、密码代、邮件配置、证书和接入域名逐项校验保留。
- 公网 RTMPS 验证证书链、RTSP 解码 181 帧且无错误。临时账号实际推流计入约 4.999 秒，停推六秒前后剩余秒数完全相同，未触发离线续期通知。合法管理请求成功，非法管理密码收到零应用响应；临时账号已删除。

报告位于私有 `runtime/reports/cumulative-usage-unit-tests.txt`、`cumulative-publisher-lifetime.json`、`buffer-validation.json`、`public-cumulative-usage-validation.json`。远程部署报告为 `/opt/ciallochat/runtime/reports/cumulative-usage-deployment.json`，恢复备份为 `/opt/ciallochat-backups/cumulative-usage-1791141925579831250`。本轮验证没有发送真实邮件。

## 最终验收边界

用户此前已经确认一秒缓冲实验版可稳定运行；此次将该机制接入完整多用户鉴权和管理服务，仍需用户用最终 URL 验证 OBS＋VRChat 长时间播放、延迟接受度与收件箱通知。服务端指标无法代替房间实际显示判断。

本地最终验收已通过，用户已授权并完成部署到 chat.v50to.cc。正式服务器五个系统级组件全部运行，公开配置、管理客户端和通知 URL 使用域名；域名证书链与证书续期 dry-run 通过。原账号文件、SMTP 与管理密码逐字节校验保留，租约数据库保留；临时验证账号已删除。

公网 RTMPS 验证启用证书链检查，接入实际服务器；RTSP 音视频经一秒缓冲解码 301 帧且无错误。非法管理密码收到零应用响应。本地默认 cialloctl 已切换到正式域名。验证媒体进程清除其代理环境变量以直接连接目标服务。

测试视频在服务器生成一次，无大素材上传；生成期间先完成正常直播部署，素材校验后原子安装，测试入口自动可用，业务服务没有因此重启。远程恢复备份位于 `/opt/ciallochat-backups`。真实 VRC 的公网长时间画面表现仍以用户使用为准。

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python scripts/validate-buffer.py --mediamtx runtime/bin/mediamtx
PYTHONPATH=src .venv/bin/python scripts/validate-control.py
./streamctl doctor
./streamctl status
```

真实媒体验证需要 FFmpeg/FFprobe；源码目录保留 `smoke-test.sh` 和 `validate-publisher-lifetime.py` 用于更全面的权限及期限回归。旧 Docker 后端未在本轮重新验收，不能将旧无缓冲延迟数字作为新方案的总延迟。
