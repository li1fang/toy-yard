# Toy-Yard Linux v0 Runbook

## 1. 目标

把 Linux 先接成：

- 单 lane
- 单方向
- SSH 同步索引包

当前固定 lane：

- `audio`

当前固定方向：

- `linux-audio-01 -> windows-yard-01`

## 2. 当前实现状态

当前 toy-yard 已实现：

- `toyyard import audio-session`
- `toyyard export audio-index-packet --session-id <id> --node-id <node_id>`
- `toyyard import audio-index-packet --packet-path <path>`

这意味着：

- Linux 可以把本地 audio session 转成 index packet
- Windows 可以只导入 packet，不复制音频数据

## 3. 建议节点命名

- Linux：`linux-audio-01`
- Windows：`windows-yard-01`

## 4. 建议目录

Linux：

- toy-yard root：`/srv/toy-yard`
- packet outbox：`/srv/toy-yard/_exchange/index_packets`

Windows：

- toy-yard root：`C:\Projects\toy-yard`
- packet inbox：`C:\Projects\toy-yard\_exchange\index_packets`

## 5. Linux 侧执行顺序

### 5.1 初始化

```bash
python -m toyyard --root /srv/toy-yard init
```

### 5.2 导入一条 audio session

```bash
python -m toyyard --root /srv/toy-yard import audio-session \
  --manifest-path /path/to/session_manifest.json
```

### 5.3 导出 index packet

```bash
python -m toyyard --root /srv/toy-yard export audio-index-packet \
  --session-id audio-session-001 \
  --node-id linux-audio-01
```

默认会写到：

```text
/srv/toy-yard/_exchange/index_packets/audio__linux-audio-01__audio-session-001.json
```

### 5.4 用 SSH 发送到 Windows

建议先用最朴素的 `scp`：

```bash
scp /srv/toy-yard/_exchange/index_packets/audio__linux-audio-01__audio-session-001.json \
  garro@windows-host:/cygdrive/c/Projects/toy-yard/_exchange/index_packets/
```

如果后续需要增量同步，再换 `rsync`。

## 6. Windows 侧执行顺序

### 6.1 导入 packet

```powershell
python -m toyyard --root C:\Projects\toy-yard import audio-index-packet `
  --packet-path C:\Projects\toy-yard\_exchange\index_packets\audio__linux-audio-01__audio-session-001.json
```

### 6.2 检查 catalog

```powershell
python -m toyyard --root C:\Projects\toy-yard report audio-catalog
```

预期至少能看到：

- `availability = remote_index_only`
- `replicated_from_node = linux-audio-01`

## 7. v0 验收标准

本轮只认这几个标准：

1. Linux 成功导入一条 `audio-session`
2. Linux 成功导出 `audio-index-packet`
3. packet 成功通过 SSH 传到 Windows
4. Windows 成功导入 packet
5. Windows `audio-catalog` 能看见远端 package
6. Windows 没有复制任何真实音频文件
7. 同一 packet 重复导入仍然幂等

## 8. 故障分层

### packet 导不出来

优先检查：

- session 是否已经被 `audio-session` 导入
- `session_id` 是否正确
- Linux toy-yard 根目录是否正确

### SSH 传不过去

这是传输层问题，不是 toy-yard index contract 问题。

优先检查：

- SSH 用户名
- 目标路径
- Windows SSH 服务或中间层映射

### Windows 导入失败

优先检查：

- packet 是否完整
- `packet_schema_version` 是否匹配
- Windows 侧 toy-yard 版本是否包含 `audio-index-packet` importer

## 9. 为什么这就是正确的 v0

因为这一步已经把最重要的东西接通了：

- canonical identity
- remote visibility
- idempotent packet import
- SSH exchange seam

而且没有过早引入：

- 媒体同步
- 文件锁
- 双向写冲突

这让后续扩展会很稳。

## 10. 后续升级顺序

### Linux v1

补：

- outbox/inbox 状态文件
- packet archive
- sync run log

### Linux v2

补：

- 第二 lane
- 双向 packet
- peer 配置

### Linux v3

补：

- 真正的双写双索引
- 按 lane ownership 控制谁是写入 owner
- 按需拉取媒体数据
