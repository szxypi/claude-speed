#!/usr/bin/env python3
"""claude-speed 测试:共享算法单元测试 + collect.main 端到端合成场景。

- 共享函数(测速核心)对 collect.py 与 statusline-speed.py 两个模块各跑一遍;
- TestScriptSync 用 AST 比对强制「两脚本算法完全一致」的纪律——
  改其中一个忘了同步另一个,这里会直接红。
运行: python3 -m unittest discover -s tests -v
"""
import ast
import contextlib
import importlib.util
import inspect
import io
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = load_module("cs_collect", "collect.py")
sl = load_module("cs_statusline", "statusline-speed.py")

# 两脚本必须逐字一致的共享部分
SHARED_FUNCS = ["parse_ts", "tail_lines", "response_groups",
                "current_model_groups", "median", "plausible_point",
                "clean_points", "ts_slope", "fit_speed", "windowed_fit",
                "subagent_paths", "agent_metrics"]
SHARED_CONSTS = ["TAIL_BYTES", "MAX_SEC_PER_TOK", "FIT_MIN_SAMPLES",
                 "FIT_MIN_SPAN", "FIT_MIN_PAIR_DX", "FIT_WINDOW_START",
                 "ERR_ROW_WINDOW", "CACHE_OK", "AGENT_ACTIVE_WINDOW",
                 "AGENT_BURN_WINDOW", "AGENT_MAX_READ", "MAX_TPS"]


# ---------- 合成 transcript 记录构造 ----------

def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def rec_a(mid, t, out, model="claude-fable-5", inp=100, cr=9000, cc=50):
    return {"type": "assistant", "timestamp": iso(t),
            "message": {"id": mid, "model": model,
                        "usage": {"output_tokens": out, "input_tokens": inp,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cc}}}


def rec_u(t):
    return {"type": "user", "timestamp": iso(t)}


def rec_err(t):
    return {"type": "assistant", "timestamp": iso(t), "isApiErrorMessage": True,
            "message": {"id": "e", "model": "<synthetic>", "usage": {}}}


def rec_att(t):
    return {"type": "attachment", "timestamp": iso(t)}


def jlines(recs):
    return [json.dumps(r) for r in recs]


def make_session(now, tps=70.0, ttft=5.0, outs=(100, 400, 800, 1500, 2500),
                 start=None, gap=60, model="claude-fable-5"):
    """构造一段「已知真值」的会话:每条响应 dur = ttft + out/tps。"""
    recs, t = [], (start if start is not None else now - 600)
    for i, out in enumerate(outs):
        recs.append(rec_u(t))
        recs.append(rec_a("m%d" % i, t + ttft + out / tps, out, model=model))
        t += gap
    return recs


# ---------- 共享算法单元测试(对两个模块各跑一遍) ----------

class SharedAlgoMixin:
    m = None  # 被测模块

    def test_grouping_merges_same_message_id(self):
        now = time.time()
        recs = [rec_u(now - 20),
                rec_a("m1", now - 15, 10), rec_a("m1", now - 12, 40),
                rec_a("m1", now - 10, 100)]
        groups, errs = self.m.response_groups(jlines(recs))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["out"], 100)          # 组内取 max
        self.assertAlmostEqual(groups[0]["start"], now - 20, delta=0.01)
        self.assertAlmostEqual(groups[0]["end"], now - 10, delta=0.01)
        self.assertEqual(errs, [])

    def test_synthetic_error_anchors_but_never_groups(self):
        now = time.time()
        recs = [rec_u(now - 30), rec_err(now - 20), rec_a("m1", now - 10, 500)]
        groups, errs = self.m.response_groups(jlines(recs))
        self.assertEqual(len(groups), 1)
        # 重试响应从错误时刻起算,不从原始请求起算
        self.assertAlmostEqual(groups[0]["start"], now - 20, delta=0.01)
        self.assertEqual(len(errs), 1)

    def test_leading_assistant_without_anchor_dropped(self):
        groups, _ = self.m.response_groups(jlines([rec_a("m1", time.time(), 100)]))
        self.assertEqual(groups, [])  # 无前置记录可当锚点,组无效

    def test_fit_recovers_known_tps_and_ttft(self):
        now = time.time()
        groups, _ = self.m.response_groups(jlines(make_session(now, tps=70, ttft=5)))
        tps, ttft = self.m.fit_speed(groups)
        self.assertAlmostEqual(tps, 70, delta=2)
        self.assertAlmostEqual(ttft, 5, delta=0.5)

    def test_fallback_is_lower_bound(self):
        now = time.time()
        recs = []
        for i in range(3):  # 3条同长回复:样本不足以回归,走 blended 下界
            t = now - 300 + i * 60
            recs += [rec_u(t), rec_a("m%d" % i, t + 10, 400)]
        groups, _ = self.m.response_groups(jlines(recs))
        fit = self.m.fit_speed(groups)
        self.assertEqual(fit, (40.0, None))  # 400tok/10s,ttft 未拆出

    def test_insufficient_returns_none(self):
        now = time.time()
        groups, _ = self.m.response_groups(
            jlines([rec_u(now - 10), rec_a("m1", now - 8, 50)]))
        self.assertIsNone(self.m.fit_speed(groups))

    def test_outlier_pause_rejected(self):
        now = time.time()
        recs = make_session(now, tps=70, ttft=5)
        # 掺一条 dur/out=1s/tok 的离群组(用户停顿混入)
        recs += [rec_u(now - 30), rec_a("mx", now - 30 + 100, 100)]
        groups, _ = self.m.response_groups(jlines(recs))
        pts = self.m.clean_points(groups)
        self.assertEqual(len(pts), 5)  # 离群组被剔除

    def test_current_model_groups_filters_old_model(self):
        now = time.time()
        recs = (make_session(now, model="claude-opus-4-8", start=now - 1200) +
                make_session(now, model="claude-fable-5", start=now - 500))
        groups, _ = self.m.response_groups(jlines(recs))
        mg = self.m.current_model_groups(groups)
        self.assertTrue(all(g["model"] == "claude-fable-5" for g in mg))
        self.assertEqual(len(mg), 5)

    def test_windowed_fit_prefers_recent_samples(self):
        now = time.time()
        # 40分钟前一批 70tok/s,近8分钟一批 35tok/s → 应报出近期的 35
        recs = (make_session(now, tps=70, ttft=5, start=now - 2400) +
                make_session(now, tps=35, ttft=5, start=now - 480))
        groups, _ = self.m.response_groups(jlines(recs))
        (tps, ttft), span = self.m.windowed_fit(groups, now)
        self.assertAlmostEqual(tps, 35, delta=2)
        self.assertLessEqual(span, self.m.FIT_WINDOW_START)

    def test_windowed_fit_expands_when_recent_insufficient(self):
        now = time.time()
        recs = make_session(now, tps=70, ttft=5, start=now - 2400, gap=30)
        groups, _ = self.m.response_groups(jlines(recs))
        (tps, _), span = self.m.windowed_fit(groups, now)
        self.assertAlmostEqual(tps, 70, delta=2)
        self.assertGreater(span, self.m.FIT_WINDOW_START)  # 扩窗才拟合成功

    def test_instant_group_rejected(self):
        # 时间戳坍缩的「瞬时组」(毫秒级 dur 挂几千 token,见于导入/云同步的
        # Codex 会话)必须被样本过滤剔除,否则污染拟合与跨会话点池
        now = time.time()
        recs = make_session(now)
        recs += [rec_u(now - 20), rec_a("mz", now - 19.999, 4380)]
        groups, _ = self.m.response_groups(jlines(recs))
        self.assertEqual(len(self.m.clean_points(groups)), 5)  # 瞬时组不在内
        self.assertFalse(self.m.plausible_point(4380, 0.001))
        self.assertTrue(self.m.plausible_point(600, 10.0))

    def test_subagent_paths_layout(self):
        root = tempfile.mkdtemp()
        main_p = os.path.join(root, "abc.jsonl")
        subdir = os.path.join(root, "abc", "subagents")
        os.makedirs(subdir)
        ap = os.path.join(subdir, "agent-x1.jsonl")
        open(ap, "w").close()
        wfdir = os.path.join(subdir, "workflows", "wf_123")
        os.makedirs(wfdir)
        wp = os.path.join(wfdir, "agent-y2.jsonl")
        open(wp, "w").close()
        self.assertEqual(self.m.subagent_paths(main_p), [ap, wp])  # 两种布局都要
        self.assertEqual(self.m.subagent_paths(None), [])

    def test_agent_metrics_active_and_burn(self):
        now = time.time()
        root = tempfile.mkdtemp()

        def agent(name, out, mtime):
            p = os.path.join(root, name)
            recs = [rec_u(now - 30), rec_a("a", now - 10, out)]
            with open(p, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            os.utime(p, (mtime, mtime))
            return p

        p1 = agent("agent-1.jsonl", 600, now - 5)    # 活跃,窗口内 600tok
        p2 = agent("agent-2.jsonl", 600, now - 5)    # 活跃,窗口内 600tok
        p3 = agent("agent-3.jsonl", 600, now - 600)  # mtime 过期 → 只当不存在
        active, burn, pts = self.m.agent_metrics([p1, p2, p3], now)
        self.assertEqual(active, 2)
        self.assertAlmostEqual(burn, 1200 / self.m.AGENT_BURN_WINDOW, delta=0.01)
        self.assertEqual(len(pts), 2)

    def test_agent_burn_prorates_window_straddling_group(self):
        # 240s 的长响应只有尾部 110s 落在燃烧窗口内:按时间占比折算,不整组记入
        now = time.time()
        root = tempfile.mkdtemp()
        p = os.path.join(root, "agent-long.jsonl")
        recs = [rec_u(now - 250), rec_a("a", now - 10, 600)]  # dur=240,0.4s/tok
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        os.utime(p, (now - 5, now - 5))
        _, burn, _ = self.m.agent_metrics([p], now)
        w = self.m.AGENT_BURN_WINDOW
        expect = 600 * ((now - 10) - (now - w)) / 240 / w  # 600×(110/240)/120
        self.assertAlmostEqual(burn, expect, delta=0.05)


class TestSharedCollect(SharedAlgoMixin, unittest.TestCase):
    m = cs


class TestSharedStatusline(SharedAlgoMixin, unittest.TestCase):
    m = sl


# ---------- 两脚本一致性(纪律测试) ----------

class TestScriptSync(unittest.TestCase):
    def test_shared_functions_ast_identical(self):
        for name in SHARED_FUNCS:
            a = ast.dump(ast.parse(textwrap.dedent(
                inspect.getsource(getattr(cs, name)))))
            b = ast.dump(ast.parse(textwrap.dedent(
                inspect.getsource(getattr(sl, name)))))
            self.assertEqual(a, b, "共享函数 %s 在两脚本中不一致" % name)

    def test_shared_constants_equal(self):
        for name in SHARED_CONSTS:
            self.assertEqual(getattr(cs, name), getattr(sl, name),
                             "共享常量 %s 在两脚本中不一致" % name)


# ---------- Codex rollout 合成记录构造 ----------

def cx(t, typ, ptyp=None, **payload):
    r = {"timestamp": iso(t), "type": typ}
    if ptyp is not None:
        payload["type"] = ptyp
    if payload:
        r["payload"] = payload
    return r


def cx_tc(t, out, inp=10000, cached=9000, cw=100):
    return cx(t, "event_msg", "token_count",
              info={"last_token_usage": {
                  "input_tokens": inp, "cached_input_tokens": cached,
                  "cache_write_input_tokens": cw, "output_tokens": out}})


def cx_session(now, tps=70.0, ttft=5.0, outs=(100, 400, 800, 1500, 2500),
               start=None, gap=60, model="gpt-5.6-sol", cwd="/Users/x/myproj"):
    """构造已知真值的 Codex 会话:每条响应 dur = ttft + out/tps。"""
    t = start if start is not None else now - 600
    recs = [cx(t - 1, "session_meta", None, cwd=cwd),
            cx(t - 1, "turn_context", None, model=model)]
    for out in outs:
        recs.append(cx(t, "event_msg", "user_message"))          # 触发(锚点)
        recs.append(cx(t + 0.5, "response_item", "reasoning"))   # 首条内容
        end = t + ttft + out / tps
        recs.append(cx(end, "event_msg", "agent_message"))       # 末条内容(组end)
        recs.append(cx_tc(end + 0.8, out))                       # usage 收尾
        t += gap
    return recs


# ---------- Kimi Code wire 合成记录构造 ----------

def km(t, typ, **fields):
    r = {"type": typ, "time": str(int(t * 1000))}  # wire 的 time 是 epoch 毫秒字符串
    r.update(fields)
    return r


def km_step(t, out, tps, ttft, model="k3", inp=100, cr=9000, cc=50):
    """一步 = 一次 API 响应:llm.request → content.part → step.end(带 usage)。"""
    end = t + ttft + out / tps
    return [km(t, "llm.request", kind="loop", model=model),
            km(t + ttft, "context.append_loop_event",
               event={"type": "content.part"}),
            km(end, "context.append_loop_event",
               event={"type": "step.end", "finishReason": "end_turn",
                      "messageId": "msg%d" % int(t),
                      "usage": {"inputOther": inp, "output": out,
                                "inputCacheRead": cr,
                                "inputCacheCreation": cc}})]


def km_session(now, tps=80.0, ttft=6.0, outs=(120, 350, 700, 1100, 1700, 2400),
               start=None, gap=60, model="k3"):
    """构造已知真值的 Kimi 会话:每条响应 dur = ttft + out/tps。"""
    recs, t = [], (start if start is not None else now - 600)
    for out in outs:
        recs.append(km(t, "turn.prompt"))
        recs += km_step(t + 0.1, out, tps, ttft, model=model)
        t += gap
    return recs


# ---------- OpenCode SQLite 归一化记录构造 ----------

def oc_message(mid, created, completed=None, role="assistant",
               provider="anthropic", model="claude-sonnet-4-5",
               inp=100, out=0, reasoning=0, cr=9000, cc=50, error=None):
    """opencode_parse 的输入口径；时间均为 epoch 毫秒。"""
    r = {"id": mid, "role": role, "created": int(created),
         "completed": int(completed) if completed is not None else None,
         "provider": provider, "model": model,
         "tokens": {"input": inp, "output": out, "reasoning": reasoning,
                    "cache": {"read": cr, "write": cc}},
         "error": error}
    return r


def oc_part(mid, typ, start=None, end=None, status=None):
    """opencode_parse 的 part 输入口径；tool 已把 state.time 摊平。"""
    return {"message_id": mid, "type": typ,
            "start": int(start) if start is not None else None,
            "end": int(end) if end is not None else None,
            "status": status}


def oc_session(now, tps=70.0, ttft=5.0,
               outs=(100, 400, 800, 1500, 2500), start=None, gap=60,
               provider="anthropic", model="claude-sonnet-4-5"):
    """构造 OpenCode 已完成 assistant 消息及其 text part。"""
    messages, parts = [], []
    t = start if start is not None else now - 600
    for i, out in enumerate(outs):
        end = t + ttft + out / tps
        mid = "oc%d" % i
        messages.append(oc_message(mid, t * 1000, end * 1000,
                                   provider=provider, model=model, out=out))
        parts.append(oc_part(mid, "text", (t + ttft) * 1000, end * 1000))
        t += gap
    return messages, parts


# ---------- collect.py 独有函数 ----------

class TestCollectHelpers(unittest.TestCase):
    def test_project_label(self):
        cases = [
            ("-Users-x-Documents-work-App--claude-worktrees-my-feature-branch-aec861",
             "feature-branch"),            # hex 尾段剥除
            ("-Users-x-Documents-Proj-sdkimg-08ec45aa", "Proj-sdkimg"),
            ("-Users-x-Documents-agent-worktrees-XY-29", "XY"),  # 短数字尾段剥除
            ("-Users-x-tools-claude-speed", "claude-speed"),
            ("-Users-x-Documents-someLongProjectName-20260521", "20260521"),  # 日期保留
            ("-private-tmpXXXX-neutral-cwd", "neutral-cwd"),
        ]
        for dirname, want in cases:
            self.assertEqual(cs.project_label(dirname), want, dirname)

    def test_model_tag(self):
        self.assertEqual(cs.model_tag("claude-opus-4-8"), "opus4.8")
        self.assertEqual(cs.model_tag("claude-fable-5"), "fable5")
        self.assertEqual(cs.model_tag("claude-haiku-4-5-20251001"), "haiku4.5")
        self.assertEqual(cs.model_tag("gpt-5.6-sol"), "gpt5.6sol")
        # OpenCode 内部模型键带来源/provider 命名空间；展示只留模型短名
        self.assertEqual(
            cs.model_tag("opencode:anthropic/claude-sonnet-4-5"), "sonnet4.5")
        self.assertEqual(cs.model_tag(None), "")


class TestCodexParse(unittest.TestCase):
    def test_recovers_known_speed(self):
        now = time.time()
        groups, err_ts, label, trig = cs.codex_parse(
            jlines(cx_session(now, tps=70, ttft=5)))
        self.assertEqual(label, "myproj")
        self.assertEqual(len(groups), 5)
        self.assertEqual(groups[-1]["model"], "gpt-5.6-sol")
        tps, ttft = cs.fit_speed(groups)
        self.assertAlmostEqual(tps, 70, delta=2)
        self.assertAlmostEqual(ttft, 5, delta=0.5)

    def test_usage_mapping_and_cache(self):
        now = time.time()
        groups, _, _, _ = cs.codex_parse(jlines(cx_session(now, outs=(500,))))
        g = groups[0]
        self.assertEqual(g["inp"], 1000)   # input(10000) − cached(9000)
        self.assertEqual(g["cr"], 9000)
        self.assertEqual(g["cc"], 100)

    def test_end_anchor_is_last_content_not_token_count(self):
        # token_count 在工具执行后才发出:组 end 必须取最后一条内容记录
        now = time.time()
        recs = [cx(now - 30, "event_msg", "user_message"),
                cx(now - 25, "response_item", "reasoning"),
                cx(now - 20, "response_item", "custom_tool_call"),   # 末条内容
                cx(now - 10, "response_item", "custom_tool_call_output"),  # 工具跑了10s
                cx_tc(now - 9.9, 300)]
        groups, _, _, _ = cs.codex_parse(jlines(recs))
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]["end"], now - 20, delta=0.01)
        self.assertAlmostEqual(groups[0]["start"], now - 30, delta=0.01)

    def test_developer_message_not_content(self):
        # role=developer/user 的 message 是输入,不该开组
        now = time.time()
        recs = [cx(now - 30, "event_msg", "user_message"),
                cx(now - 29, "response_item", "message", role="developer"),
                cx(now - 25, "response_item", "message", role="assistant"),
                cx_tc(now - 24, 200)]
        groups, _, _, _ = cs.codex_parse(jlines(recs))
        self.assertEqual(len(groups), 1)
        # 锚点应是 developer message(组前一条),而非更早的 user_message
        self.assertAlmostEqual(groups[0]["start"], now - 29, delta=0.01)

    def test_unclosed_turn_discarded_at_boundary(self):
        # 轮1没有 token_count 收尾 → 轮2的 user_message 应丢弃残组,
        # 不得把两轮合并成一个「含用户思考时间」的假慢组
        now = time.time()
        recs = [cx(now - 300, "event_msg", "user_message"),
                cx(now - 299, "response_item", "reasoning"),
                cx(now - 296, "event_msg", "agent_message"),   # 轮1无收尾
                cx(now - 190, "event_msg", "user_message"),    # 轮2边界
                cx(now - 189, "response_item", "reasoning"),
                cx(now - 185, "event_msg", "agent_message"),
                cx_tc(now - 184.5, 400)]
        groups, _, _, _ = cs.codex_parse(jlines(recs))
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]["start"], now - 190, delta=0.01)
        self.assertAlmostEqual(groups[0]["end"], now - 185, delta=0.01)

    def test_model_fallback_sentinel(self):
        # 尾读截掉 turn_context 时 model 未知 → 哨兵 "codex",不与 None 混池
        now = time.time()
        recs = [cx(now - 30, "event_msg", "user_message"),
                cx(now - 25, "response_item", "reasoning"),
                cx_tc(now - 24, 200)]
        groups, _, _, _ = cs.codex_parse(jlines(recs))
        self.assertEqual(groups[0]["model"], "codex")

    def test_waiting_trigger_cleared_by_content_and_completion(self):
        now = time.time()
        base = [cx(now - 60, "event_msg", "user_message"),
                cx(now - 55, "response_item", "reasoning"),
                cx_tc(now - 50, 100)]
        # 尾部是触发记录 → 等待中
        _, _, _, trig = cs.codex_parse(jlines(
            base + [cx(now - 8, "event_msg", "user_message")]))
        self.assertAlmostEqual(trig, now - 8, delta=0.01)
        # task_complete 清零
        _, _, _, trig = cs.codex_parse(jlines(
            base + [cx(now - 8, "event_msg", "user_message"),
                    cx(now - 7, "event_msg", "task_complete")]))
        self.assertIsNone(trig)


class TestKimiParse(unittest.TestCase):
    def test_recovers_known_speed(self):
        now = time.time()
        groups, err_ts, _ = cs.kimi_parse(jlines(km_session(now, tps=80, ttft=6)))
        self.assertEqual(len(groups), 6)
        self.assertEqual(groups[-1]["model"], "k3")
        self.assertEqual(err_ts, [])
        tps, ttft = cs.fit_speed(groups)
        self.assertAlmostEqual(tps, 80, delta=2)
        self.assertAlmostEqual(ttft, 6, delta=0.5)

    def test_usage_mapping(self):
        now = time.time()
        groups, _, _ = cs.kimi_parse(jlines(km_session(now, outs=(500,))))
        g = groups[0]
        self.assertEqual(g["inp"], 100)
        self.assertEqual(g["cr"], 9000)
        self.assertEqual(g["cc"], 50)

    def test_start_anchor_is_request_send_time(self):
        # turn.prompt 在前:锚点是 llm.request(请求发出时刻),不是 prompt
        now = time.time()
        recs = [km(now - 30, "turn.prompt")] + km_step(now - 20, 300, 80, 6)
        groups, _, _ = cs.kimi_parse(jlines(recs))
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]["start"], now - 20, delta=0.01)
        self.assertAlmostEqual(groups[0]["end"], now - 20 + 6 + 300 / 80, delta=0.01)

    def test_retry_reanchors_to_retry_request(self):
        # 失败请求(无 step.end)30s 后重试:锚点必须取重试的 llm.request,
        # 否则 dur 掺入重试间隔成假慢点
        now = time.time()
        recs = [km(now - 60, "llm.request", kind="loop", model="k3")]
        recs += km_step(now - 30, 600, 80, 6)
        groups, _, _ = cs.kimi_parse(jlines(recs))
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]["start"], now - 30, delta=0.01)

    def test_waiting_trigger_cleared_by_content_and_step_end(self):
        now = time.time()
        # 尾部是 llm.request(已发请求无返回)→ 等待中
        _, _, trig = cs.kimi_parse(jlines(
            [km(now - 8, "turn.prompt"),
             km(now - 7, "llm.request", kind="loop", model="k3")]))
        self.assertAlmostEqual(trig, now - 7, delta=0.01)
        # 首个内容到达清零
        _, _, trig = cs.kimi_parse(jlines(
            [km(now - 8, "llm.request", kind="loop", model="k3"),
             km(now - 6, "context.append_loop_event",
                event={"type": "content.part"})]))
        self.assertIsNone(trig)
        # step.end 落地清零
        _, _, trig = cs.kimi_parse(jlines(km_step(now - 20, 300, 80, 6)))
        self.assertIsNone(trig)

    def test_malformed_and_unknown_records_skipped(self):
        # wire 是未文档化内部格式:坏行/未知记录一律跳过,不影响解析
        now = time.time()
        recs = ["not json",
                json.dumps({"type": "metadata", "protocol_version": "1.0"}),
                json.dumps({"type": "usage.record", "model": "kimi-code/k3"})]
        recs += jlines(km_step(now - 20, 300, 80, 6))
        groups, _, _ = cs.kimi_parse(recs)
        self.assertEqual(len(groups), 1)


class TestOpenCodeParse(unittest.TestCase):
    def test_recovers_known_speed(self):
        messages, parts = oc_session(time.time(), tps=70, ttft=5)
        groups, err_ts, trig = cs.opencode_parse(messages, parts)
        self.assertEqual(len(groups), 5)
        self.assertEqual(err_ts, [])
        self.assertIsNone(trig)
        tps, ttft = cs.fit_speed(groups)
        self.assertAlmostEqual(tps, 70, delta=2)
        self.assertAlmostEqual(ttft, 5, delta=0.5)

    def test_usage_model_and_overlapping_tool_time(self):
        # 总墙钟 20s；两个工具区间 [2,9]、[7,15] 的并集为 13s，
        # generation duration 应为 7s，不能把重叠的 2s 重复扣除。
        m = oc_message("m1", 1_000_000, 1_020_000,
                       inp=321, out=400, reasoning=25, cr=9876, cc=54)
        parts = [oc_part("m1", "tool", 1_002_000, 1_009_000, "completed"),
                 oc_part("m1", "tool", 1_007_000, 1_015_000, "completed"),
                 oc_part("m1", "text", 1_019_000, 1_020_000)]
        groups, err_ts, trig = cs.opencode_parse([m], parts)
        self.assertEqual(len(groups), 1)
        self.assertEqual(err_ts, [])
        self.assertIsNone(trig)
        g = groups[0]
        self.assertEqual(g["id"], "m1")
        self.assertEqual(g["out"], 425)  # 可见输出 + reasoning 都是生成 token
        self.assertEqual(g["model"],
                         "opencode:anthropic/claude-sonnet-4-5")
        self.assertEqual((g["inp"], g["cr"], g["cc"]), (321, 9876, 54))
        self.assertAlmostEqual(g["end"], 1020.0)
        self.assertAlmostEqual(g["start"], 1013.0)
        self.assertAlmostEqual(g["end"] - g["start"], 7.0)

    def test_generation_boundary_still_deducts_earlier_tool(self):
        # 没有 text/reasoning 时，最后一个 tool 的 start 是 generation 边界。
        # 第一个长工具已在边界前完整发生，必须从 45s elapsed 中扣掉 30s；
        # 第二个工具自身从边界才开始，不应混入生成时长。
        m = oc_message("m1", 2_000_000, 2_060_000, out=600)
        parts = [oc_part("m1", "tool", 2_005_000, 2_035_000, "completed"),
                 oc_part("m1", "tool", 2_045_000, 2_055_000, "completed")]
        groups, _, _ = cs.opencode_parse([m], parts)
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]["end"], 2060.0)  # 活跃时间仍取 completed
        self.assertAlmostEqual(groups[0]["end"] - groups[0]["start"], 15.0)

    def test_waiting_requires_empty_unfinished_assistant(self):
        now_ms = int(time.time() * 1000)
        pending = oc_message("pending", now_ms - 8000, completed=None)

        # step-start 是账务边界，不是模型内容，仍处于等待首字。
        _, _, trig = cs.opencode_parse(
            [pending], [oc_part("pending", "step-start")])
        self.assertAlmostEqual(trig, (now_ms - 8000) / 1000, delta=0.001)

        # 任一 text/reasoning/tool part 到达，都说明已经开始响应。
        for typ in ("text", "reasoning", "tool"):
            with self.subTest(typ=typ):
                part = oc_part("pending", typ, now_ms - 7000, None,
                               "running" if typ == "tool" else None)
                _, _, trig = cs.opencode_parse([pending], [part])
                self.assertIsNone(trig)

        completed = oc_message("done", now_ms - 8000, now_ms - 7000)
        _, _, trig = cs.opencode_parse([completed], [])
        self.assertIsNone(trig)

    def test_error_is_reported_not_grouped(self):
        m = oc_message("bad", 3_000_000, 3_001_000,
                       error={"name": "ProviderError"})
        groups, err_ts, trig = cs.opencode_parse([m], [])
        self.assertEqual(groups, [])
        self.assertEqual(len(err_ts), 1)
        self.assertIsNone(trig)


# ---------- collect.main 端到端合成场景 ----------

class TestCollectMain(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.codex_root = tempfile.mkdtemp()  # 隔离,防真实 ~/.codex 漏进测试
        self.kimi_root = tempfile.mkdtemp()   # 隔离,防真实 ~/.kimi-code 漏进测试
        self.opencode_root = tempfile.mkdtemp()  # 隔离真实 OpenCode SQLite
        self.opencode_db = os.path.join(self.opencode_root, "opencode.db")
        self._had_opencode_db = hasattr(cs, "OPENCODE_DB")
        self._old = (cs.ROOT, cs.CODEX_ROOT, cs.KIMI_ROOT,
                     getattr(cs, "OPENCODE_DB", None))
        cs.ROOT, cs.CODEX_ROOT, cs.KIMI_ROOT = (self.root, self.codex_root,
                                                self.kimi_root)
        cs.OPENCODE_DB = self.opencode_db
        self.now = time.time()

    def tearDown(self):
        cs.ROOT, cs.CODEX_ROOT, cs.KIMI_ROOT = self._old[:3]
        if self._had_opencode_db:
            cs.OPENCODE_DB = self._old[3]
        else:
            del cs.OPENCODE_DB

    def write(self, dirname, recs, mtime=None):
        d = os.path.join(self.root, dirname)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "s.jsonl")
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))

    def write_kimi(self, wdkey, recs, mtime=None):
        # 布局:<KIMI_ROOT>/<wd_key>/<sessionId>/agents/main/wire.jsonl
        p = os.path.join(self.kimi_root, wdkey, "session_x", "agents", "main")
        os.makedirs(p, exist_ok=True)
        wp = os.path.join(p, "wire.jsonl")
        with open(wp, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        if mtime:
            os.utime(wp, (mtime, mtime))

    def create_opencode_db(self, wal=False):
        """建立 OpenCode 正式 schema 中采集器实际依赖的最小子集。"""
        conn = sqlite3.connect(self.opencode_db)
        if wal:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                             "wal")
            conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                directory TEXT,
                time_updated INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT,
                session_id TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT
            );
            CREATE INDEX message_session_time_created_id_idx
                ON message(session_id, time_created, id);
            CREATE INDEX part_message_id_id_idx ON part(message_id, id);
            CREATE INDEX part_session_idx ON part(session_id);
        """)
        conn.commit()
        return conn

    def write_opencode(self, label, messages, parts=(), updated=None,
                       sid="ses_test", parent=None, conn=None):
        """把归一化 fixture 还原成 OpenCode message/part 的 data JSON。"""
        own_conn = conn is None
        if own_conn:
            conn = self.create_opencode_db()
        updated_ms = (int(updated * 1000) if updated is not None else
                      max([m.get("completed") or m["created"] for m in messages]
                          or [int(self.now * 1000)]))
        conn.execute("INSERT INTO session VALUES (?, ?, ?, ?)",
                     (sid, parent, "/Users/x/" + label, updated_ms))
        message_times = {}
        for m in messages:
            tm = {"created": m["created"]}
            if m.get("completed") is not None:
                tm["completed"] = m["completed"]
            data = {"role": m["role"], "time": tm}
            if m["role"] == "assistant":
                data.update({"providerID": m.get("provider"),
                             "modelID": m.get("model"),
                             "tokens": m.get("tokens") or {}})
                if m.get("error") is not None:
                    data["error"] = m["error"]
            mt = m.get("completed") or m["created"]
            message_times[m["id"]] = mt
            conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                         (m["id"], sid, m["created"], mt, json.dumps(data)))
        for i, p in enumerate(parts):
            if p["type"] == "tool":
                state = {"status": p.get("status")}
                pt = {}
                if p.get("start") is not None:
                    pt["start"] = p["start"]
                if p.get("end") is not None:
                    pt["end"] = p["end"]
                if pt:
                    state["time"] = pt
                data = {"type": "tool", "state": state}
            else:
                data = {"type": p["type"]}
                pt = {}
                if p.get("start") is not None:
                    pt["start"] = p["start"]
                if p.get("end") is not None:
                    pt["end"] = p["end"]
                if pt:
                    data["time"] = pt
            pt_created = p.get("start") or message_times[p["message_id"]]
            pt_updated = p.get("end") or pt_created
            conn.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                         ("prt_%s_%d" % (sid, i), p["message_id"], sid,
                          pt_created, pt_updated, json.dumps(data)))
        conn.commit()
        if own_conn:
            conn.close()

    def run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cs.main()
        return buf.getvalue()

    def test_idle_empty_root(self):
        out = self.run_main()
        self.assertIn("⚪", out.splitlines()[0])
        self.assertIn("近2小时无响应", out)

    def test_opencode_missing_db_is_not_created(self):
        self.assertFalse(os.path.exists(self.opencode_db))
        out = self.run_main()
        self.assertFalse(os.path.exists(self.opencode_db))
        self.assertIn("近2小时无响应", out)

    def test_opencode_unknown_schema_degrades_to_empty_source(self):
        conn = sqlite3.connect(self.opencode_db)
        conn.execute("CREATE TABLE future_schema_only (id TEXT PRIMARY KEY)")
        conn.close()
        out = self.run_main()
        self.assertIn("近2小时无响应", out)
        self.assertNotIn("⚡?", out)

    def test_opencode_reads_committed_wal(self):
        # 桌面端运行时 DB 常驻 WAL；只读采集器必须能看到尚未 checkpoint 的提交。
        conn = self.create_opencode_db(wal=True)
        messages, parts = oc_session(
            self.now, outs=(500,), start=self.now - 20,
            provider="openai", model="gpt-5.1-codex")
        self.write_opencode("wal-project", messages, parts, conn=conn)
        try:
            self.assertTrue(os.path.exists(self.opencode_db + "-wal"))
            out = self.run_main()
        finally:
            conn.close()
        self.assertIn("wal-project·gpt5.1codex", out)

    def test_full_fit_renders_speed_and_ttft(self):
        self.write("-Users-x-proj-alpha", make_session(self.now, tps=70, ttft=4))
        out = self.run_main()
        self.assertIn("🟢", out.splitlines()[0])
        self.assertIn("tok/s", out)
        self.assertIn("首字", out)
        self.assertIn("alpha·fable5", out)

    def test_two_stage_borrows_slope(self):
        self.write("-Users-x-proj-rich", make_session(self.now, tps=70, ttft=5))
        poor = [rec_u(self.now - 200),
                rec_a("p1", self.now - 200 + 5 + 200 / 70, 200),
                rec_u(self.now - 100),
                rec_a("p2", self.now - 100 + 5 + 250 / 70, 250)]
        self.write("-Users-x-proj-poor", poor, mtime=self.now)
        out = self.run_main()
        self.assertIn("≈", out)  # poor 会话借 rich 的斜率

    def test_realtime_waiting_new_session(self):
        self.write("-Users-x-proj-fresh", [rec_u(self.now - 9)], mtime=self.now - 9)
        out = self.run_main()
        self.assertNotIn("⏳", out.splitlines()[0])  # 等待只进下拉,不占图标栏
        self.assertIn("等待首个响应", out)

    def test_waiting_with_history_says_not_first(self):
        recs = [rec_u(self.now - 200), rec_a("m1", self.now - 194, 120),
                rec_u(self.now - 10)]
        self.write("-Users-x-proj-loop", recs, mtime=self.now - 10)
        out = self.run_main()
        self.assertIn("等待响应", out)
        self.assertNotIn("等待首个响应", out)  # 有历史响应就不是「首个」

    def test_waiting_timeout_hides_indicator(self):
        self.write("-Users-x-proj-stale", [rec_u(self.now - 300)],
                   mtime=self.now - 300)
        out = self.run_main()
        self.assertNotIn("⏳", out)  # >120s 视为已中断

    def test_attachment_tail_still_waiting(self):
        self.write("-Users-x-proj-att",
                   [rec_u(self.now - 9), rec_att(self.now - 8.9)],
                   mtime=self.now - 9)
        out = self.run_main()
        self.assertIn("等待首个响应", out)  # 行内可见;图标栏不再显示 ⏳
        self.assertNotIn("⏳", out.splitlines()[0])

    def test_all_error_session_surfaces_warning(self):
        recs = [rec_u(self.now - 200), rec_err(self.now - 190),
                rec_err(self.now - 140), rec_err(self.now - 100)]
        self.write("-Users-x-proj-broken", recs, mtime=self.now - 100)
        out = self.run_main()
        self.assertIn("⚠️", out.splitlines()[0])
        self.assertIn("无成功响应", out)
        self.assertIn("⚠️3错", out)

    def test_cold_cache_flagged(self):
        recs = [rec_u(self.now - 60),
                rec_a("m1", self.now - 60 + 5 + 300 / 70, 300, cr=100, cc=8000)]
        recs = make_session(self.now, start=self.now - 700) + recs
        self.write("-Users-x-proj-cold", recs)
        out = self.run_main()
        self.assertIn("冷", out)

    def write_agents(self, dirname, n, out=600, mtime=None):
        """给 write() 建的会话(s.jsonl)配 n 个子代理 transcript。"""
        subdir = os.path.join(self.root, dirname, "s", "subagents")
        os.makedirs(subdir, exist_ok=True)
        for i in range(n):
            p = os.path.join(subdir, "agent-%d.jsonl" % i)
            recs = [rec_u(self.now - 30), rec_a("a%d" % i, self.now - 10, out)]
            with open(p, "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            mt = mtime if mtime else self.now - 5
            os.utime(p, (mt, mt))

    def test_background_agents_shown(self):
        self.write("-Users-x-proj-bg", make_session(self.now))
        self.write_agents("-Users-x-proj-bg", 2)
        out = self.run_main()
        self.assertIn("🤖2·Σ10tok/s", out)  # 2×600tok/120s = 10
        self.assertIn("🤖2", out.splitlines()[0])

    def test_background_only_title_shows_fleet(self):
        # 主链最后响应在 20+ 分钟前(超出标题窗口),但代理正在烧
        self.write("-Users-x-proj-bgonly",
                   make_session(self.now, start=self.now - 1900, gap=30))
        self.write_agents("-Users-x-proj-bgonly", 3)
        out = self.run_main()
        self.assertTrue(out.splitlines()[0].startswith("🤖3 Σ"),
                        "标题应为舰队状态,实际: " + out.splitlines()[0])

    def test_stale_agents_not_counted(self):
        self.write("-Users-x-proj-oldbg", make_session(self.now))
        self.write_agents("-Users-x-proj-oldbg", 2, mtime=self.now - 600)
        out = self.run_main()
        self.assertNotIn("🤖", out)

    def test_title_sums_agents_across_sessions(self):
        # 标题舰队数 = 所有在榜会话合计(2+3=5),不是单一「最新会话」
        self.write("-Users-x-proj-fleetA", make_session(self.now))
        self.write_agents("-Users-x-proj-fleetA", 2)
        self.write("-Users-x-proj-fleetB",
                   make_session(self.now, start=self.now - 400))
        self.write_agents("-Users-x-proj-fleetB", 3)
        out = self.run_main()
        self.assertIn("🤖5", out.splitlines()[0])

    def test_silent_main_with_agents_row(self):
        # 主链无任何成功响应、也不在等待态,但代理在烧 → 「后台任务运行中」
        self.write("-Users-x-proj-silentbg", [rec_u(self.now - 300)])
        self.write_agents("-Users-x-proj-silentbg", 2)
        out = self.run_main()
        self.assertIn("后台任务运行中", out)
        self.assertIn("🤖2·Σ10tok/s", out)
        self.assertTrue(out.splitlines()[0].startswith("🤖2 Σ"))

    def write_codex(self, fname, recs, mtime=None):
        d = os.path.join(self.codex_root, "2026", "07", "22")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, fname)
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))

    def test_codex_session_rendered(self):
        self.write_codex("rollout-x.jsonl", cx_session(self.now, tps=90, ttft=4))
        out = self.run_main()
        self.assertIn("myproj·gpt5.6sol", out)
        self.assertIn("🟢", out)
        self.assertIn("tok/s", out)
        self.assertIn("首字", out)

    def test_opencode_session_rendered(self):
        messages, parts = oc_session(self.now, tps=70, ttft=5)
        self.write_opencode("oc-project", messages, parts)
        out = self.run_main()
        self.assertIn("oc-project·sonnet4.5", out)
        self.assertIn("🟢", out)
        self.assertIn("tok/s", out)
        self.assertIn("首字", out)

    def test_codex_and_claude_merged(self):
        self.write("-Users-x-proj-claude", make_session(self.now))
        self.write_codex("rollout-y.jsonl", cx_session(self.now, start=self.now - 500))
        out = self.run_main()
        self.assertIn("proj-claude·fable5", out)
        self.assertIn("myproj·gpt5.6sol", out)

    def test_opencode_and_claude_merged(self):
        self.write("-Users-x-proj-claude", make_session(self.now))
        messages, parts = oc_session(self.now, start=self.now - 500)
        self.write_opencode("oc-merge", messages, parts)
        out = self.run_main()
        self.assertIn("proj-claude·fable5", out)
        self.assertIn("oc-merge·sonnet4.5", out)

    def test_opencode_child_session_is_background_agent(self):
        conn = self.create_opencode_db()
        root_messages, root_parts = oc_session(
            self.now, start=self.now - 600)
        self.write_opencode("oc-parent", root_messages, root_parts,
                            sid="ses_parent", conn=conn)
        child_messages, child_parts = oc_session(
            self.now, outs=(600,), start=self.now - 30)
        for m in child_messages:
            m["id"] = "child_" + m["id"]
        for p in child_parts:
            p["message_id"] = "child_" + p["message_id"]
        self.write_opencode("oc-child", child_messages, child_parts,
                            sid="ses_child", parent="ses_parent", conn=conn)
        conn.close()
        out = self.run_main()
        self.assertIn("oc-parent·sonnet4.5", out)
        self.assertIn("🤖1·Σ5tok/s", out)  # 600 token / 120s burn window
        self.assertNotIn("oc-child·", out)  # child 不独占下拉名额

    def test_zombie_session_excluded(self):
        # 文件 mtime 很新(被后台进程碰过),但最后响应在窗口外 → 不入榜
        old = make_session(self.now, start=self.now - 3 * 3600, gap=30)
        self.write("-Users-x-proj-zombie", old, mtime=self.now - 60)
        out = self.run_main()
        self.assertIn("近2小时无响应", out)

    def test_codex_zombie_excluded(self):
        # Codex 侧同样适用僵尸过滤:文件 mtime 新、内容陈旧 → 不入榜
        stale = cx_session(self.now, start=self.now - 3 * 3600, gap=30)
        self.write_codex("rollout-z.jsonl", stale, mtime=self.now - 60)
        out = self.run_main()
        self.assertIn("近2小时无响应", out)

    def test_opencode_zombie_excluded(self):
        # session.time_updated 很新，但完成响应已在窗口外，不能成为僵尸行。
        messages, parts = oc_session(
            self.now, start=self.now - 3 * 3600, gap=30)
        self.write_opencode("oc-zombie", messages, parts,
                            updated=self.now - 60)
        out = self.run_main()
        self.assertIn("近2小时无响应", out)

    def test_codex_waiting_row(self):
        recs = cx_session(self.now, outs=(300,), start=self.now - 200)
        recs.append(cx(self.now - 10, "event_msg", "user_message"))
        self.write_codex("rollout-w.jsonl", recs, mtime=self.now - 10)
        out = self.run_main()
        self.assertIn("⏳等10秒", out)  # 有历史响应:速度段照常,等待作附加段

    def test_opencode_waiting_row(self):
        messages, parts = oc_session(
            self.now, outs=(300,), start=self.now - 200)
        messages.append(oc_message("pending", (self.now - 10) * 1000))
        self.write_opencode("oc-wait", messages, parts)
        out = self.run_main()
        self.assertIn("⏳等10秒", out)

    def test_kimi_session_rendered(self):
        # session_index 缺失 → 标签回退自 workDirKey(wd_<slug>_<hash12>)
        self.write_kimi("wd_myproj_abc123def456",
                        km_session(self.now, tps=80, ttft=6))
        out = self.run_main()
        self.assertIn("myproj·k3", out)
        self.assertIn("🟢", out)
        self.assertIn("tok/s", out)
        self.assertIn("首字", out)

    def test_kimi_waiting_row(self):
        recs = km_session(self.now, outs=(300,), start=self.now - 200)
        recs.append(km(self.now - 10, "turn.prompt"))
        self.write_kimi("wd_waitproj-abc123def456", recs, mtime=self.now - 10)
        out = self.run_main()
        self.assertIn("⏳等10秒", out)

    def test_kimi_zombie_excluded(self):
        # 文件 mtime 新、内容陈旧 → 不入榜(僵尸过滤与 Claude/Codex 同规则)
        stale = km_session(self.now, start=self.now - 3 * 3600, gap=30)
        self.write_kimi("wd_oldproj-abc123def456", stale, mtime=self.now - 60)
        out = self.run_main()
        self.assertIn("近2小时无响应", out)

    def test_agent_transcripts_ignored(self):
        d = os.path.join(self.root, "-Users-x-proj-sub")
        os.makedirs(d)
        with open(os.path.join(d, "agent-abc123.jsonl"), "w") as f:
            for r in make_session(self.now):
                f.write(json.dumps(r) + "\n")
        out = self.run_main()
        self.assertIn("近2小时无响应", out)  # 子代理文件不算会话


# ---------- statusline 端到端 ----------

class TestStatuslineMain(unittest.TestCase):
    def _run(self, stdin_obj):
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(stdin_obj) if stdin_obj is not None
                                else "not json")
        try:
            with contextlib.redirect_stdout(buf):
                sl.main()
        finally:
            sys.stdin = old
        return buf.getvalue()

    def test_renders_speed_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for r in make_session(time.time(), tps=70, ttft=4):
                f.write(json.dumps(r) + "\n")
            path = f.name
        out = self._run({"model": {"display_name": "TestModel"},
                         "transcript_path": path,
                         "context_window": {"used_percentage": 42}})
        os.unlink(path)
        self.assertIn("TestModel", out)
        self.assertIn("tok/s", out)
        self.assertIn("首字", out)
        self.assertIn("ctx 42%", out)

    def test_renders_background_agents(self):
        root = tempfile.mkdtemp()
        now = time.time()
        path = os.path.join(root, "sess.jsonl")
        with open(path, "w") as f:
            for r in make_session(now):
                f.write(json.dumps(r) + "\n")
        subdir = os.path.join(root, "sess", "subagents")
        os.makedirs(subdir)
        ap = os.path.join(subdir, "agent-1.jsonl")
        with open(ap, "w") as f:
            f.write(json.dumps(rec_u(now - 30)) + "\n")
            f.write(json.dumps(rec_a("a", now - 10, 600)) + "\n")
        out = self._run({"transcript_path": path})
        self.assertIn("🤖1", out)
        self.assertIn("Σ5tok/s", out)  # 600tok/120s

    def test_bad_stdin_degrades_gracefully(self):
        out = self._run(None)
        self.assertIn("⚡ 暂无速度数据", out)


if __name__ == "__main__":
    unittest.main()
