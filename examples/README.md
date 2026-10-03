# 敏感文件占位示例

这里展示真实运行文件的结构，所有密钥、哈希、主机名均为占位符，不能用于启动服务。真实 `runtime/`、`.env`、账号凭据、私钥、备份和工具缓存被 Git 忽略，也不进入源码交付包。不要解除忽略规则来提交真实配置。

| 真实文件 | 公开示例 | 生成方式 |
| --- | --- | --- |
| `runtime/settings.json` | `runtime/settings.example.json` | setup 初始化后按部署环境修改 |
| `runtime/accounts.json` | `runtime/accounts.example.json` | `streamctl user add/import` 生成独立 Argon2id 哈希 |
| `runtime/control.json` | `runtime/control.example.json` | setup 自动生成独立管理凭据 |
| `runtime/mediamtx/mediamtx.yml` | `runtime/mediamtx/mediamtx.example.yml` | 从账号记录生成，不手工填入示例哈希 |
| `runtime/watchdog/config.json` | `runtime/watchdog/config.example.json` | streamctl 从设置和管理凭据生成，仅供码率监控容器使用 |
| 用户交付凭据 JSON | `runtime/user-credentials.example.json` | 创建账号或重置密钥时受限保存 |
| `runtime/certs/server.crt` | `runtime/certs/server.crt.example` | 证书机构签发的服务器证书及完整中间链 |
| `runtime/certs/server.key` | `runtime/certs/server.key.example` | 与证书对应的真实 PEM 私钥 |
| `runtime/backups/*.json` | 不提供可恢复的假备份 | `streamctl backup` 从真实实例生成，权限 600 |

示例路径相对于本目录。真实文件保持 600、runtime 保持 700；示例无需限制读取。证书和私钥占位内容只说明格式，不包含可用密码、哈希、证书或私钥。

创建可用实例：先执行 setup，再通过 CLI 建立账号。不要把这些占位符文件直接复制到 runtime。推流凭据交给发布者，观看凭据交给观看者，Control API 凭据只供管理员使用。
