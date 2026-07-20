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
                "current_model_groups", "median", "clean_points",
                "ts_slope", "fit_speed", "windowed_fit"]
SHARED_CONSTS = ["TAIL_BYTES", "MAX_SEC_PER_TOK", "FIT_MIN_SAMPLES",
                 "FIT_MIN_SPAN", "FIT_MIN_PAIR_DX", "FIT_WINDOW_START",
                 "ERR_ROW_WINDOW", "CACHE_OK"]


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


# ---------- collect.py 独有函数 ----------

class TestCollectHelpers(unittest.TestCase):
    def test_project_label(self):
        cases = [
            ("-Users-x-Documents-work-App--claude-worktrees-llm-api-concurrency-aec861",
             "api-concurrency"),           # hex 尾段剥除
            ("-Users-x-Documents-Proj-sdkimg-08ec45aa", "Proj-sdkimg"),
            ("-Users-x-Documents-agent-worktrees-CLA-29", "CLA"),  # 短数字尾段剥除
            ("-Users-x-tools-claude-speed", "claude-speed"),
            ("-Users-x-Documents-claudecodePrj-20260521", "20260521"),  # 日期保留
            ("-private-tmpXXXX-neutral-cwd", "neutral-cwd"),
        ]
        for dirname, want in cases:
            self.assertEqual(cs.project_label(dirname), want, dirname)

    def test_model_tag(self):
        self.assertEqual(cs.model_tag("claude-opus-4-8"), "opus4.8")
        self.assertEqual(cs.model_tag("claude-fable-5"), "fable5")
        self.assertEqual(cs.model_tag("claude-haiku-4-5-20251001"), "haiku4.5")
        self.assertEqual(cs.model_tag(None), "")


# ---------- collect.main 端到端合成场景 ----------

class TestCollectMain(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._old_root = cs.ROOT
        cs.ROOT = self.root
        self.now = time.time()

    def tearDown(self):
        cs.ROOT = self._old_root

    def write(self, dirname, recs, mtime=None):
        d = os.path.join(self.root, dirname)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "s.jsonl")
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        if mtime:
            os.utime(p, (mtime, mtime))

    def run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cs.main()
        return buf.getvalue()

    def test_idle_empty_root(self):
        out = self.run_main()
        self.assertIn("⚪", out.splitlines()[0])
        self.assertIn("近2小时无 Claude 响应", out)

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
        self.assertIn("⏳", out.splitlines()[0])
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
        self.assertIn("⏳", out.splitlines()[0])

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

    def test_agent_transcripts_ignored(self):
        d = os.path.join(self.root, "-Users-x-proj-sub")
        os.makedirs(d)
        with open(os.path.join(d, "agent-abc123.jsonl"), "w") as f:
            for r in make_session(self.now):
                f.write(json.dumps(r) + "\n")
        out = self.run_main()
        self.assertIn("近2小时无 Claude 响应", out)  # 子代理文件不算会话


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

    def test_bad_stdin_degrades_gracefully(self):
        out = self._run(None)
        self.assertIn("⚡ 暂无速度数据", out)


if __name__ == "__main__":
    unittest.main()
