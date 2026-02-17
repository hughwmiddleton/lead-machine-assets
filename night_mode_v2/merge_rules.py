from typing import Dict, Iterable, List

MERGE_RULES_MAP: Dict[str, str] = {
    "Artist Name": "never_overwrite",
    "Primary Email": "fill_blank",
    "All Emails": "union_multi",
    "Status": "status_worst_wins",
    "Playcount": "max_wins",
}


def _split_multi(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        parts = [part.strip().lower() for part in value.split(";") if part.strip()]
        seen = set()
        ordered: List[str] = []
        for part in parts:
            if part not in seen:
                seen.add(part)
                ordered.append(part)
        return ordered
    return []


def _fill_blank(target_val, source_val):
    return source_val if not target_val and source_val else target_val


def _union_multi(target_val, source_val):
    combined = _split_multi(target_val) + _split_multi(source_val)
    seen = set()
    ordered: List[str] = []
    for part in combined:
        if part not in seen:
            seen.add(part)
            ordered.append(part)
    return ";".join(ordered) if ordered else target_val or source_val


def _status_worst_wins(target_val, source_val):
    ranks = {"BLOCK": 3, "WARN": 2, "OK": 1}
    if target_val in ranks and source_val in ranks:
        return target_val if ranks[target_val] >= ranks[source_val] else source_val
    if target_val in ranks:
        return target_val
    if source_val in ranks:
        return source_val
    return target_val or source_val


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _max_wins(target_val, source_val):
    t_num = _to_number(target_val)
    s_num = _to_number(source_val)
    if t_num is not None and s_num is not None:
        return t_num if t_num >= s_num else s_num
    return _fill_blank(target_val, source_val)


RULE_IMPL = {
    "fill_blank": _fill_blank,
    "union_multi": _union_multi,
    "status_worst_wins": _status_worst_wins,
    "never_overwrite": lambda t, s: t,
    "max_wins": _max_wins,
}


def apply_merge(target: Dict[str, object], source: Dict[str, object]) -> Dict[str, object]:
    result = dict(target)

    # apply mapped rules
    for col, rule_name in MERGE_RULES_MAP.items():
        impl = RULE_IMPL.get(rule_name)
        if not impl:
            continue
        result[col] = impl(target.get(col), source.get(col))

    # copy any unmapped new keys without overwriting
    for col, value in source.items():
        if col in MERGE_RULES_MAP:
            continue
        if col not in result and value:
            result[col] = value

    return result
