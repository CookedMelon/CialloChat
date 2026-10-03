# 账号与运维

从项目根目录运行命令。管理员需要 runtime 文件和 Docker 访问权限。可用 `--runtime /独立目录` 或 `CIALLOCHAT_RUNTIME` 指定数据目录；隔离服务还需设置 `CIALLOCHAT_PROJECT`，默认项目名 `ciallochat`。

## 创建与交付

```bash
umask 077
./streamctl user add alice > /安全目录/alice-credentials.json
./streamctl user add bob --prompt-password --prompt-read-key
./streamctl user list
./streamctl user info alice
```

创建同时交付 `publish_username`、`publish_key`、`publish_url`、`read_username`、`read_key`、`read_url`。把 `publish_url` 交给发布者，把 `read_url` 交给观看者。观看身份形如 `ciallochat-read-alice`，由 CLI 生成；不要用业务用户名或推流密钥猜测观看凭据。`password` 是兼容旧调用的推流密钥别名。

每个账号固定路径 `live/<username>`。用户名为 1–48 位 ASCII，首位字母或数字，其余可含 `_`、`-`；保留 `any`、`admin` 及 `ciallochat-` 前缀。两类密钥独立随机生成，默认各有 192 bit 熵；自选密钥为 12–256 字符，不能有控制字符，两类密钥不得相同。分别用独立随机盐的 Argon2id 保存；list/info 和业务备份不输出媒体明文密钥。

明文只在创建或对应重置时交付。已经保存当前密钥时，可隐藏输入重新生成该类 URL：

```bash
./streamctl user credentials alice --kind publish --prompt-password
./streamctl user credentials alice --kind read --prompt-password
```

`credentials` 不会从哈希恢复遗忘的密钥，输入错误会失败；遗忘时重置对应密钥。`--password-stdin` 接收对应操作的一行密钥；add 另有 `--read-key-stdin`。同时使用两个 stdin 选项时，先读一行推流密钥，再读一行观看密钥。不要把密钥当命令行参数；不要将完整 URL 写进日志或版本控制。管理工具负责 URL 编码，包括 `&`、`#`、`%` 和 Unicode。

## 密钥重置与撤销

```bash
./streamctl user reset-publish-key alice
./streamctl user reset-read-key alice
./streamctl user disable alice
./streamctl user enable alice
./streamctl user delete bob
```

`reset-password` 保留为 `reset-publish-key` 的兼容命令。重置推流密钥会断开该路径发布者，观看密钥保留；重置观看密钥会断开该路径全部读者，发布者和推流密钥保留。禁用或删除会断开发布者及读者，拒绝双方重连；启用后可使用现有密钥重新连接。一个观看密钥可共享给多个观看者，要撤销其中持钥者需重置整路观看密钥。

CLI 使用文件锁、原子写入、旧版本备份和事务日志，确认完整配置 revision 已加载，再通过本机认证 API 撤销 RTMP/RTMPS/RTSP 对应连接。API 隐藏哈希，不能仅比较掩码判定生效。撤销未完成会返回非零并记录待处理范围，后续操作重试；可执行 `down`、`up` 断开所有残留连接。第二位发布者不能覆盖原直播。

日志跟随和等待输入不占用账号修改锁。证书续期文件缺失不阻止撤销权限；启动、apply、证书重载仍严格检查 TLS。服务停止时账号操作只生成配置。runtime/backups 中自动备份同样需要保护。

## 批量导入

导入受限 UTF-8 JSON 数组，字段为 `username`、`publish_key`、`read_key`；`password` 兼容旧推流字段。缺失的密钥分别随机生成。示例中密钥应替换为各自独立的真实值：

```json
[
  {"username":"alice","publish_key":"publish-example-A","read_key":"view-example-A"},
  {"username":"bob"}
]
```

```bash
chmod 600 /安全目录/users.json
./streamctl user import /安全目录/users.json --credentials-file /安全目录/new-credentials.json
# 也可使用受保护标准输入：user import - --credentials-file ...
```

全部校验后才整批提交。重复账号、非法密钥或已有交付目标导致整批失败。自动生成密钥必须指定 `--credentials-file`；交付文件新建为 600，拒绝覆盖已有文件。全部密钥由管理员提供时可不指定交付文件。工具不自动删除输入文件。

## 旧账号迁移

schema 1 旧账号不会被普通启动、配置应用或账号命令接受。先停止服务，撤销所有旧匿名连接，再迁移：

```bash
./streamctl down
./streamctl user migrate --credentials-file /安全目录/read-credentials.json
./streamctl up
```

原推流哈希原样保留，每个账号独立生成观看密钥，受限文件交付其 `read_url`；账号升级为 schema 2，删除匿名 read 权限。迁移前保留原数据备份。迁移失败或中断时恢复到禁止媒体访问的配置，普通启动继续拒绝旧数据；不会重新开放匿名观看。排除错误后重新执行迁移，不要手工恢复旧匿名配置。

## 备份恢复

```bash
./streamctl backup /安全目录/backup.json
./streamctl backup /安全目录/with-tls.json --include-certificates
./streamctl down
./streamctl restore /安全目录/backup.json
./streamctl up
```

备份含两类媒体哈希、设置、配置、版本及独立 Control API 凭据；媒体明文密钥不包含在备份里。默认不含证书或私钥，显式选项才包含。输出权限 600，已有目标拒绝覆盖。

恢复要求服务已停止、备份权限 600、版本一致且数据与配置一致。先在临时目录检查，production 同时检查证书；未含证书的备份需要目标已有兼容证书。恢复前生成含证书的本机回滚备份，不自动启动服务。

恢复旧 schema 1 备份必须显式生成观看凭据，不能恢复匿名权限：

```bash
./streamctl restore /安全目录/old-backup.json --migrate-credentials-file /安全目录/restored-read-credentials.json
```
