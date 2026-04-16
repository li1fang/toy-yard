# Toy-Yard Index Packet Replication 正式设计稿 v0

## 1. 目标

当前目标不是同步整个仓库，也不是同步 SQLite，更不是同步大体积素材数据。

当前目标是先建立一条稳定、可审计、可幂等的：

- `node A -> index packet -> SSH -> node B`

复制链路。

v0 固定范围：

- lane：`audio`
- 方向：`single direction`
- 载荷：`index only`
- 传输：`SSH/scp/rsync`
- 数据面：`不同步音频文件本体`

这条链路的作用是：

- 让 Linux 节点先开始入仓和产出索引
- 让 Windows 主仓能看见远端 lane 的 canonical sample/package/artifact
- 先把“索引传播”做稳，再扩展到真正的双写双索引

## 2. 非目标

v0 明确不做：

- 同步 `toyyard.sqlite`
- 同步 `04_registry/canonical/audio/**` 的真实媒体文件
- 自动拉取远端媒体到本地
- 双向冲突合并
- 混合多个 lane 的统一复制

## 3. 节点模型

每个 toy-yard 实例是一个独立 node。

建议最少有两个 node id：

- `windows-yard-01`
- `linux-audio-01`

每个 node 保持：

- 自己的本地根目录
- 自己的 SQLite
- 自己的 canonical 写入
- 自己导出的 index packet

v0 的权责切法：

- Linux node
  - 负责 `audio-session` 的本地导入
  - 负责导出 `audio index packet`
- Windows node
  - 负责导入 Linux 发来的 `audio index packet`
  - 只建立 `remote_index_only` 的索引可见性

## 4. Packet Contract

当前已实现的 packet 形状是：

```json
{
  "packet_schema_version": "audio_index_packet_v0",
  "packet_kind": "audio_index",
  "lane": "audio",
  "origin_node_id": "linux-audio-01",
  "created_at": "...",
  "packet_id": "idxpkt_audio_...",
  "payload_hash": "...",
  "sample": {},
  "package": {},
  "source": {},
  "artifacts": {
    "source": [],
    "package": []
  }
}
```

关键约束：

- `packet_schema_version` 固定为 `audio_index_packet_v0`
- `origin_node_id` 明确说明包来自哪个 node
- `packet_id` 是这次导出快照的稳定身份
- `payload_hash` 用于检测“内容相同但传输路径不同”
- packet 只携带：
  - sample/package 元数据
  - alias
  - source 元数据
  - artifact 路径与格式
- packet 不携带音频二进制

## 5. Idempotency

v0 的幂等策略是：

- packet 级：
  - `packet://<origin_node_id>/<packet_id>`
- 实体级：
  - sample 依赖 `canonical_sample_id`
  - package 依赖 `canonical_package_id`
  - alias 依赖现有唯一约束
  - artifact 依赖 `(owner_type, owner_id, stage, artifact_kind, path)`

这意味着：

- 同一 packet 重复导入不会制造新的 canonical package
- 同一远端 artifact 重复导入不会制造新的 artifact 行
- packet 文件路径变了，只要内容未变，实体层仍然稳定

## 6. 状态语义

v0 新增了两个非常重要的本地可见语义：

- package metadata
  - `availability = remote_index_only`
- artifact status
  - `remote_indexed`

这两个语义表示：

- 本地已经知道这份资产存在
- 但本地并没有拿到真实媒体数据
- 当前只是索引层接通

这一步非常关键，因为它让“看得见”与“已经拿到本地数据”明确分开。

## 7. 为什么 v0 不同步数据

因为 v0 要先解决的是：

- canonical id 是否稳定
- alias 是否稳定
- 远端 artifact 是否能安全落地为本地索引
- 重复导入是否幂等
- SSH 包传输是否足够稳定

如果一开始就同步数据，会把下面几类问题糊在一起：

- 网络传输问题
- 大文件校验问题
- 路径映射问题
- 远端缓存策略问题
- canonical/index 模型本身的问题

所以 v0 的策略是：

先同步“知识”，不同步“素材本体”。

## 8. Linux v0 的正式边界

Linux v0 只做：

1. `toyyard import audio-session`
2. `toyyard export audio-index-packet`
3. 用 `scp` 或 `rsync` 把 packet 发到 Windows

Windows v0 只做：

1. `toyyard import audio-index-packet`
2. 在本地 catalog/report 中看到远端音频资产

## 9. 未来如何扩到双写双索引

### v1

新增：

- `node registry`
- `replication run log`
- `artifact location` 记录
- lane 级 ownership

目标：

- 不同 lane 可以有不同 owner node
- 不再默认所有 lane 都由 Windows 主仓单写

### v2

新增：

- 双向 packet
- 冲突检测
- field ownership
- on-demand artifact fetch

目标：

- 允许 Linux 与 Windows 都成为 canonical 写入端
- 但每个字段、每个 lane、每个 artifact 的 ownership 必须明确

### v3

新增：

- 多消费者索引包
- 统一 exchange hub
- Linux/Windows/macOS 多节点

目标：

- toy-yard 从单机仓库升级成真正的 packet exchange hub

## 10. 当前结论

当前这版 v0 已经足够作为正式起步模型：

- 简单
- 真实可跑
- 不偷带媒体同步复杂性
- 能自然升级到双写双索引

所以建议后续严格遵守一个顺序：

1. 先把 `audio index packet` 跑稳
2. 再补 node/ownership 元信息
3. 再考虑双向 packet
4. 最后才碰真实数据复制
