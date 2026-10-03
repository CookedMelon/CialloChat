# 四路播放对比

四路均为 x264 High、2560×1440、yuv420p、单切片、无 B 帧、CBR，以及 AAC 48 kHz 立体声 128 Kbps。以 T01 为基准，其他每路只改变一个测试项。

| 编号 | 帧率 | 视频目标码率 | 关键帧间隔 | 对比项 |
| --- | --- | --- | --- | --- |
| T01 | 60 fps | 12000 Kbps | 2 秒 | 基准 |
| T02 | 30 fps | 12000 Kbps | 2 秒 | 帧率 |
| T03 | 60 fps | 6000 Kbps | 2 秒 | 码率 |
| T04 | 60 fps | 12000 Kbps | 0 秒／自动 | 关键帧 |

0 秒按照 OBS 的含义保留编码器自动配置，不把它传给 FFmpeg 的 `-g 0`。本轮素材的自动间隔实测约 4.17 秒；固定间隔素材实测为 2 秒。依据：[OBS x264 配置](https://github.com/obsproject/obs-studio/blob/master/plugins/obs-x264/obs-x264.c)。

画面显示配置、移动测试图、帧计数和片段时间，并有连续测试音。片段每 12 秒循环一次，计数会正常重置。它们用于比较出画、运动和冻结；片段时间不是当前墙钟，不能单独据此测量端到端直播延迟。

每路使用独立账号与观看密钥，以与 OBS 相同的 RTMPS 发布、RTSP/TCP 观看路径进行测试。先启动并检查，再从私有交付文件取得完整 `vrchat_read_url`；公开文档只提供格式：

```text
rtspt://SERVER:8554/live/vrc-test-01?read_key=REPLACE_WITH_THIS_TEST_READ_KEY
```

在同一房间依次播放四路，每路观察约 5 分钟，记录是否出画、是否自行冻结，以及冻结的大致时刻。管理员按测试路径与时间关联来源地址、会话状态和发送计数。观看测试不依赖 OBS 持续推流。

## 重建素材与启动

在已有 FFmpeg（libx264、drawtext）和字体的开发环境预编码，上传输出目录。生产服务器只循环复制已编码音视频，不实时转码。

```bash
python3 scripts/build-playback-fixtures.py --output runtime/playback-comparison-4
```

在已有项目虚拟环境的部署服务器运行：

```bash
/opt/ciallochat/.venv/bin/python /opt/ciallochat/scripts/publish-playback-matrix.py \
  --project-root /opt/ciallochat --runtime /opt/ciallochat/runtime \
  --fixtures /root/ciallochat-upload/playback-comparison-4 \
  --handoff /root/ciallochat-upload/playback-comparison-4/watch-urls.json \
  --hours 48 --publish-loopback
```

`--publish-loopback` 使用回环发布并按公开主机名校验生产 TLS 证书，观看仍走公网。

交付文件包含观看密钥，需保持 0600，不能提交到 Git。程序在退出或到期时停止发布并撤销自己创建的测试账号；通过用户名和推流哈希匹配所有权，不删除已有用户。若使用 systemd，停止对应测试单元即可清理，不重启生产媒体服务。本轮单元名为 `ciallochat-playback-comparison.service`，启动和清理都已核对原账号、原 OBS 发布会话及生产容器保持不变。

素材参数、四路服务器音视频解码及路径状态见 [四路测试证据](evidence/playback-comparison.json)。公网探测曾遇到连接超时，服务器回环验证不能替代 VRChat 房间的稳定播放结果；房间对比仍待用户反馈。

## 本轮房间反馈与公网发送故障

用户反馈 T01 显示 stream end，T02/T03 出画后冻结或间歇更新，T04 没有反应；冻结的路线也会迟迟不出首帧。本轮四路均未通过稳定播放验证。

北京时间 10 月 4 日 00:54–01:00 的对应观看会话已进入 RTSP/TCP 双轨播放，来源均匹配此前确认的直连公网地址。随后四路都出现发送队列溢出，丢弃 RTP 包，并多次发生 TCP 写超时。首次 T01 会话丢弃 10343 个 RTP 包后在约 10 秒结束；选取的 T02 会话丢弃 32846 个；T03 为 6582 个；T04 为 10495 个。这里计数是 RTP 包，不是解码后完整视频帧。迟出画、冻结及结束播放在本轮都有传输堵塞证据，不能据此认定四种编码分别存在已定位的不同兼容问题。

普通 SSH 文件下载同样慢：恢复正常路由后，一条新连接的 20.061 秒实测速率约 1.137 Mbps；两条新连接并发的总速率约 1.038 Mbps。TCP 采样存在重传、较小的拥塞窗口，接收窗口仍开放。检查时 MediaMTX CPU 约 13%，可用内存约 912 MiB，eth0 队列无积压、无累计丢弃；这只是观察窗口，不是对所有时刻资源状态的保证。

仅到当前观看地址的临时路由 MTU 1200 测试未明显改善，45 秒后自动移除，恢复已核对。未留下永久网络参数调整，生产媒体容器未重启，四路素材继续发布。

用户确认服务器是轻量应用服务器的 200 Mbps 峰值套餐。[阿里云资源限制说明](https://help.aliyun.com/zh/simple-application-server/product-overview/limits) 明确，峰值不作为持续公网带宽的业务承诺，资源争抢时可能限速或丢包。但当前采样不能单独确定是云端限速、线路丢包还是端点转发导致，应以这些会话时间、普通下载吞吐和重传证据核查实际公网交付，再继续编码对比。

补充普通大文件双向传输：使用系统 SCP/SFTP、新建 SSH 连接、关闭压缩，测试内容为随机字节，不经过媒体服务或编码器。64 MiB 目标上传在 192.558 秒采样后主动停止，服务端实际收到 23761920 字节（约 22.66 MiB），平均 0.987 Mbps；上传目标未全部完成，不能记为完整 64 MiB 上传。已收文件与本地源文件对应前缀 SHA256 一致。随后完整下载这份 23761920 字节文件耗时 172.549 秒，平均 1.102 Mbps，整份文件 SHA256 校验通过；多个连续 15 秒区间约为 1.114 Mbps。相比短时间探测，这证明本次普通文件传输持续偏慢，但仍不能单独定位到云端限速或具体网络组件。
