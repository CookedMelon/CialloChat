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
