# 验证报告

本报告对应独立推流密钥、观看密钥的 schema 2 实现。服务器功能、权限撤销、部署和协议转发分别验证；Windows 客户端由用户操作。日期：2026-10-03。

## 新版功能与权限

URL 观看密钥与码率上限版本：管理及 CLI 的 **45 项测试全部通过**（本机 Python 3.14 与服务器 Python 3.12），真实 Docker Compose 的 **43 项音视频测试全部通过**，辅助 native 的 **42 项全部通过**。新增覆盖第一次 DESCRIBE 无 Authorization 直接成功、错误 URL 密钥、上限配置、窗口峰值、在线调整，以及 RTMP/RTMPS/RTSP 发布持续超限自动断连。其余角色隔离、撤销、TLS、重启和备份恢复仍在完整真实码流测试中执行。

| 验收内容 | 实际结果与证据 |
| --- | --- |
| 双密钥隔离 | 匿名、只有用户名、错误观看密钥、推流密钥用于观看、跨账号观看均返回认证拒绝；观看凭据不能发布或访问管理 API |
| 观看密钥重置 | 已连接读者退出、旧密钥拒绝、新密钥可解码；原发布者进程和 source ID 保持不变 |
| 推流管理 | 匿名、错误密钥、未知/跨账号路径和重复发布拒绝；禁用、启用、推流重置、删除实际生效 |
| 音视频与隔离 | Alice/Bob 两路 H.264 + AAC 可解码，红/蓝像素区分；同路两读者同时在线并解码 |
| 特殊字符 | 推流查询参数及观看 URL 百分号编码通过；真实 Bob 凭据含特殊字符，观看密钥含 Unicode |
| 旧数据安全迁移 | 推流哈希原样保留，观看密钥以 600 文件交付；中断迁移恢复禁止媒体访问的配置；旧备份必须显式迁移，不能恢复匿名 read |
| 事务与并发 | 批量原子性、并发写入、文件锁、失败回滚、待撤销恢复；等待输入或跟随日志不阻止另一管理员撤销 |
| 持久化与恢复 | 服务重启后账号相同；production 备份恢复后 RTMPS 发布和 RTSP 音视频可用；默认备份不含证书或媒体明文密钥 |
| TLS | 生产无明文 RTMP 监听；测试 CA 验证真实 RTMPS，错误信任和主机名被拒绝；更新证书后实际 TLS DER 与新文件一致 |
| 证书异常下撤销 | 续期文件缺失仍可撤销现有直播；生产启动继续拒绝缺失证书 |
| 容器部署 | 实际锁定镜像、UID/GID、回环端口、只读挂载、只读根文件系统、restart 策略及日志轮转参数通过 |
| 无 Basic 挑战的观看 URL | 正确 URL 第一次 DESCRIBE 返回 200，无 WWW-Authenticate；H.264 和 AAC 随后可解码 |
| 持续超限自动断连 | RTMP、RTMPS、RTSP 均验证；低于上限者可解码，其他发布者与账号密钥保留 |
| 私有准入与监控 | auth 仅挂载哈希权限配置，watchdog 仅挂载专用监控配置，两者只在内部网络可用；实际健康状态通过 |
| 端口检查 | 真正监听冲突被拒绝；已关闭连接的 TIME_WAIT 不误阻止重启 |

直接证据：[Python 3.14 管理测试](evidence/url-key-cap-unit-python314.txt)、[Python 3.12 管理测试](evidence/url-key-cap-unit-python312.txt)、[新版 Compose 音视频](evidence/url-key-cap-compose-smoke.json)、[辅助 native](evidence/url-key-cap-native-smoke.json)。拒绝用例要求明确服务器状态码或拒绝日志与路径状态，不把任意客户端失败视为权限通过。

另通过真实 1440p/60 fps 高码率验收：预编码 H.264 34000 Kbps + AAC 192 Kbps，通过验证 TLS 的 RTMPS 发布、无 Basic 挑战的 URL 观看，经 RTSP/TCP 实际解码音视频，持续在线且未触发默认 45000 Kbps 上限。实际接收码率见 [高码率报告](evidence/url-key-cap-profile.json)。合成画面证明传输与配置兼容，不代表所有真实桌面内容的主观画质。

## 环境与部署

开发实例为 Ubuntu 26.04 WSL amd64，系统 Python 3.14.4，FFmpeg 8.0.1 使用项目内提取的依赖。最新 Compose 验证在 Ubuntu 24.04 部署服务器上执行，Docker 29.8.2、Compose 5.6.0、Python 3.12.3、FFmpeg 6.1。本轮没有可用的本机 Docker，未调整 Windows、Docker Desktop、WSL 集成、用户组或 socket 权限。媒体镜像固定为 MediaMTX 1.21.1，digest 见 [版本锁](../config/version.json)。

Ubuntu 24.04 辅助容器使用 Python 3.12.3，在普通 UID 1000 下通过双密钥管理测试、锁定依赖安装、Bash 语法及 Python 编译。它证明管理代码兼容性，不能替代原生 systemd 与重启验收。报告见 [容器辅助验证](evidence/ubuntu24-container.json)。

URL/码率监控改动之前，原生 Ubuntu 24.04 VM 的安装与重启验收已通过，Python 3.12.3 的 32 项管理测试及重启后的 35 项真实媒体验证全部通过。结果见 [VM 报告](evidence/ubuntu24-vm.json) 和 [VM 真实媒体](evidence/ubuntu24-vm-smoke.json)。该环境在隔离容器中运行 QEMU/KVM，guest 的 PID 1 为 systemd；安装、官方 noble Docker 仓库、重复 setup 保留数据、daemon 自启动、guest 重启后的容器/API恢复及新版真实媒体权限均单独检查。用重启前后不同 boot ID 证明进行了 guest 系统重启，未重启用户的 Windows 或 WSL。实际架构为 amd64；arm64 配置支持尚无本轮运行证据。

本轮 VM 内的安装、重启和媒体检查均完成，报告已取回；宿主验收脚本因执行期间文件更新而在收尾退出异常，临时容器已清理。脚本已改为使用启动时的内容快照，模拟运行中替换文件的隔离回归检查通过，Bash 语法检查通过。该修复未重新执行已经通过的 guest 安装和媒体检查。

## 延迟验收

本轮追加的 URL 准入和码率监控没有进入媒体转发路径；未重新测量下面的协议延迟基线，旧数值不作为 34 Mbps 新负载的延迟保证。云端辅助实时编码未达到 60 fps，不能把其低发送速率归因于服务器转发；生产发布者在外部编码，本轮高码率验收使用预编码素材复制发送。

不对播放器进行持续调参。服务器协议测试计时从压缩 H.264 帧写入 RTMP 前开始，到 RTSP/TCP 收齐同一帧结束，使用 RTP 时间戳和 VCL NAL SHA256 双重匹配，不包含编码、解码或显示。

1440p/约 59.45 fps、素材约 6.36 Mbps、AAC 同时传输，单读者、四读者、四正常读者加一慢读者分别采样 60 秒。最新探针检查接收速率、内容匹配、丢帧、重复和乱序；具体统计及边界见 [延迟报告](latency.md)。队列容量 512 和写超时 10 秒不是固定等待时长。媒体没有经过 Python，没有新增转码、切片、录制或固定时间聚合。

用户曾确认 Windows OBS 发布和播放器画面正常，并报告 VLC 约 0.8 秒、PotPlayer 超过 2 秒、FFplay 约 1.7 秒。未证明这些画面延迟的完整归属，不把服务器协议统计当作用户屏幕延迟已经消失。手工声音、停止、重连及正式公网证书下 OBS 的全部结果未得到明确确认；不阻止修订范围内的服务器交付，也不标为已验证。

## 可复现命令

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh streamctl scripts/*.sh
./setup.sh --check --with-validation
./scripts/smoke-test.sh
./scripts/smoke-test.sh --profile-only --profile-input /路径/已编码的1440p60-34Mbps-H264-AAC.mp4 --report runtime/reports/profile.json
./scripts/validate-ubuntu24.sh
./scripts/validate-ubuntu24-vm.sh
./scripts/measure-forwarding.sh --seconds 60
./scripts/measure-forwarding.sh --seconds 60 --readers 4
./scripts/measure-forwarding.sh --seconds 60 --readers 4 --slow-reader
```

媒体测试默认创建临时目录、独立 Compose 项目、随机回环端口及临时账号，退出清理测试资源。可显式指定 `--ports RTMP RTMPS RTSP API`、`--ffmpeg`、`--ffprobe` 和 `--report`。协议探针额外需要已有 Go 1.26；Go、FFmpeg 和图形播放器都不是生产媒体转发的持续依赖。

`validate-ubuntu24-vm.sh` 需要可用 `/dev/kvm`，下载官方 cloud image 并校验 SHA256，仅在隔离 guest 内安装系统依赖及执行重启。辅助 native MediaMTX 测试使用 `smoke-test.sh --mediamtx /路径/mediamtx`，不能单独证明 Docker 部署。历史匿名版报告与包含编码/解码的测量保留作对照，以本报告链接的新版鉴权证据为准。
