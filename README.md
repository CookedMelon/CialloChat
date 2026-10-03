# CialloChat

单机直播推流服务：OBS / FFmpeg 发布 H.264 + AAC，MediaMTX 向多个 RTSP/TCP 观看者分发同一路直播。每个账号有独立推流密钥、观看密钥和固定路径 `live/<username>`，禁止匿名观看，管理接口仅映射本机并独立认证。媒体直接转发，无转码、录制或网页后台。

已实现双密钥管理、旧账号迁移、连接撤销、TLS、备份恢复和服务器协议延迟测量。播放器调优不属于项目交付范围。实测范围与限制见验证报告。

部署目标 Ubuntu 24.04，开发兼容 Ubuntu 26.04，Python ≥ 3.12。MediaMTX 固定为 **1.21.1**，镜像及 digest 见 [版本锁定](config/version.json)。完整需求见 [PROJECT_PLAN.md](PROJECT_PLAN.md)。

本地使用：

```bash
./setup.sh --mode local --with-validation
./streamctl user add alice
./streamctl user add bob
./streamctl up --mode local
./streamctl status
./scripts/smoke-test.sh
./streamctl down
```

`user add` 一次性交付独立的推流密钥、观看密钥以及编码后的两类 URL。保存到只有管理员可读的地方。生产模式先配置主机名和 TLS，再执行 `up --mode production`，详见 [部署说明](docs/deployment.md)。setup 只准备环境，不启动服务。

- [账号与运维命令](docs/accounts.md)
- [OBS、FFmpeg、VLC 客户端](docs/clients.md)
- [验证范围与结果](docs/validation.md)
- [服务端转发延迟测量](docs/latency.md)

开发验证：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh streamctl scripts/*.sh
./scripts/validate-ubuntu24.sh          # 隔离的 Python 3.12/Ubuntu 24.04 容器验证
./scripts/validate-ubuntu24-vm.sh       # 隔离 Ubuntu 24.04 VM 安装与重启验收
./setup.sh --check --with-validation
```

`runtime/`、虚拟环境、账号、证书和备份均不进入版本控制。该工具不承诺任意编码转换、RTSP 链路加密、硬码率限制或高可用。

完整源码交付包可用 `./scripts/package.sh` 生成到 `dist/ciallochat-dual-key.tar.gz`，附 SHA256 校验和与包内文件清单；包含源码、部署脚本、中文说明和验证证据，不包含 runtime、账号、密钥、证书或虚拟环境。解压后按部署文档初始化新实例。旧实例先按 [迁移步骤](docs/accounts.md#旧账号迁移) 升级账号。

敏感运行文件的公开占位样例及对应生成方式见 [examples/README.md](examples/README.md)。示例中的密钥、哈希和证书都不可用；新服务器必须生成自己的账号、管理凭据与证书。
