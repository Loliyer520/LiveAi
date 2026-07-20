# MC 26.2 mineflayer 协议适配补丁

## 背景

MC 26.2（协议版本 776，data version 4903，build 2026-06-16）运行在 `/my/own/mc/`。

mineflayer 使用的 minecraft-data 库中，26.2 的 protocol.json 是直接从 1.21.11（协议版本 774）复制的，**未包含 26.2 的实际协议变更**。

PrismarineJS/minecraft-data 的 `pc_26_2` 分支于 2026-06-16 创建，但协议数据从未从实际服务器 JAR 中提取生成——所有 26.2 数据文件（protocol.json, blocks.json, items.json 等）都是 1.21.11 的副本。

## 当前状态

### 已修复
- **login/success 包**: 添加了 `restBuffer` 字段吸收 26.2 新增的 16 字节数据（疑似 UUID 类型的会话/档案标识符）。这阻止了登录阶段的第一层字节不同步。

### 未修复
- **所有 Play 阶段包**: 大量 `PartialReadError`，包括：
  - `end_combat_event` (0x40)
  - `set_projectile_power` (0x85)
  - `recipe_book_settings` (0x4a)
  - `remove_entity_effect` (0x4c)
  - `world_border_center` (0x56)
  - ...等几乎所有 play 包

- **Serverbound 包**: 服务器报 `Failed to decode packet 'serverbound/minecraft:jigsaw_generate'`，说明 bot 发出的包也被误读

### 根本原因
login/success 之后的所有包因为字节不同步而全部解析错误。26.2 的实际包结构与 1.21.11（protocol.json 的定义）有差异，但缺少官方协议文档来确认具体变更。

## 补丁文件

| 文件 | 说明 |
|------|------|
| `protocol_26.2_patched.json` | 打了 login/success restBuffer 补丁的完整 protocol.json |
| `apply.sh` | 将补丁 protocol.json 复制到 node_modules 的脚本 |

## 使用方法

```bash
# npm install 之后
cd /my/own/autoplay
bash patches/apply.sh

# 运行 bot
node main.js --username BotName
```

## 下一步（TODO）

1. **等待 PrismarineJS 更新**: 关注 [minecraft-data](https://github.com/PrismarineJS/minecraft-data) 仓库，等 `pc_26_2` 分支生成真实的 26.2 协议数据后，运行 `npm update minecraft-data`。

2. **或自行提取协议**: 
   - 使用 minecraft-data 的协议提取工具（`node minecraft-data/bin/pc_extractor.js`）从 26.2 服务器 JAR 生成数据
   - 需要能访问 PrismarineJS/minecraft-data 仓库的完整源码

3. **手动逐个修复**: 通过包捕获分析每个包的字节结构，逐个更新 protocol.json。工作量极大。

## 26.2 服务器信息

- 协议版本: 776 (0x308)
- 数据版本: 4903
- 构建时间: 2026-06-16T12:01:27Z
- Java 版本: 25
- 在线模式: false
- Screen 会话: mc
