#!/usr/bin/env python3
"""Claude Code statusline:显示上一条响应的实测生成速度。

数据来源两处:
- stdin:Claude Code 每次更新喂进来的会话 JSON(model / cost / context_window / transcript_path);
- transcript JSONL:同一 message.id 的连续 assistant 记录 = 一次 API 响应,
  用「组内最后时间戳 - 组前一条记录时间戳」近似该响应的端到端耗时(含 TTFT)。
  fit_speed 用 Theil-Sen 回归拆出纯生成速度与首字延迟,详见 README。
"""
import glob
import json
import os
import sys
import time
from datetime import datetime

# Windows 控制台默认 GBK/cp1252 编码,输出 emoji/中文会 UnicodeEncodeError;
# 托盘与 statusline 宿主都按 UTF-8 读,这里强制 stdout 为 UTF-8(其余平台无影响)。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass

TAIL_BYTES = 400_000  # transcript 可达几十 MB,只读尾部足够覆盖最近若干条响应

# 速度拆分参数(见 fit_speed,与 collect.py 完全一致)
MAX_SEC_PER_TOK = 0.5   # dur/out>0.5s(<2tok/s)的组基本掺了「发消息前的停顿」,剔除
MAX_TPS = 400           # TPS 合理上限:拟合接受域与样本过快界共用(见 plausible_point)
FIT_MIN_SAMPLES = 5     # 拆分所需最少有效响应数
FIT_MIN_SPAN = 150      # output token 跨度需 ≥ 此值,回归才有信息量
FIT_MIN_PAIR_DX = 50    # Theil-Sen 只取 x 差 ≥ 此值的点对,避免小分母放大噪声
FIT_WINDOW_START = 600  # 滑动窗口起步(秒):拟合优先用近期样本,不足自动倍增扩窗
ERR_ROW_WINDOW = 1800   # 统计近 30 分钟的 API 错误数
CACHE_OK = 0.9          # 缓存命中率颜色阈值(绿档)
AGENT_ACTIVE_WINDOW = 90   # 子代理文件在此窗口内有写入 → 视为活跃(在跑)
AGENT_BURN_WINDOW = 120    # 后台吞吐统计窗口:近 N 秒产出 token 之和 / N
AGENT_MAX_READ = 8         # 每轮最多读几个子代理文件(控 IO,其余只计数)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def tail_lines(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # 丢掉被截断的半行
            return f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return []


def response_groups(lines):
    """把同一 message.id 的连续 assistant 记录并成一次 API 响应。

    返回 (groups, err_ts):
      groups 每组含 out(输出token)/start/end/model 及输入侧 inp/cr/cc
      (input_tokens / cache_read / cache_creation,组内 usage 重复,取 max);
      err_ts 为 API 错误合成消息的时间戳列表(供错误显性化展示)。
    """
    groups, cur, last_ts, err_ts = [], None, None, []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = parse_ts(r.get("timestamp"))
        if r.get("type") == "assistant":
            m = r.get("message") or {}
            u = m.get("usage") or {}
            mid, out = m.get("id"), u.get("output_tokens") or 0
            mdl = m.get("model")
            # API 错误合成消息(model='<synthetic>')不成组;但其时间戳仍更新锚点:
            # 报错后重试的响应,从错误时刻起算比从原始请求起算更贴近真实耗时。
            if mdl == "<synthetic>" or r.get("isApiErrorMessage"):
                if ts:
                    last_ts = ts
                    err_ts.append(ts)
                continue
            if cur and cur["id"] == mid:
                cur["end"] = ts or cur["end"]
                cur["out"] = max(cur["out"], out)
                cur["inp"] = max(cur["inp"], u.get("input_tokens") or 0)
                cur["cr"] = max(cur["cr"], u.get("cache_read_input_tokens") or 0)
                cur["cc"] = max(cur["cc"], u.get("cache_creation_input_tokens") or 0)
            elif mid:
                if cur:
                    groups.append(cur)
                cur = {"id": mid, "start": last_ts, "end": ts,
                       "out": out, "model": mdl,
                       "inp": u.get("input_tokens") or 0,
                       "cr": u.get("cache_read_input_tokens") or 0,
                       "cc": u.get("cache_creation_input_tokens") or 0}
        if ts:
            last_ts = ts
    if cur:
        groups.append(cur)
    return [
        g for g in groups
        if g["start"] and g["end"] and g["end"] > g["start"] and g["out"]
    ], err_ts


def current_model_groups(groups):
    """只留最新响应所属模型的组:会话中途切模型时,不同模型的点混拟会失真。"""
    if not groups:
        return groups
    cur = groups[-1].get("model")
    return [g for g in groups if g.get("model") == cur]


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def plausible_point(out, dur):
    """样本点合理性(双侧):过慢 = 掺入「发消息前停顿」的离群组;
    过快 = 时间戳坍缩的「瞬时组」——导入/云同步的 Codex 会话会把成批记录
    写成毫秒级时间差,数千 token 挂在 0.001s 上,足以污染 Theil-Sen 中位。
    过快界与拟合接受域上限(MAX_TPS)一致。"""
    return dur / out < MAX_SEC_PER_TOK and out / dur <= MAX_TPS


def clean_points(groups):
    """组 → (out, dur) 点集,剔除不合理样本(见 plausible_point)。"""
    return [(g["out"], g["end"] - g["start"]) for g in groups
            if plausible_point(g["out"], g["end"] - g["start"])]


def ts_slope(pts):
    """Theil-Sen 中位斜率(对离群稳健)。pts=[(out,dur)];返回 b(秒/tok) 或 None。"""
    if len(pts) < FIT_MIN_SAMPLES:
        return None
    outs = [o for o, _ in pts]
    if max(outs) - min(outs) < FIT_MIN_SPAN:
        return None
    slopes = [(pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0])
              for i in range(len(pts)) for j in range(i + 1, len(pts))
              if abs(pts[j][0] - pts[i][0]) >= FIT_MIN_PAIR_DX]
    if len(slopes) < 3:
        return None
    b = median(slopes)
    if b <= 0 or not 3 <= 1.0 / b <= MAX_TPS:
        return None
    return b


def fit_speed(groups):
    """把一批响应的(out, 端到端耗时)拆成纯生成速度与首字延迟。

    模型 dur ≈ TTFT + out/TPS:斜率=1/TPS(纯生成速度), 截距=TTFT。
    这样消掉「速度随回复长短波动」的伪波动,只留真正在抖的 TTFT。

    返回 (tps, ttft) 拆分成功 / (tps, None) 下界近似 / None 数据不足。
    """
    pts = clean_points(groups)
    b = ts_slope(pts)
    if b is not None:
        return 1.0 / b, max(0.0, median([y - b * x for x, y in pts]))
    # 回退:长回复的 blended 速度。注意它是真 TPS 的严格下界(分母含 TTFT):
    # 300tok/真速60/TTFT5s 时只显 30,虚低一半。故语义是「≥」,调用方须按下界展示。
    long_tps = [o / d for o, d in pts if o >= 300]
    if long_tps:
        return median(long_tps), None
    return None


def windowed_fit(groups, now):
    """滑动窗口拟合:优先只用近期样本(服务端负载随时段漂移,旧样本会抹平当下波动),
    拆不出则窗口倍增扩张直至覆盖全部样本。

    返回 (fit, span):fit 同 fit_speed;span 为完整拆分实际用到的样本时间跨度(秒),
    近似/失败时为 None。
    """
    w = FIT_WINDOW_START
    while True:
        sub = [g for g in groups if now - g["end"] <= w]
        if len(sub) == len(groups):
            f = fit_speed(groups)
            ok = f is not None and f[1] is not None
            span = now - min((g["end"] for g in groups), default=now)
            return f, (span if ok else None)
        f = fit_speed(sub)
        if f is not None and f[1] is not None:
            return f, now - min(g["end"] for g in sub)
        w *= 2


def subagent_paths(transcript_path):
    """主 transcript 路径 → 其子代理 transcript 列表,覆盖两种布局:
    - Agent 工具:   <会话uuid>/subagents/agent-*.jsonl
    - Workflow 编排: <会话uuid>/subagents/workflows/<runid>/agent-*.jsonl"""
    if not transcript_path:
        return []
    stem = (transcript_path[:-6] if transcript_path.endswith(".jsonl")
            else transcript_path)
    sub = os.path.join(stem, "subagents")
    return sorted(glob.glob(os.path.join(sub, "agent-*.jsonl")) +
                  glob.glob(os.path.join(sub, "workflows", "*", "agent-*.jsonl")))


def agent_metrics(paths, now, parse=None):
    """子代理聚合 → (活跃代理数, 后台总吞吐 Σtok/s, 可入池样本点)。

    活跃 = 文件在 AGENT_ACTIVE_WINDOW 内有写入(生成中的代理会持续写)。
    吞吐 = 活跃代理近 AGENT_BURN_WINDOW 内产出的 token / 窗口时长(舰队
    燃烧率,含在途未完成的组;跨窗口的长响应按时间占比折算,不整组记入)。
    样本点 = (model, out, dur),供两阶段拟合入池。每轮最多读
    AGENT_MAX_READ 个最新文件控制 IO,超出的只计数不读——吞吐会相应低估。
    parse = lines→groups 提取器,默认 Claude transcript;Kimi 子代理传 wire 解析。
    """
    stamped = []
    for p in paths:
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if now - mt <= AGENT_ACTIVE_WINDOW:
            stamped.append((mt, p))
    stamped.sort(reverse=True)
    active, out_sum, pts = len(stamped), 0.0, []
    for _, p in stamped[:AGENT_MAX_READ]:
        groups = parse(tail_lines(p)) if parse else response_groups(tail_lines(p))[0]
        for g in groups:
            d = g["end"] - g["start"]
            if plausible_point(g["out"], d):
                pts.append((g.get("model"), g["out"], d))
            if now - g["end"] <= AGENT_BURN_WINDOW:
                frac = min(1.0, (g["end"] - max(g["start"], now - AGENT_BURN_WINDOW)) / d)
                out_sum += g["out"] * frac
    return active, out_sum / AGENT_BURN_WINDOW, pts


def fmt_dur(ms):
    s = int(ms / 1000)
    return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


def speed_parts(info, now):
    """速度相关片段(不含模型名/ctx/API 时长),供 --segment 嵌入既有状态栏。

    返回 ANSI 片段列表;无任何响应数据时返回 []。"""
    parts = []
    groups, err_ts = response_groups(tail_lines(info.get("transcript_path")))
    fit, win = windowed_fit(current_model_groups(groups), now)
    if fit is None:
        if groups:  # 有响应但样本不足以拆分:只报最近一条的规模,不虚报速度
            g = groups[-1]
            parts.append(paint("dim", "⚡速度样本不足") + " " +
                         paint("text", "最近%dtok·%.0fs" % (g["out"], g["end"] - g["start"])))
    else:
        tps, ttft = fit
        # 纯生成速度阈值(已剥离 TTFT): 绿≥50 黄≥30 红<30
        if ttft is None:
            # 回退近似是下界:下界过绿线才敢亮绿,否则暗色显示「不确定」
            parts.append(paint("ok" if tps >= 50 else "dim", "⚡≥%.0f tok/s" % tps))
        else:  # 首字延迟阈值: 绿≤5s 黄≤12s 红>12s
            sk = "ok" if tps >= 50 else ("warn" if tps >= 30 else "bad")
            tk = "ok" if ttft <= 5 else ("warn" if ttft <= 12 else "bad")
            seg = (paint(sk, "⚡%.0f tok/s" % tps) + " " +
                   paint(tk, "首字%.1fs" % ttft))
            if win and win > FIT_WINDOW_START:  # 扩窗才标口径,常态不占宽
                seg += paint("dim", "·近%d分" % round(win / 60))
            parts.append(seg)
        g = groups[-1]
        denom = g["inp"] + g["cr"] + g["cc"]
        if denom > 0:
            hit = 100.0 * g["cr"] / denom
            hk = "ok" if hit >= 100 * CACHE_OK else ("warn" if hit >= 50 else "bad")
            parts.append(paint(hk, "缓存%.0f%%%s" % (hit, "冷" if g["cc"] > g["cr"] else "")))
        parts.append(paint("text", "最近%dtok·%.0fs" % (g["out"], g["end"] - g["start"])))

    n_ag, burn, _ = agent_metrics(subagent_paths(info.get("transcript_path")), now)
    if n_ag:  # 后台子代理在跑:代理数 + 舰队燃烧率
        parts.append(paint("info", "🤖%d Σ%.0ftok/s" % (n_ag, burn)))

    nerr = sum(1 for e in err_ts if now - e <= ERR_ROW_WINDOW)
    if nerr:
        parts.append(paint("bad", "⚠️%d错" % nerr))
    return parts


# 片段分隔符;嵌入宿主状态栏时可用 CLAUDE_SPEED_SEP 对齐宿主风格(如 " │ ")
SEP = os.environ.get("CLAUDE_SPEED_SEP") or " \033[2m|\033[0m "

# 调色板:默认 ANSI 16 色;嵌入时宿主可用 CLAUDE_SPEED_PALETTE 传自己的序列
# (键=值,以 | 分隔,值原样使用,如 "ok=\033[38;2;0;175;80m|dim=\033[2m")。
# 值里允许写字面 \033(宿主用 printf %b 输出时再转义),也允许真 ESC 字符。
PALETTE = {"ok": "\033[32m", "warn": "\033[33m", "bad": "\033[31m",
           "info": "\033[36m", "dim": "\033[2m", "text": "", "reset": "\033[0m"}
for _kv in (os.environ.get("CLAUDE_SPEED_PALETTE") or "").split("|"):
    _k, _sep, _v = _kv.partition("=")
    if _sep and _k.strip() in PALETTE:
        PALETTE[_k.strip()] = _v


def paint(key, text):
    """按调色板着色;text 键(正文色)为空时不包裹,免得多余的 reset。"""
    c = PALETTE.get(key, "")
    return c + text + PALETTE["reset"] if c else text


def main(argv=None):
    """默认:完整状态栏(模型 | 速度… | API 时长 | ctx)。
    --segment:只输出速度片段,供既有状态栏脚本拼接;无数据时不输出任何字符。"""
    argv = sys.argv[1:] if argv is None else argv
    try:
        info = json.load(sys.stdin)
    except ValueError:
        info = {}
    now = time.time()
    if "--segment" in argv:
        parts = speed_parts(info, now)
        if parts:
            print(SEP.join(parts))
        return

    parts = []
    model = ((info.get("model") or {}).get("display_name") or "").strip()
    if model:
        parts.append("\033[1m%s\033[0m" % model)  # 独立模式下模型名加粗
    parts.extend(speed_parts(info, now))
    api_ms = (info.get("cost") or {}).get("total_api_duration_ms")
    if api_ms:
        parts.append("API %s" % fmt_dur(api_ms))
    pct = (info.get("context_window") or {}).get("used_percentage")
    if pct is not None:
        ck = "bad" if pct >= 85 else ("warn" if pct >= 60 else "text")
        parts.append(paint(ck, "ctx %.0f%%" % pct))

    print(SEP.join(parts) if parts else "⚡ 暂无速度数据")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # --segment 嵌入模式下宁可空白,别往宿主状态栏塞错误文本
        if "--segment" not in sys.argv[1:]:
            print("⚡ n/a")
