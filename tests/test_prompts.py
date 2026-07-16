"""analyze_prompt 单测：深度模式应给更详尽、含同行/情景的指令。"""

from bellwether.agent.prompts import analyze_prompt


def test_deep_prompt_is_more_detailed():
    normal = analyze_prompt("AAPL", deep=False)
    deep = analyze_prompt("AAPL", deep=True)

    assert "AAPL" in normal and "AAPL" in deep
    assert len(deep) > len(normal)
    # 深度报告特有要素
    assert "compare_peers" in deep
    assert "情景" in deep
    assert "同行" in deep
    # 普通版不应背负这些重型要求
    assert "compare_peers" not in normal
