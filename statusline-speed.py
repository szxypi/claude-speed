#!/usr/bin/env python3
"""Claude Code statusline:显示上一条响应的实测生成速度。

数据来源两处:
- stdin:Claude Code 每次更新喂进来的会话 JSON(model / cost / context_window / transcript_path);
- transcript JSONL:同一 message.id 的连续 assistant 记录 = 一次 API 响应,
  用「组内最后时间戳 - 组前一条记录时间戳」近似该响应的端到端耗时(含 TTFT)。
  fit_speed 用 Theil-Sen 回归拆出纯生成速度与首字延迟,详见 README。
"""
import json
import os
import sys
import time
from datetime import datetime

TAIL_BYTES = 400_000  # transcript 可达几十 MB,只读尾部足够覆盖最近若干条响应

# 速度拆分参数(见 fit_speed,与 collect.py 完全一致)
MAX_SEC_PER_TOK = 0.5   # dur/out>0.5s(<2tok/s)的组基本掺了「发消息前的停顿」,剔除
FIT_MIN_SAMPLES = 5     # 拆分所需最少有效响应数
FIT_MIN_SPAN = 150      # output token 跨度需 ≥ 此值,回归才有信息量
FIT_MIN_PAIR_DX = 50    # Theil-Sen 只取 x 差 ≥ 此值的点对,避免小分母放大噪声
FIT_WINDOW_START = 600  # 滑动窗口起步(秒):拟合优先用近期样本,不足自动倍增扩窗
ERR_ROW_WINDOW = 1800   # 统计近 30 分钟的 API 错误数
CACHE_OK = 0.9          # 缓存命中率 ≥ 此值视为常态,不显示


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


def clean_points(groups):
    """组 → (out, dur) 点集,剔除掺入「发消息前停顿」的离群组。"""
    return [(g["out"], g["end"] - g["start"]) for g in groups
            if (g["end"] - g["start"]) / g["out"] < MAX_SEC_PER_TOK]


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
    if b <= 0 or not 3 <= 1.0 / b <= 400:
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


def fmt_dur(ms):
    s = int(ms / 1000)
    return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


def main():
    try:
        info = json.load(sys.stdin)
    except ValueError:
        info = {}
    parts = []
    now = time.time()

    model = ((info.get("model") or {}).get("display_name") or "").strip()
    if model:
        parts.append("\033[1m%s\033[0m" % model)

    groups, err_ts = response_groups(tail_lines(info.get("transcript_path")))
    fit, win = windowed_fit(current_model_groups(groups), now)
    if fit is None:
        if groups:  # 有响应但样本不足以拆分:只报最近一条的规模,不虚报速度
            g = groups[-1]
            parts.append("\033[2m速度样本不足\033[0m 最近%dtok·%.0fs"
                         % (g["out"], g["end"] - g["start"]))
    else:
        tps, ttft = fit
        # 纯生成速度阈值(已剥离 TTFT): 绿≥50 黄≥30 红<30
        if ttft is None:
            # 回退近似是下界:下界过绿线才敢亮绿,否则暗色显示「不确定」
            sc = "32" if tps >= 50 else "2"
            parts.append("\033[%sm⚡≥%.0f tok/s\033[0m" % (sc, tps))
        else:  # 首字延迟阈值: 绿≤5s 黄≤12s 红>12s
            sc = "32" if tps >= 50 else ("33" if tps >= 30 else "31")
            tc = "32" if ttft <= 5 else ("33" if ttft <= 12 else "31")
            seg = ("\033[%sm⚡%.0f tok/s\033[0m \033[%sm首字%.1fs\033[0m"
                   % (sc, tps, tc, ttft))
            if win and win > FIT_WINDOW_START:  # 扩窗才标口径,常态不占宽
                seg += "\033[2m·近%d分\033[0m" % round(win / 60)
            parts.append(seg)
        g = groups[-1]
        denom = g["inp"] + g["cr"] + g["cc"]
        if denom > 0 and g["cr"] / denom < CACHE_OK:  # 常态(≥90%)不显示
            hit = 100.0 * g["cr"] / denom
            hc = "33" if hit >= 50 else "31"
            parts.append("\033[%sm缓存%.0f%%%s\033[0m"
                         % (hc, hit, "冷" if g["cc"] > g["cr"] else ""))
        parts.append("最近%dtok·%.0fs" % (g["out"], g["end"] - g["start"]))

    nerr = sum(1 for e in err_ts if now - e <= ERR_ROW_WINDOW)
    if nerr:
        parts.append("\033[31m⚠️%d错\033[0m" % nerr)

    api_ms = (info.get("cost") or {}).get("total_api_duration_ms")
    if api_ms:
        parts.append("API %s" % fmt_dur(api_ms))
    pct = (info.get("context_window") or {}).get("used_percentage")
    if pct is not None:
        cc = "31" if pct >= 85 else ("33" if pct >= 60 else "0")
        parts.append("\033[%smctx %.0f%%\033[0m" % (cc, pct))

    print(" \033[2m|\033[0m ".join(parts) if parts else "⚡ 暂无速度数据")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("⚡ n/a")
