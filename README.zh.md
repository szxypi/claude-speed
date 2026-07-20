# claude-speed

**看见 Claude Code 的真实生成速度。** macOS 菜单栏应用 + 终端 statusline，把回复的**真·生成速度（tok/s）**和**首字延迟（TTFT）**拆开显示——后者才是真正在波动的东西。

[English docs →](README.md)

> 非官方社区工具，与 Anthropic 无关。

```
菜单栏:      ⚠️🟢71 ⏳8s          ← 近期有错 · 真实速度 71 tok/s · 当前请求已等 8 秒
statusline:  Fable 5 | ⚡71 tok/s 首字4.2s | 最近1022tok·22s | ctx 70%
```

<!-- TODO: 补截图 — assets/menubar.png, assets/statusline.png -->

## 为什么做这个

用 Claude Code 常觉得「忽快忽慢」，其中大部分是**测量假象**：

- 每次 API 响应都有一笔**固定的首字开销（TTFT，通常 5–7 秒）**：排队、prompt 预填充、缓存查找；
- 朴素算法 `输出token ÷ 总耗时` 因此**在短回复上塌方**：真速 60 tok/s、TTFT 5s 时，300 token 的回复显示 30 tok/s（虚低一半），3000 token 的回复显示 55——同一台服务器、同一秒。

claude-speed 对近期响应拟合 `耗时 ≈ TTFT + token数/TPS`（Theil-Sen 回归，对离群稳健），把两个数分开报告：

- **TPS（斜率）**——模型真实生成速度。很稳。这是主指标；
- **TTFT（截距）**——首字延迟。真正在抖的量（缓存未命中/上下文太长/服务端高峰），也是本工具报警的对象。

## 组件

| 文件 | 作用 |
|---|---|
| `collect.py` | 采集核心：扫 `~/.claude/projects/` 活跃会话 transcript，输出「首行=菜单栏标题，其余行=下拉明细」 |
| `main.swift` → `ClaudeSpeed` | 菜单栏应用（零第三方依赖，`swiftc` 直编），每 3s 刷新 |
| `statusline-speed.py` | [Claude Code statusline](https://docs.anthropic.com/en/docs/claude-code/statusline) 脚本——同一套算法，渲染成输入框下方一行 ANSI 彩色文本 |

覆盖所有客户端（CLI、桌面 App、VS Code 插件都写同一份 transcript）。只读本地文件，无任何网络请求。

## 安装

**菜单栏应用（macOS）：**

```bash
git clone https://github.com/YOURNAME/claude-speed && cd claude-speed
./install.sh                # 只装菜单栏
./install.sh --statusline   # 顺带接线 Claude Code statusline
```

需要 Xcode Command Line Tools（`xcode-select --install`）和 python3。更新代码后重跑 `install.sh` 即可。

**只用 statusline（任何平台，含 Linux）：** 在 `~/.claude/settings.json` 加：

```json
{ "statusLine": { "type": "command", "command": "/path/to/claude-speed/statusline-speed.py", "padding": 0 } }
```

**卸载：** `./uninstall.sh`（移除 LaunchAgent 和 statusline 接线）。

## 显示语义速查

菜单栏标题 = `[⚠️][速度灯][ ⏳Ns]`，例 `⚠️🟢71 ⏳8s`：

| 符号 | 含义 |
|---|---|
| 🟢 / 🟡 / 🔴 | 纯生成速度 ≥50 / ≥30 / <30 tok/s |
| `≈70` | 斜率借自同模型其他会话（两阶段拟合——新会话样本还少） |
| `≥50` | 下界估计（样本不足以拆 TTFT，真值只会更高） |
| `⚪` | 闲置（10 分钟无响应）或速度不确定 |
| `⏳8s`（实时） | 已发出请求等了 8 秒还没响应——每 3s 刷新看得到在涨；>120s 视为已中断自动消失 |
| `⏳18s`（响应后） | 最近拟合的首字延迟 ≥12s 告警 |
| `⚠️` | 近 5 分钟内有 API 错误 |

下拉每行一个活跃会话（最多 3 个，近 2 小时）：

```
myproject·fable5  🟢 71 tok/s 首字4s  缓存12%冷  ⚠️1错  最近1022tok·22s  4秒前
```

`首字Ns`=首字延迟 · `·近N分`=扩窗后的拟合口径（≤10 分钟不标）· `缓存N%`=命中率 <90% 才显示（解释首字尖峰；`冷`=缓存写入多于读取）· `⚠️N错`=近 30 分钟错误数 · `最近Ntok·Ns`=最后一条响应的规模；全错会话显示 `🔴 无成功响应`；等待中的会话按有无历史响应显示「等待首个响应/等待响应」。

statusline 颜色：速度 绿≥50/黄≥30/红<30 · 首字 绿≤5s/黄≤12s/红>12s · ctx 黄≥60%/红≥85%。

## 测速算法

- transcript JSONL 里同一 `message.id` 的连续 assistant 记录 = 一次 API 响应。耗时 = 组内最后时间戳 − 组前一条记录时间戳（≈请求发出时刻），**含 TTFT**；token 数取组内 `usage.output_tokens` 最大值（同组 usage 重复）；
- API 错误合成消息（`message.model == '<synthetic>'`）不成组，但时间戳仍当下一组锚点（报错重试从错误时刻起算更准），并计入 ⚠️ 指示；
- **分模型拟合**：只用当前模型的组——会话中途切模型混拟会失真；
- **剔离群**：`耗时/token > 0.5s` 的组掺了「用户打字停顿」，剔除；
- **滑动窗口**：优先用近 10 分钟样本（服务端负载随时段漂移），拆不出则窗口倍增，扩窗标 `·近N分`；
- **两阶段拟合**：单会话样本不足时，斜率（模型属性）借同模型跨会话点池，截距（会话属性）本地拟合——新会话约 2 条响应就能出速度（标 `≈`）；
- **诚实回退**：再不行时，长回复（≥300tok）的 blended 速度按**下界** `≥N` 显示（下界过绿线才亮绿灯）；否则「速度样本不足」。大文件只尾读 400KB，坏行静默跳过。

## 运维

```bash
# 改 main.swift 后重编译
swiftc -O -o ClaudeSpeed main.swift

# 重启菜单栏应用——必须用 kickstart，不要用 unload/load：
# 在非 GUI 上下文（SSH/后台 shell/自动化代理）执行 unload/load 会把应用启动在
# 无 WindowServer 连接的环境：进程活着但图标不显示、定时器不跑。
launchctl kickstart -k gui/$(id -u)/com.claude-speed.menubar

# 手动测采集
./collect.py
```

阈值、窗口、会话数等可调参数都是两个 Python 脚本顶部的常量。

## License

[MIT](LICENSE)
