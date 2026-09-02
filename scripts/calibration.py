"""从历史命中率得到经验概率。

这是最小可用的校准基线：按 score 分箱统计未来样本的上涨频率。生产版应使用
滚动窗口、足够样本量与独立样本做校准，并报告 calibration curve / Brier score。
"""
from __future__ import annotations


def calibrate_by_score(rows: list[dict], bin_width: int = 5) -> dict[int, float]:
    bins: dict[int, list[int]] = {}
    for row in rows:
        score = float(row.get("score", 0))
        label = row.get("next_day_up")
        if label is None:
            continue
        bucket = int(max(0, min(100, score)) // bin_width * bin_width)
        bins.setdefault(bucket, []).append(int(label))
    return {bucket: sum(labels) / len(labels) for bucket, labels in sorted(bins.items()) if len(labels) >= 20}
