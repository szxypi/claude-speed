#!/usr/bin/env python3
"""金标准回归:把估计器对冻结夹具的读数钉在 tests/golden.json 的登记值上。

这是「基准漂移」的防线(见 METRIC.md)。两道断言:
  1. 读数 vs 登记值(drift_tol):任何算法改动若移动了读数就变红——
     基准可以改,但必须显式跑 regen_golden.py 重定标、在 diff 里留痕;
  2. 读数 vs 真值(calib_tol):合成夹具带已知真值,保证估计器无偏——
     即便有人重定标后读数与登记值一致,把读数标定到偏离真值处也会被拦。
"""
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def opencode_groups(cs, lines):
    """归一化 JSONL 按 SQLite 来源表拆开后交给 OpenCode 解析器。"""
    records = [json.loads(line) for line in lines]
    messages = [r for r in records if r.get("table") == "message"]
    parts = [r for r in records if r.get("table") == "part"]
    return cs.opencode_parse(messages, parts)[0]


def load_cs():
    spec = importlib.util.spec_from_file_location("cs_golden", os.path.join(REPO, "collect.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestGolden(unittest.TestCase):
    PARSERS = {"claude": lambda cs, lines: cs.response_groups(lines)[0],
               "codex": lambda cs, lines: cs.codex_parse(lines)[0],
               "kimi": lambda cs, lines: cs.kimi_parse(lines)[0],
               "opencode": opencode_groups}

    @classmethod
    def setUpClass(cls):
        cls.cs = load_cs()
        with open(os.path.join(HERE, "golden.json")) as f:
            cls.doc = json.load(f)

    def measure(self, name, source):
        with open(os.path.join(HERE, "fixtures", name + ".jsonl")) as f:
            lines = f.read().splitlines()
        groups = self.PARSERS[source](self.cs, lines)
        return groups, self.cs.fit_speed(self.cs.current_model_groups(groups))

    def test_version_matches_metric_md(self):
        with open(os.path.join(REPO, "METRIC.md")) as f:
            head = f.read(400)
        self.assertIn("v%s" % self.doc["metric_version"], head,
                      "METRIC.md 顶部的版本号必须与 golden.json 一致")

    def test_fixtures_present(self):
        # 登记表里每个夹具都得有对应的冻结字节
        for name in self.doc["fixtures"]:
            self.assertTrue(
                os.path.exists(os.path.join(HERE, "fixtures", name + ".jsonl")),
                "缺少夹具文件: " + name)

    def test_readings_pinned_and_calibrated(self):
        dt, ct = self.doc["drift_tol"], self.doc["calib_tol"]
        for name, e in self.doc["fixtures"].items():
            with self.subTest(fixture=name):
                groups, fit = self.measure(name, e["source"])
                self.assertEqual(len(groups), e["groups"], "解析组数漂移")

                reg = e["reading"]
                # --- 断言1:读数钉在登记值上(形状一致 + 数值在 drift_tol 内) ---
                if reg is None:
                    self.assertIsNone(fit, "登记为「样本不足」,实际拆出了读数")
                    continue
                self.assertIsNotNone(fit, "登记有读数,实际为 None")
                self.assertAlmostEqual(fit[0], reg["tps"], delta=dt["tps"],
                                       msg="TPS 相对登记值漂移(需 regen_golden 重定标)")
                if reg["ttft"] is None:
                    self.assertIsNone(fit[1], "登记为下界(无 TTFT),实际拆出了 TTFT")
                else:
                    self.assertIsNotNone(fit[1], "登记有 TTFT,实际为下界")
                    self.assertAlmostEqual(fit[1], reg["ttft"], delta=dt["ttft"],
                                           msg="TTFT 相对登记值漂移")

                # --- 断言2:合成夹具的读数须落在真值附近(估计器无偏) ---
                truth = e["truth"]
                if truth is not None:
                    self.assertIsNotNone(fit[1], "带真值的夹具应能完整拆分")
                    self.assertLessEqual(
                        abs(fit[0] - truth["tps"]), max(3.0, ct["tps_rel"] * truth["tps"]),
                        "TPS 偏离真值超容差(估计器有偏)")
                    self.assertLessEqual(
                        abs(fit[1] - truth["ttft"]), ct["ttft_abs"],
                        "TTFT 偏离真值超容差(估计器有偏)")


if __name__ == "__main__":
    unittest.main()
