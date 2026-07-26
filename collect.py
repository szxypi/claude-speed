#!/usr/bin/env python3
"""菜单栏速度采集:扫描 ~/.claude/projects 下近期活跃的会话 transcript。

输出协议(给 ClaudeSpeed 菜单栏程序,每 3s 调一次):
  第 1 行 = 菜单栏标题;其余行 = 下拉菜单内容。

测速核心与 statusline-speed.py 完全一致:同一 message.id 的连续 assistant
记录 = 一次 API 响应(端到端耗时含 TTFT);fit_speed 用 Theil-Sen 回归拆出
纯生成速度(斜率倒数)与首字延迟(截距),详见 README。
本文件额外做:多会话聚合、跨会话共享斜率(两阶段拟合)、实时等待指示。
"""
import glob
import json
import os
import time
from datetime import datetime

ROOT = os.path.expanduser("~/.claude/projects")
SCAN_WINDOW = 2 * 3600  # 下拉列出近 2 小时内有写入的会话
TITLE_WINDOW = 10 * 60  # 最新响应超过 10 分钟,标题速度位显示为闲置
MAX_SESSIONS = 4        # 下拉最多列几个会话(Claude 与 Codex 合并后取最活跃的)
TAIL_BYTES = 400_000
CODEX_ROOT = os.path.expanduser("~/.codex/sessions")  # Codex CLI 会话目录
# Kimi Code 会话目录(数据根可用 KIMI_CODE_HOME 重定位,见官方 data-locations 文档)
KIMI_ROOT = os.path.join(
    os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code"),
    "sessions")

# 速度拆分参数(见 fit_speed,与 statusline-speed.py 完全一致)
MAX_SEC_PER_TOK = 0.5   # dur/out>0.5s(<2tok/s)的组基本掺了「发消息前的停顿」,剔除
MAX_TPS = 400           # TPS 合理上限:拟合接受域与样本过快界共用(见 plausible_point)
FIT_MIN_SAMPLES = 5     # 拆分所需最少有效响应数
FIT_MIN_SPAN = 150      # output token 跨度需 ≥ 此值,回归才有信息量
FIT_MIN_PAIR_DX = 50    # Theil-Sen 只取 x 差 ≥ 此值的点对,避免小分母放大噪声
FIT_WINDOW_START = 600  # 滑动窗口起步(秒):拟合优先用近期样本,不足自动倍增扩窗
WAIT_MIN = 3            # 最后记录是 user 且距今超过此秒数 → 正在等待响应
WAIT_MAX = 120          # 等待超过此秒数视为请求已被中断,不再显示 ⏳
ERR_TITLE_WINDOW = 300  # 最新会话近 5 分钟内有 API 错误 → 标题挂 ⚠️
ERR_ROW_WINDOW = 1800   # 下拉行统计近 30 分钟的 API 错误数
CACHE_OK = 0.9          # 缓存命中率颜色阈值(statusline 绿档)
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
                f.readline()
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


def last_record_info(lines):
    """尾部最后一条 user/assistant 记录 → (type, 时间戳)。
    只认这两类:用户带附件发消息时尾部是 attachment 记录(也带时间戳),
    system/progress/summary 等辅助记录同理,都不能代表「谁在等谁」。"""
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = parse_ts(r.get("timestamp"))
        if ts and r.get("type") in ("user", "assistant"):
            return r.get("type"), ts
    return None, None


def current_model_groups(groups):
    """只留最新响应所属模型的组:会话中途切模型时,不同模型的点混拟会失真。"""
    if not groups:
        return groups
    cur = groups[-1].get("model")
    return [g for g in groups if g.get("model") == cur]


def model_tag(mid):
    """'claude-opus-4-8'→'opus4.8'  'claude-fable-5'→'fable5'
    非 claude 系(如 Codex 的 'gpt-5.6-sol'→'gpt5.6sol')去连字符截短。"""
    parts = [p for p in (mid or "").split("-") if p]
    if not parts:
        return ""
    if parts[0] == "claude":
        parts = parts[1:]
        if not parts:
            return ""
        nums = [x for x in parts[1:] if x.isdigit() and len(x) <= 2]
        return parts[0] + (nums[0] + ("." + nums[1] if len(nums) > 1 else "")
                           if nums else "")
    return "".join(parts)[:12]


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


def recent_files():
    """近期活跃会话。活跃时间取 max(主transcript mtime, 最新子代理 mtime)——
    后台任务跑着时主 transcript 静默,只看主文件会把正在烧的会话判成闲置/挤出榜。
    返回 [(活跃时间, 主路径, 项目目录名, 子代理路径列表)]。"""
    now, found = time.time(), []
    try:
        dirs = list(os.scandir(ROOT))
    except OSError:
        return []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(os.scandir(d.path))
        except OSError:
            continue
        for f in entries:
            # agent-*.jsonl 是子代理 transcript,不当独立会话(经 subagent_paths 归属)
            if not f.name.endswith(".jsonl") or f.name.startswith("agent-"):
                continue
            try:
                eff = f.stat().st_mtime
            except OSError:
                continue
            agents = subagent_paths(f.path)
            for a in agents:
                try:
                    eff = max(eff, os.path.getmtime(a))
                except OSError:
                    continue
            if now - eff < SCAN_WINDOW:
                found.append((eff, f.path, d.name, agents))
    found.sort(key=lambda t: t[0], reverse=True)
    return found[:MAX_SESSIONS]


# 标签里无意义的路径段(不含项目身份信息)
LABEL_NOISE = {"users", "documents", "work", "tools", "home", "worktrees",
               "desktop", "downloads", "projects", "repos", "src", "code",
               (os.environ.get("USER") or "").lower()}


def project_label(dirname):
    """目录名转短标签。目录名是路径转的('/'→'-'),朴素取最后一段会把
    worktree/临时目录显示成随机 hash(如 aec861)或撞车的通用词(如 cwd)。
    策略:剥掉纯数字短尾段与含数字的 hex 尾段(随机后缀),再取最后一两段。"""
    parts = [p for p in dirname.split("-") if p]
    while parts and (
        (parts[-1].isdigit() and len(parts[-1]) <= 4)  # 长数字可能是日期型项目名,保留
        or (len(parts[-1]) >= 6  # hex hash 须字母数字混合,纯数字(如日期)不算
            and any(c.isdigit() for c in parts[-1])
            and any(c.isalpha() for c in parts[-1])
            and all(c in "0123456789abcdef" for c in parts[-1].lower()))
    ):
        parts.pop()
    if not parts:
        return dirname[-12:]
    if (len(parts) >= 2 and parts[-2].lower() not in LABEL_NOISE
            and len(parts[-2]) + len(parts[-1]) < 20):
        return parts[-2] + "-" + parts[-1]
    return parts[-1]


# ---- Codex CLI 数据源(仅 collect;statusline 是 Claude Code 的 UI,够不着 Codex) ----

# rollout 记录里属于「模型生成内容」的 payload 类型:属于在途响应的一部分。
# 其余类型(user_message/tool输出/token_count/turn_context...)只当时间锚点。
CODEX_CONTENT_RI = {"reasoning", "message", "custom_tool_call", "function_call",
                    "web_search_call", "local_shell_call"}
CODEX_CONTENT_EM = {"agent_reasoning", "agent_reasoning_delta", "agent_message"}
CODEX_TRIGGER_EM = {"user_message", "task_started"}


def codex_parse(lines):
    """解析 Codex rollout JSONL → (groups, err_ts, label, last_trigger_ts)。

    组语义与 response_groups 对齐:连续的内容记录 = 一次在途响应,
    `token_count` 事件收尾并提供 usage。锚点规则:
    - start = 组第一条内容记录的前一条记录时间戳(触发它的 user/tool 输出,含 TTFT);
    - end = 最后一条内容记录的时间戳——不能用 token_count 的:它在工具执行完
      之后才发出,会把工具耗时算进生成时间。
    usage 映射:inp=input−cached(与 Claude 的「非缓存输入」口径一致)、
    cr=cached、cc=cache_write。模型来自最近的 turn_context,标签来自
    session_meta.cwd。last_trigger_ts 供等待检测(内容一到就清零,
    task_complete 也清零)。
    """
    groups, err_ts = [], []
    cur, last_ts = None, None
    model, label, last_trigger_ts = None, "", None
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = parse_ts(r.get("timestamp"))
        t, p = r.get("type"), r.get("payload") or {}
        pt = p.get("type")
        if t == "session_meta" and p.get("cwd"):
            label = os.path.basename(str(p["cwd"]).rstrip("/")) or label
        elif t == "turn_context" and p.get("model"):
            model = p["model"]
        is_content = ts and (
            (t == "response_item" and pt in CODEX_CONTENT_RI
             and not (pt == "message" and p.get("role") != "assistant"))
            or (t == "event_msg" and pt in CODEX_CONTENT_EM))
        if is_content:
            if cur is None:
                cur = {"start": last_ts, "end": ts}
            else:
                cur["end"] = ts
            last_trigger_ts = None
        elif t == "event_msg" and pt == "token_count":
            u = (p.get("info") or {}).get("last_token_usage") or {}
            out = u.get("output_tokens") or 0
            if cur and cur["start"] and out:
                cr = u.get("cached_input_tokens") or 0
                # 大文件尾读可能不含稀疏的 turn_context → model 未知时用
                # "codex" 哨兵:显示上有归属,两阶段点池也不会与 Claude 的
                # 无模型旧格式(None)混池
                groups.append({"id": None, "start": cur["start"],
                               "end": cur["end"], "out": out,
                               "model": model or "codex",
                               "inp": max(0, (u.get("input_tokens") or 0) - cr),
                               "cr": cr,
                               "cc": u.get("cache_write_input_tokens") or 0})
            cur = None
        elif t == "event_msg" and pt in ("error", "stream_error"):
            if ts:
                err_ts.append(ts)
        elif ts and t == "event_msg" and pt in CODEX_TRIGGER_EM:
            # 真·轮次边界(用户消息/任务开始):未被 token_count 收尾的残组直接
            # 丢弃——否则下一轮内容会并进同一组,把用户思考时间算进耗时,
            # 拟合出「大 dur 中等 out」的假慢点
            cur = None
            last_trigger_ts = ts
        elif ts and t == "response_item" and pt == "custom_tool_call_output":
            last_trigger_ts = ts  # 轮内触发(工具输出):组保持开放,工具耗时由 end 锚点排除
        elif t == "event_msg" and pt == "task_complete":
            last_trigger_ts = None
        if ts:
            last_ts = ts
    return ([g for g in groups if g["start"] and g["end"] > g["start"]],
            err_ts, label, last_trigger_ts)


def codex_recent_files():
    """近期活跃的 Codex 会话(布局 sessions/YYYY/MM/DD/rollout-*.jsonl)。"""
    now, found = time.time(), []
    for p in glob.glob(os.path.join(CODEX_ROOT, "*", "*", "*", "rollout-*.jsonl")):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if now - mt < SCAN_WINDOW:
            found.append((mt, p))
    found.sort(reverse=True)
    return found[:MAX_SESSIONS]


# ---- Kimi Code 数据源(仅 collect;wire.jsonl 是未文档化内部格式,解析须防御) ----


def kimi_ts(v):
    """wire 记录的 time 字段是 epoch 毫秒字符串。"""
    try:
        return float(v) / 1000
    except (TypeError, ValueError):
        return None


def kimi_parse(lines):
    """解析 Kimi Code wire.jsonl → (groups, err_ts, last_trigger_ts)。

    组语义与 response_groups 对齐:一次 API 响应 = llm.request → 紧随的
    step.end 配对。锚点规则:
    - start = llm.request.time(请求发出时刻,语义同 Claude 的「组前一条记录」);
    - end = step.end.time(响应完整消费,端到端含 TTFT)。
    重试(服务端错误后重新请求):新的 llm.request 到来时丢弃未闭合的旧
    request,锚点取重试时刻——同 Claude 的错误锚点哲学。
    usage 映射:inp=inputOther、cr=inputCacheRead、cc=inputCacheCreation
    (与 Claude 的非缓存输入/缓存读/缓存写一一对应)。模型取 llm.request.model
    (如 "k3"),与 Claude/Codex 的模型名不撞池。
    err_ts:错误记录格式未实测到,防御性收集任何 type 含 error 的记录。
    last_trigger_ts:尾部是 turn.prompt/llm.request(已发请求、首个内容
    尚未返回)时置位,供等待检测;内容一到或 step.end 落地即清零。
    """
    groups, err_ts = [], []
    cur, last_trigger_ts = None, None
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t, ts = r.get("type"), kimi_ts(r.get("time"))
        et = (r.get("event") or {}).get("type") if t == "context.append_loop_event" else None
        if t == "llm.request":
            cur = {"start": ts, "model": r.get("model") or "kimi"}
            if ts:
                last_trigger_ts = ts
        elif t == "turn.prompt":
            if ts:
                last_trigger_ts = ts
        elif et == "step.end":
            u = (r.get("event") or {}).get("usage") or {}
            out = u.get("output") or 0
            if cur and cur["start"] and ts and out:
                groups.append({"id": (r.get("event") or {}).get("messageId"),
                               "start": cur["start"], "end": ts, "out": out,
                               "model": cur["model"],
                               "inp": u.get("inputOther") or 0,
                               "cr": u.get("inputCacheRead") or 0,
                               "cc": u.get("inputCacheCreation") or 0})
            cur = None
            last_trigger_ts = None
        elif et == "content.part":
            last_trigger_ts = None  # 首个返回内容到达 → 不再算「等待首字」
        if ts and ("error" in str(t).lower() or "error" in str(et).lower()):
            err_ts.append(ts)
    return ([g for g in groups if g["end"] > g["start"]],
            err_ts, last_trigger_ts)


def kimi_agent_paths(session_dir):
    """Kimi 子代理 wire:<sessionDir>/agents/agent-*/wire.jsonl(平铺布局)。"""
    return sorted(glob.glob(os.path.join(session_dir, "agents",
                                         "agent-*", "wire.jsonl")))


def kimi_session_labels():
    """session_index.jsonl → {sessionDir: 项目短标签}(workDir 的 basename)。"""
    labels = {}
    try:
        with open(os.path.join(os.path.dirname(KIMI_ROOT),
                               "session_index.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sd, wd = r.get("sessionDir"), r.get("workDir")
                if sd and wd:
                    labels[sd] = os.path.basename(str(wd).rstrip("/"))
    except OSError:
        pass
    return labels


def kimi_recent_files():
    """近期活跃的 Kimi Code 会话(布局 sessions/<wd_key>/<sessionId>/agents/main/wire.jsonl)。
    活跃时间取 max(主 wire, 最新子代理 wire)——同 Claude 的 recent_files:
    后台子代理跑着时主 wire 静默,只看主文件会把在烧的会话判成闲置。
    返回 [(活跃时间, 主 wire 路径, 会话目录, 子代理路径列表)]。"""
    now, found = time.time(), []
    for p in glob.glob(os.path.join(KIMI_ROOT, "*", "*",
                                    "agents", "main", "wire.jsonl")):
        try:
            eff = os.path.getmtime(p)
        except OSError:
            continue
        sdir = os.path.dirname(os.path.dirname(os.path.dirname(p)))
        agents = kimi_agent_paths(sdir)
        for a in agents:
            try:
                eff = max(eff, os.path.getmtime(a))
            except OSError:
                continue
        if now - eff < SCAN_WINDOW:
            found.append((eff, p, sdir, agents))
    found.sort(key=lambda t: t[0], reverse=True)
    return found[:MAX_SESSIONS]


def kimi_label_fallback(sdir):
    """session_index 缺失时从 workDirKey 目录名推标签:wd_<slug>_<hash12> → slug。"""
    key = os.path.basename(os.path.dirname(sdir))
    if key.startswith("wd_"):
        head, sep, tail = key[3:].rpartition("_")
        if sep and len(tail) == 12 and all(c in "0123456789abcdef"
                                           for c in tail.lower()):
            return head
        return key[3:]
    return key or "kimi"


def fmt_ago(sec):
    return "%d分" % (sec // 60) if sec >= 60 else "%d秒" % sec


def lamp(tps):
    # 菜单栏无法给文字上色(⚡ 是彩色绘文字,恒黄)。改用信号灯绘文字本身的颜色
    # 编码速度档,阈值同 statusline: 绿≥50 / 黄≥30 / 红<30。
    return "🟢" if tps >= 50 else ("🟡" if tps >= 30 else "🔴")


def main():
    now = time.time()

    # ---- 扫描:每会话解析一次尾部,同时汇集跨会话共享斜率的点池 ----
    sessions = []
    pool = {}  # model -> [(out,dur)]:TPS 是模型属性,跨会话同构,可共享斜率
    for mtime, path, dname, agent_paths in recent_files():
        lines = tail_lines(path)
        groups, err_ts = response_groups(lines)
        ltype, lts = last_record_info(lines)
        wait = None  # 实时等待:最后一条是 user 记录(含 tool_result)且迟迟无响应
        if ltype == "user" and lts and WAIT_MIN < now - lts <= WAIT_MAX:
            wait = int(now - lts)
        n_ag, burn, apts = agent_metrics(agent_paths, now)
        # 入榜需有窗口内的实质活动:近期响应 / 等待中 / 近期错误 / 代理在跑。
        # 只按文件 mtime 会放进「文件被碰过但最后响应在几天前」的僵尸会话
        # (如后台 claude -p 追加了非响应记录),显示成「N千分钟前」还挤占名额。
        has_recent = bool(groups) and now - groups[-1]["end"] < SCAN_WINDOW
        has_err = any(now - e <= ERR_ROW_WINDOW for e in err_ts)
        if not has_recent and wait is None and not has_err and not n_ag:
            continue
        for g in groups:
            d = g["end"] - g["start"]
            if plausible_point(g["out"], d):
                pool.setdefault(g.get("model"), []).append((g["out"], d))
        for mdl, out, d in apts:  # 子代理响应与主链同构,并入同模型点池
            pool.setdefault(mdl, []).append((out, d))
        sessions.append({"mtime": mtime, "label": project_label(dname),
                         "groups": groups, "err_ts": err_ts, "wait": wait,
                         "agents": (n_ag, burn)})

    # ---- Codex 会话:解析出同构的组,下游流水线全部复用 ----
    for mtime, path in codex_recent_files():
        groups, err_ts, label, trig_ts = codex_parse(tail_lines(path))
        wait = None
        if trig_ts and WAIT_MIN < now - trig_ts <= WAIT_MAX:
            wait = int(now - trig_ts)
        has_recent = bool(groups) and now - groups[-1]["end"] < SCAN_WINDOW
        has_err = any(now - e <= ERR_ROW_WINDOW for e in err_ts)
        if not has_recent and wait is None and not has_err:
            continue
        for g in groups:
            d = g["end"] - g["start"]
            if plausible_point(g["out"], d):
                pool.setdefault(g.get("model"), []).append((g["out"], d))
        sessions.append({"mtime": mtime, "label": (label or "codex")[:16],
                         "groups": groups, "err_ts": err_ts, "wait": wait,
                         "agents": (0, 0.0)})

    # ---- Kimi Code 会话:wire.jsonl 解析出同构的组,下游流水线全部复用 ----
    klabels = kimi_session_labels()
    for eff, path, sdir, agent_paths in kimi_recent_files():
        groups, err_ts, trig_ts = kimi_parse(tail_lines(path))
        wait = None
        if trig_ts and WAIT_MIN < now - trig_ts <= WAIT_MAX:
            wait = int(now - trig_ts)
        n_ag, burn, apts = agent_metrics(
            agent_paths, now, parse=lambda lines: kimi_parse(lines)[0])
        # 入榜规则与 Claude 段一致:近期响应 / 等待中 / 近期错误 / 代理在跑
        has_recent = bool(groups) and now - groups[-1]["end"] < SCAN_WINDOW
        has_err = any(now - e <= ERR_ROW_WINDOW for e in err_ts)
        if not has_recent and wait is None and not has_err and not n_ag:
            continue
        for g in groups:
            d = g["end"] - g["start"]
            if plausible_point(g["out"], d):
                pool.setdefault(g.get("model"), []).append((g["out"], d))
        for mdl, out, d in apts:  # 子代理响应与主链同构,并入同模型点池
            pool.setdefault(mdl, []).append((out, d))
        sessions.append({"mtime": eff,
                         "label": (klabels.get(sdir)
                                   or kimi_label_fallback(sdir))[:16],
                         "groups": groups, "err_ts": err_ts, "wait": wait,
                         "agents": (n_ag, burn)})

    # 多源合并后按活跃时间取最活跃的 MAX_SESSIONS 个
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    sessions = sessions[:MAX_SESSIONS]

    # ---- 每会话拟合:自身滑窗拟合 → 全局斜率+会话截距(两阶段) → 下界近似 ----
    rows = []
    for s in sessions:
        groups = s["groups"]
        if not groups:  # 主链还没有成功响应:只有等待/错误/后台代理信息
            rows.append({"end": s["mtime"], "mtime": s["mtime"], "fit": None,
                         "glob": False, "win": None, "wait": s["wait"],
                         "err_ts": s["err_ts"], "label": s["label"],
                         "model": "", "last": None, "agents": s["agents"]})
            continue
        g = groups[-1]
        mg = current_model_groups(groups)
        fit, win = windowed_fit(mg, now)
        borrowed = False  # 注意别用 glob 当变量名,会遮蔽 glob 模块
        if fit is None or fit[1] is None:
            # 两阶段:斜率借同模型跨会话点池,截距(TTFT)用本会话残差中位
            b = ts_slope(pool.get(mg[-1].get("model")) or [])
            pts = clean_points(mg)
            if b is not None and len(pts) >= 2:
                fit = (1.0 / b, max(0.0, median([y - b * x for x, y in pts])))
                win, borrowed = None, True
        rows.append({"end": g["end"], "mtime": s["mtime"], "fit": fit,
                     "glob": borrowed, "win": win, "wait": s["wait"],
                     "err_ts": s["err_ts"], "label": s["label"],
                     "model": model_tag(g.get("model")), "last": g,
                     "agents": s["agents"]})
    rows.sort(key=lambda r: r["end"], reverse=True)

    # ---- 标题:⚠️(近期错误)+ 速度灯 + 🤖(后台舰队) ----
    # 等待/高首字的 ⏳ 只进下拉,不占图标栏(用户偏好:标题保持最简)
    speed_seg = ""
    for r in rows:
        if r["fit"] is not None and now - r["end"] < TITLE_WINDOW:
            tps, ttft = r["fit"]
            if ttft is None:
                # 回退近似是下界:下界过绿线才敢亮绿灯,否则亮 ⚪「不确定」
                speed_seg = "🟢≥%.0f" % tps if tps >= 50 else "⚪≥%.0f" % tps
            else:
                speed_seg = "%s%s%.0f" % (lamp(tps), "≈" if r["glob"] else "", tps)
            break
    err_flag = "⚠️" if any(now - e <= ERR_TITLE_WINDOW
                           for r in rows for e in r["err_ts"]) else ""
    # 舰队口径与 ⚠️ 一致:聚合所有在榜会话,避免「谁的 mtime 最新」的瞬时竞态
    n_ag = sum(r["agents"][0] for r in rows)
    burn = sum(r["agents"][1] for r in rows)
    if speed_seg:
        print(err_flag + speed_seg + (" 🤖%d" % n_ag if n_ag else ""))
    elif n_ag:  # 前台无读数、后台在烧:标题只显舰队状态
        print(err_flag + "🤖%d Σ%.0f" % (n_ag, burn))
    else:
        print(err_flag + "⚪" if err_flag else "⚪")

    # ---- 下拉明细 ----
    for r in rows:
        head = "%s·%s" % (r["label"], r["model"]) if r["model"] else r["label"]
        segs = []
        fit = r["fit"]
        if fit is None:
            if r["wait"] is not None:
                # 「首个」只对真·新会话(无任何历史响应)成立,否则与「最近Ntok」矛盾
                segs.append("⏳ 等待%s响应 %d秒"
                            % ("首个" if r["last"] is None else "", r["wait"]))
            elif r["last"] is None and r["agents"][0]:
                segs.append("后台任务运行中")  # 主链无响应,速度看 🤖 段
            elif r["last"] is None:
                segs.append("🔴 无成功响应")  # 全错会话:groups 空,靠 ⚠️N错 说明原因
            else:
                segs.append("⚪ 速度样本不足")
        elif fit[1] is None:
            lp = "🟢" if fit[0] >= 50 else "⚪"
            segs.append("%s ≥%.0f tok/s" % (lp, fit[0]))
        else:
            seg = "%s %s%.0f tok/s 首字%.0fs" % (
                lamp(fit[0]), "≈" if r["glob"] else "", fit[0], fit[1])
            if r["win"] and r["win"] > FIT_WINDOW_START:
                seg += "·近%d分" % round(r["win"] / 60)
            segs.append(seg)
        n_ag, burn = r["agents"]
        if n_ag:
            segs.append("🤖%d·Σ%.0ftok/s" % (n_ag, burn))
        g = r["last"]
        if g:
            denom = g["inp"] + g["cr"] + g["cc"]
            if denom > 0:
                seg = "缓存%d%%" % round(100 * g["cr"] / denom)
                if g["cc"] > g["cr"]:
                    seg += "冷"
                segs.append(seg)
        nerr = sum(1 for e in r["err_ts"] if now - e <= ERR_ROW_WINDOW)
        if nerr:
            segs.append("⚠️%d错" % nerr)
        if r["wait"] is not None and fit is not None:
            segs.append("⏳等%d秒" % r["wait"])
        if g:
            segs.append("最近%dtok·%.0fs" % (g["out"], g["end"] - g["start"]))
            segs.append("%s前" % fmt_ago(now - r["end"]))
        print("%s  %s" % (head, "  ".join(segs)))
    if not rows:
        print("近2小时无响应")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("⚡?")
