"""Tests for lcb/fixes.py: which commit subjects count as bug fixes.

    PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_fixes -v
"""

import unittest

from lcb.fixes import attribution, classify, issue_refs


class ClassifyTest(unittest.TestCase):
    def assertKinds(self, cases):
        for subject, kind in cases:
            with self.subTest(subject=subject):
                self.assertEqual(classify(subject), kind)

    def test_fixes(self):
        self.assertKinds([
            ("fix: normalize whitespace in owner search", "conventional"),
            ("fix(jmx): strip spaces before path", "conventional"),
            ("fix!: drop legacy owner lookup", "conventional"),
            ("fixed lazy loading issues", "keyword"),
            ("Fix #108 owner update", "keyword"),
            ("Fix date formatting", "keyword"),
            ("hotfix for NPE in visit form", "keyword"),
            ("Bugfix: vets page", "keyword"),
            ('Revert "Make jar not war"', "revert"),
            # Chinese subjects (macrozheng/mall)
            ("库存问题修复", "keyword"),                      # stock issue fixed
            ("修复账号被禁用后，之前Token还能使用的问题。", "keyword"),
            ("解决优惠券使用门槛为小数的精度丢失问题", "keyword"),   # solved coupon precision loss
            ("修正订单金额计算", "keyword"),                   # corrected order total
        ])

    def test_not_fixes(self):
        self.assertKinds([
            ("Add pet birthday validation", None),
            ("Prefix owner ids", None),                         # "fix" inside a word
            ("Merge pull request #12 from a/fix-owner", None),  # the fix commit itself counts
            ('Polish "Fix content negotiation for /vets"', None),
            ("docs: Fix formatting in Docker instructions", None),
            ("test: fix flaky OwnerControllerTests", None),
            ("Fix typo in readme", None),
            ("Fixed some typos in comments", None),
            ("Fix broken links", None),
            ("Revert checkstyle to latest v12", None),          # not git's revert format
            ("支付订单金额改为从数据库订单表中获取", None),       # a change, not a fix
            ("修改收货地址接口", None),                         # 修改 = modify
            ("修复文档中的链接", None),                         # fixed links in the docs
            ("修复注释错别字", None),                           # fixed a typo in a comment
            ("", None),
            (None, None),
        ])


class IssueRefsTest(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(issue_refs("Fix #108 owner update"), [108])
        self.assertEqual(issue_refs("#18 fixing error, see #18 and #20"), [18, 20])
        self.assertEqual(issue_refs("Merge pull request #136 from x/y"), [])
        self.assertEqual(issue_refs("See https://example.com/page#12"), [])


class AttributionTest(unittest.TestCase):
    def test_focused_fix_scores_higher_than_sweeping_one(self):
        focused = attribution(n_methods=1, lines=4, total_lines=4, files_changed=1)
        sweeping = attribution(n_methods=10, lines=4, total_lines=40, files_changed=30)
        self.assertEqual(focused, 0.9091)
        self.assertLess(sweeping, 0.01)

    def test_no_line_counts_falls_back_to_focus(self):
        self.assertEqual(attribution(n_methods=2, lines=0, total_lines=0, files_changed=0), 0.25)


if __name__ == "__main__":
    unittest.main()
