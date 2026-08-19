"""Tests for exemplar injection — retrieved corrections as few-shot pairs before the live turn."""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.exemplar_inject import build_messages_with_exemplars  # noqa: E402


class TestInject(unittest.TestCase):
    def test_no_exemplars_is_passthrough(self):
        m = build_messages_with_exemplars("SYS", "Q", [])
        self.assertEqual(m, [{"role": "system", "content": "SYS"},
                             {"role": "user", "content": "Q"}])

    def test_exemplars_become_fewshot_pairs_before_live_turn(self):
        ex = [{"messages": [{"role": "user", "content": "orig"}], "corrected_answer": "fixed"}]
        m = build_messages_with_exemplars("SYS", "Q", ex)
        self.assertEqual(m[0], {"role": "system", "content": "SYS"})
        self.assertEqual(m[1], {"role": "user", "content": "orig"})
        self.assertEqual(m[2], {"role": "assistant", "content": "fixed"})
        self.assertEqual(m[-1], {"role": "user", "content": "Q"})   # live turn is last

    def test_multiple_exemplars_preserve_order(self):
        ex = [
            {"messages": [{"role": "user", "content": "a"}], "corrected_answer": "A"},
            {"messages": [{"role": "user", "content": "b"}], "corrected_answer": "B"},
        ]
        m = build_messages_with_exemplars("SYS", "Q", ex)
        self.assertEqual([x["content"] for x in m],
                         ["SYS", "a", "A", "b", "B", "Q"])


if __name__ == "__main__":
    unittest.main()
