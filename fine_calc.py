"""活动超时罚款计算（纯函数，供 database / reset / utils 共用）"""
from typing import Dict, List


def parse_fine_segments(fine_rates: Dict) -> List[int]:
    """从罚款配置解析有效分钟档位并升序排列。"""
    segments: List[int] = []
    for time_key in fine_rates.keys():
        try:
            if isinstance(time_key, str) and "min" in time_key.lower():
                time_value = int(time_key.lower().replace("min", "").strip())
            else:
                time_value = int(time_key)
            segments.append(time_value)
        except (ValueError, TypeError):
            continue
    segments.sort()
    return segments


def _lookup_fine_amount(fine_rates: Dict, segment: int) -> int:
    key = str(segment)
    if key not in fine_rates:
        key = f"{segment}min"
    return int(fine_rates.get(key, 0) or 0)


def compute_activity_overtime_fine(
    fine_rates: Dict, overtime_minutes: float
) -> int:
    """
    按分段规则计算活动超时罚款。
    规则：取第一个 overtime_minutes <= segment 的档位；若超出所有档位则用最大档。
    """
    if not fine_rates or overtime_minutes <= 0:
        return 0

    segments = parse_fine_segments(fine_rates)
    if not segments:
        return 0

    for segment in segments:
        if overtime_minutes <= segment:
            return _lookup_fine_amount(fine_rates, segment)

    return _lookup_fine_amount(fine_rates, segments[-1])
