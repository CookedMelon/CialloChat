# 部署 CialloChat

## 环境与初始化

正式目标是原生 Ubuntu 24.04 amd64/arm64；Ubuntu 26.04 用于开发。Python 使用 `/usr/bin/python3` 建立项目 `.venv`，依赖全部固定在 `requirements.lock`。Docker Compose 需要 ≥ 2.24.4（本地端口覆盖使用 `!override`），已有可用 Docker/Compose 会复用。

```bash
sudo ./setup.sh --mode production
```

新生产实例没有证书时，setup 会在配置阶段明确失败。已完成的安装和初始化会保留，配置证书后重复执行即可。setup 不创建默认推流密码，不启动 CialloChat，不修改防火墙，不自动加入 docker 组，不自动卸载冲突包。使用 setup 的同一管理员身份运行后续命令，避免混用 root/普通用户造成文件权限问题。

原生 Ubuntu 缺少 Docker 时，setup 使用 Docker 官方仓库，并按 `/etc/os-release` 选择 noble 或 resolute；已有 CLI 但 daemon 不可用时报告故障。WSL 应先在 Docker Desktop → Settings → Resources → WSL Integration 启用对应发行版，确认 `docker info` 和 `docker compose version` 真正成功，再运行 setup。setup 不在 WSL 中替换 Docker Desktop。

```bash
./setup.sh --check                 # 只读，不安装依赖或初始化
./streamctl doctor --with-validation
```

首次运行需要访问系统 apt 源、Docker 官方仓库、PyPI 和 Docker Hub。端口冲突在启动前检查；doctor 会区分 OS、架构、WSL、Python、Docker daemon、Compose、FFmpeg 和配置权限。

## 生产设置与 TLS

在停止服务后编辑 `runtime/settings.json`，保留 schema 与端口字段。示例改动：

```json
{
  "mode": "production",
  "hostname": "stream.example.com",
  "bind_address": "0.0.0.0",
  "certificate": "certs/server.crt",
  "private_key": "certs/server.key"
}
```

此片段是修改项，不能替代完整设置文件。证书 PEM 应包含服务器证书和中间证书链，私钥为可无人值守读取的 PEM。使用外部签发/续期工具，不要求某个证书供应商。将文件放入 runtime/certs，设置权限：

```bash
chmod 700 runtime runtime/certs
chmod 600 runtime/settings.json runtime/certs/server.crt runtime/certs/server.key
./streamctl apply --mode production
./streamctl config-check
./streamctl user add alice
./streamctl up --mode production
./streamctl status
```

工具检查文件权限、证书/私钥公钥一致、主机名、有效期和信任链。生产缺少或无效证书时启动失败；RTMP 服务只开启 TLS 的 1936，Compose 不发布 1935。RTSP 8554 要求该路独立观看密钥，仍为未加密播放；推流 TLS 不代表观看链路加密。MediaMTX 的 publish 授权按账号与路径生效，RTSP 服务本身也支持已授权账号发布；指定的推流工作流使用 RTMPS，不能把协议端口限制当作按协议授权。

容器使用运行管理工具者的 UID/GID，runtime 与私钥无需放宽给其他宿主用户。容器根文件系统只读，配置目录与证书目录只读挂载；挂载目录允许原子替换生成配置。

开放 TCP 1936、8554，管理 API 的 9997 只绑定 127.0.0.1。地址/端口可配置，local 强制宿主回环绑定。Docker 发布端口可能绕过 UFW；按 Docker 的 DOCKER-USER/iptables 规则和云安全组控制访问，不能仅凭 UFW 状态判断暴露范围。setup 不自动重写规则。

## 证书更新

活动直播会因证书重启短暂断开。先在独立目录验证新证书、私钥和链，保存旧文件，再在维护窗口替换 runtime/certs 下的文件，保持 600 权限：

```bash
./streamctl certificate-reload
```

该命令检查证书，重启 MediaMTX，确认管理接口和实际 TLS 连接中服务器证书与新文件相同。若失败，恢复旧证书/私钥，重新执行命令。OBS/FFmpeg 应重新连接。`runtime/certs/ca.crt` 仅用于显式本地测试 CA，正式公网证书部署应删除测试 CA 并使用系统信任链。

## 服务与升级

容器使用 `restart: unless-stopped`；宿主 Docker daemon 应由 systemd 启动。日志每份 10 MiB，保留 3 份。状态分别输出 container_running、api_available 和各路径 ready/tracks/readers。容器/API 活着不等于直播有媒体。

```bash
./streamctl logs --tail 100
./streamctl logs --follow
./streamctl down
./streamctl backup /安全目录/before-upgrade.json --include-certificates
```

升级需显式修改 `config/version.json` 和 Compose 中版本、digest，核对模板/API，备份后测试。失败回到原版本的程序、镜像和备份。restore 拒绝不同版本元数据，不能把不兼容备份直接套进新版本。

单条直播出口带宽约等于码率 × 同时读者数，再预留协议开销；第一版无转码、流量额度、录制、HLS 或 WebRTC，不承诺公网延迟。

依据：[Docker Ubuntu 安装](https://docs.docker.com/engine/install/ubuntu/)、[Compose 合并规则](https://docs.docker.com/reference/compose-file/merge/)、[MediaMTX 1.21.1](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.1)。
