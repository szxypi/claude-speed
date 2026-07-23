#!/usr/bin/env python3
"""重新生成金标准夹具(tests/fixtures/*.jsonl)。

这些冻结的 transcript 是本项目的**测量标准**——相当于千克原器。它们以字节形式
提交入库;测试读取它们、把估计器输出与 tests/golden.json 的登记值比对。

只在**创建或扩充**夹具时运行本脚本,绝不在常规测试里跑。改完后运行
regen_golden.py 更新登记表,并 review golden.json 的 diff(读数变化必须留痕)。

设计取舍:
- 全部合成,不含任何真实 transcript 内容(隐私);
- 每个稳态夹具带**已知真值**(生成时的 TPS/TTFT),故登记表能同时暴露估计器偏差;
- 病理夹具编码评审中发现的真实失效模式(时间戳坍缩的瞬时组、未收尾的跨轮合并),
  以「稳态样本 + 毒样本」构造:估计器正确时毒样本被中和、读数落在真值上;
  一旦回归,读数漂移 → 测试变红。

时间戳锚定在固定纪元(fit_speed 只看 end−start,与 now 无关),故夹具完全确定。
"""
import json
import os
from datetime import datetime, timedelta, timezone

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
REF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(off):
    return (REF + timedelta(seconds=off)).isoformat().replace("+00:00", "Z")


# ---------- Claude transcript 记录 ----------

def c_user(off):
    return {"type": "user", "timestamp": iso(off)}


def c_asst(off, out, mid, model="claude-fable-5", inp=100, cr=9000, cc=50):
    return {"type": "assistant", "timestamp": iso(off),
            "message": {"id": mid, "model": model,
                        "usage": {"output_tokens": out, "input_tokens": inp,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cc}}}


def claude_steady(tps, ttft, outs, model="claude-fable-5", gap=60, t0=0):
    """稳态会话:每条响应 dur = ttft + out/tps(端到端,含 TTFT)。"""
    recs, t = [], t0
    for i, out in enumerate(outs):
        recs.append(c_user(t))
        recs.append(c_asst(t + ttft + out / tps, out, "m%d" % (t0 + i), model=model))
        t += gap
    return recs


# 确定性抖动(不用随机,保持夹具可复现):打破完美线性,让点对斜率分散,
# 这样对稳健估计器内部(中位 vs 均值、点对筛选)的改动也会移动读数被抓到。
JITTER = [0.7, -0.5, 0.3, -0.8, 0.6, -0.4, 0.9, -0.6, 0.2, -0.3]


def claude_noisy(tps, ttft, outs, model="claude-fable-5", gap=60):
    """稳态 + 确定性抖动:dur = ttft + out/tps + jitter。读数≈真值但非精确。"""
    recs, t = [], 0
    for i, out in enumerate(outs):
        recs.append(c_user(t))
        dur = ttft + out / tps + JITTER[i % len(JITTER)]
        recs.append(c_asst(t + dur, out, "n%d" % i, model=model))
        t += gap
    return recs


def claude_mixed():
    """会话中途切模型:opus 段(55/6)在前,fable 段(70/5)在后。
    current_model_groups 应只留 fable,读数=fable 真值。"""
    return (claude_steady(55, 6, [120, 350, 700, 1200, 1900, 2700],
                          model="claude-opus-4-8", gap=60, t0=0)
            + claude_steady(70, 5, [100, 300, 600, 1000, 1800, 2600],
                            model="claude-fable-5", gap=60, t0=6))


# ---------- Codex rollout 记录 ----------

def cx(off, typ, ptyp=None, **payload):
    r = {"timestamp": iso(off), "type": typ}
    if ptyp is not None:
        payload["type"] = ptyp
    if payload:
        r["payload"] = payload
    return r


def cx_tc(off, out, inp=10000, cached=9000, cw=100):
    return cx(off, "event_msg", "token_count",
              info={"last_token_usage": {
                  "input_tokens": inp, "cached_input_tokens": cached,
                  "cache_write_input_tokens": cw, "output_tokens": out}})


def codex_turn(t, out, tps, ttft, closed=True):
    """一轮:user_message → reasoning(首内容) → agent_message(末内容) → token_count。
    closed=False 时省略 token_count(模拟未收尾的轮次)。"""
    end = t + ttft + out / tps
    recs = [cx(t, "event_msg", "user_message"),
            cx(t + 0.5, "response_item", "reasoning"),
            cx(end, "event_msg", "agent_message")]
    if closed:
        recs.append(cx_tc(end + 0.8, out))
    return recs


def codex_steady(tps, ttft, outs, model="gpt-5.6-sol", cwd="/Users/x/proj", gap=90):
    recs = [cx(-1, "session_meta", None, cwd=cwd),
            cx(-1, "turn_context", None, model=model)]
    t = 0
    for out in outs:
        recs += codex_turn(t, out, tps, ttft)
        t += gap
    return recs


def codex_instant_poison(t):
    """时间戳坍缩的瞬时组:2000 token 挤在 1ms 内 → out/dur≈2e6,应被过滤。"""
    return [cx(t, "event_msg", "user_message"),
            cx(t + 0.0005, "response_item", "reasoning"),
            cx(t + 0.001, "event_msg", "agent_message"),
            cx_tc(t + 0.0015, 2000)]


def codex_crossturn_poison(t, tps, ttft):
    """未收尾的轮 + 真·轮次边界 + 一条干净轮。
    正确解析:残轮丢弃、干净轮(落在稳态线上)保留;回归则两轮合并成慢离群。"""
    return (codex_turn(t, 800, tps, ttft, closed=False)          # 无 token_count
            + codex_turn(t + 100, 500, tps, ttft, closed=True))   # 边界 + 干净轮


# ---------- 夹具集 ----------

def build():
    return {
        # 主标定:两种速率regime,验证估计器在不同档位都无偏
        "claude-steady-70x5": claude_steady(70, 5, [100, 300, 600, 1000, 1800, 2600]),
        "claude-steady-45x8": claude_steady(45, 8, [120, 350, 700, 1200, 2000, 3000]),
        # 带确定性噪声:非完美线性,读数对稳健估计器内部改动敏感(drift 防线咬得住)
        "claude-noisy-70x5": claude_noisy(
            70, 5, [110, 260, 520, 880, 1350, 1900, 2500, 3100]),
        # 下界回退路径:仅 3 条长回复(<5 样本),读数应为 (blended中位, None)
        "claude-fallback-lowerbound": claude_steady(70, 5, [400, 650, 900]),
        # 分模型过滤:混模型会话,读数应=当前(fable)模型
        "claude-mixed-model": claude_mixed(),
        # Codex 主标定
        "codex-steady-90x4": codex_steady(90, 4, [150, 400, 800, 1400, 2200, 3000]),
        # 病理:瞬时组过滤(plausible_point 过快界)
        "codex-instant-group": (codex_steady(90, 4, [150, 400, 800, 1400, 2200, 3000])
                                + codex_instant_poison(700)),
        # 病理:跨轮合并丢弃(轮次边界)
        "codex-cross-turn": (codex_steady(90, 4, [150, 400, 800, 1400, 2200, 3000])
                             + codex_crossturn_poison(700, 90, 4)),
    }


def main():
    os.makedirs(FIX, exist_ok=True)
    for name, recs in build().items():
        with open(os.path.join(FIX, name + ".jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("wrote", name + ".jsonl", "(%d records)" % len(recs))


if __name__ == "__main__":
    main()
