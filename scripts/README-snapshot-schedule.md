# A0 每日快照调度（launchd）

每日本地 08:30 运行 `bellwether snapshot`（黄金集 90 标的全量），数据落 `~/.bellwether/snapshots/`，日志落 `~/.bellwether/logs/`。

## 安装 / 重装
```fish
mkdir -p ~/.bellwether/logs
cp scripts/com.bellwether.snapshot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/(id -u) ~/Library/LaunchAgents/com.bellwether.snapshot.plist
```

## 状态与手动触发
```fish
launchctl print gui/(id -u)/com.bellwether.snapshot | head -20   # 查看状态
launchctl kickstart gui/(id -u)/com.bellwether.snapshot          # 立即手动跑一次
cat ~/.bellwether/snapshots/last_status.json                     # 最近一次全量结果（告警面）
tail ~/.bellwether/logs/snapshot.err.log                         # 失败详情
```

## 卸载
```fish
launchctl bootout gui/(id -u)/com.bellwether.snapshot
rm ~/Library/LaunchAgents/com.bellwether.snapshot.plist
```

## 告警语义
- `last_status.json` 的 `ok=false` / 退出码 `2`=部分失败（降级）、`1`=全军覆没。
- 手工排查用 `bellwether snapshot --smoke`（写 `manifest-smoke.json`，不污染全量告警面）。
