"""R1 裸数字扫描器单测（spec-001 v1.1 §3）。"""

import re

from bellwether.ir.verify import scan_naked_numbers


def test_token_is_only_legal_number_carrier():
    assert scan_naked_numbers("收盘价为 [E1]，高于 [E23] 的水平") == []


def test_arabic_digits_outside_token_flagged():
    hits = scan_naked_numbers("毛利率提升到 45%")
    assert [h.text for h in hits] == ["4", "5"]
    assert hits[0].position == 7


def test_fullwidth_digits_flagged():
    assert scan_naked_numbers("涨了１０％") != []


def test_chinese_numerals_flagged():
    assert scan_naked_numbers("营收增长三成") != []
    assert scan_naked_numbers("市值破万亿") != []
    assert scan_naked_numbers("两个季度均下滑") != []


def test_token_digits_not_flagged_but_stray_bracket_is():
    # [E12] 合法；残缺形态 [E12 不是令牌，其中数字应被抓
    assert scan_naked_numbers("[E12]") == []
    assert scan_naked_numbers("[E12 的水平") != []


def test_whitelist_masks_matches():
    whitelist = (re.compile(r"Q[1-4]"),)
    assert scan_naked_numbers("Q3 表现见 [E4]", whitelist) == []
    assert scan_naked_numbers("Q3 增长 12%", whitelist) != []  # 白名单不豁免其他数字


def test_clean_text_passes():
    assert scan_naked_numbers("盈利能力延续改善趋势，估值处于历史区间内") == []
