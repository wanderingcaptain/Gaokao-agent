"""
规则引擎：硬性过滤，基于考生实际位次 ±1000 筛选候选学校
流程：
  1. 以考生填报的省份位次 ±1000 作为固定筛选区间
  2. 在 admission_records 中找近三年（2023~2025）中，至少一年 min_rank 落在区间内的专业
  3. 按画像中的硬性约束过滤：excluded_cities、accept_sino_foreign
  4. 返回候选列表（含近三年录取历史）
"""
from sqlalchemy.orm import Session
from database.models import University, AdmissionRecord


RECENT_YEARS = [2023, 2024, 2025]   # 参考年份
RANK_WINDOW  = 1000                  # 位次筛选窗口：考生位次 ± RANK_WINDOW


def run_rule_engine(db: Session, profile: dict) -> list[dict]:
    """
    主入口：返回候选院校专业列表。
    每项包含：大学信息 + 目标专业 + 近三年录取历史。
    """
    basic       = profile["basic_info"]
    constraints = profile["constraints"]

    province     = basic["province"]
    subject_type = basic["subject_type"]
    user_rank    = basic["province_rank"]   # 考生填报的省份位次

    # ── 1. 直接用考生位次 ± RANK_WINDOW 作为筛选区间 ──────────
    rank_high = max(1, user_rank - RANK_WINDOW)  # 位次更小（所考较强的学校）
    rank_low  = user_rank + RANK_WINDOW          # 位次更大（所考较弱的学校）
    print(f"[RuleEngine] 考生位次={user_rank}, 筛选区间=[{rank_high}, {rank_low}]", flush=True)

    # ── 2. 查询近三年有录取数据的 (university_id, major_name, enrollment_type) 组合 ──
    # 至少有一年 min_rank 落在 [rank_high, rank_low] 区间内
    qualifying_combos = (
        db.query(
            AdmissionRecord.university_id,
            AdmissionRecord.major_name,
            AdmissionRecord.enrollment_type,
        )
        .filter(
            AdmissionRecord.province == province,
            AdmissionRecord.subject_type == subject_type,
            AdmissionRecord.year.in_(RECENT_YEARS),
            AdmissionRecord.min_rank >= rank_high,
            AdmissionRecord.min_rank <= rank_low,
        )
        .distinct()
        .all()
    )

    if not qualifying_combos:
        print(f"[RuleEngine] 未找到符合条件的学校专业组合，返回空列表", flush=True)
        return []
    print(f"[RuleEngine] qualifying_combos 原始数量: {len(qualifying_combos)}", flush=True)

    # ── 3. 硬性约束过滤 ──────────────────────────────
    excluded_cities     = set(constraints.get("excluded_cities", []))
    accept_sino_foreign = constraints.get("accept_sino_foreign", False)

    # 预加载院校信息
    univ_ids = list({c.university_id for c in qualifying_combos})
    universities = {
        u.university_id: u
        for u in db.query(University).filter(University.university_id.in_(univ_ids)).all()
    }

    # ── 4. 组装结果 ───────────────────────────────────
    results = []
    for combo in qualifying_combos:
        univ = universities.get(combo.university_id)
        if univ is None:
            continue

        # 城市过滤
        if univ.city in excluded_cities:
            continue

        # 中外合作过滤
        if combo.enrollment_type == "中外合作" and not accept_sino_foreign:
            continue

        # 获取全部录取数据，供右侧详情页展示完整趋势
        history_rows = (
            db.query(AdmissionRecord)
            .filter(
                AdmissionRecord.university_id == combo.university_id,
                AdmissionRecord.major_name == combo.major_name,
                AdmissionRecord.enrollment_type == combo.enrollment_type,
                AdmissionRecord.province == province,
                AdmissionRecord.subject_type == subject_type,
            )
            .order_by(AdmissionRecord.year.asc())
            .all()
        )

        admission_history = [
            {
                "year":             r.year,
                "min_score":        r.min_score,
                "min_rank":         r.min_rank,
                "max_score":        r.max_score,
                "avg_score":        r.avg_score,
                "enrollment_count": r.enrollment_count,
                "score_diff":       r.score_diff,
            }
            for r in history_rows
        ]

        results.append({
            "university_id":   univ.university_id,
            "university_name": univ.name,
            "province":        univ.province,
            "city":            univ.city,
            "university_type": univ.university_type,
            "is_sino_foreign": bool(univ.is_sino_foreign),
            "major_name":      combo.major_name,
            "enrollment_type": combo.enrollment_type,
            "admission_history": admission_history,
            # 下方字段由 LLM 补全
            "tier":            None,
            "reason":          None,
            "enriched_data":   {},
        })

    print(f"[RuleEngine] 过滤后候选数量: {len(results)}", flush=True)

    # ── 5. 冲稳保分流与数量限制 ──
    user_rank = basic["province_rank"]
    preferred_cities = set(constraints.get("preferred_cities", []))

    chong_list = []
    wen_list = []
    bao_list = []

    for item in results:
        ranks = [h["min_rank"] for h in item["admission_history"] if h.get("min_rank")]
        if not ranks:
            # 如果没有历史位次数据，默认归为稳
            wen_list.append(item)
            continue
        avg_rank = sum(ranks) / len(ranks)

        # 按绝对位次差分类（与 ±1000 筛选窗口配套）：
        #   冲：学校平均录取位次 比 考生位次 好超过 1500 名（学校更难）
        #   保：学校平均录取位次 比 考生位次 差超过 1500 名（学校更容易）
        #   稳：其余，位次差在 ±1500 以内
        if avg_rank < user_rank - 1500:
            chong_list.append(item)
        elif avg_rank > user_rank + 1500:
            bao_list.append(item)
        else:
            wen_list.append(item)

    # 排序辅助函数：优先把考生感兴趣城市的学校排在前面，其次按位次排列
    def item_sort_key(x):
        pref_city_val = 0 if x["city"] in preferred_cities else 1
        last = x["admission_history"][-1] if x["admission_history"] else {}
        rank_val = last.get("min_rank", 999999)
        return (pref_city_val, rank_val)

    print(f"[RuleEngine] 冲={len(chong_list)} / 稳={len(wen_list)} / 保={len(bao_list)}", flush=True)

    chong_list.sort(key=item_sort_key)
    wen_list.sort(key=item_sort_key)
    bao_list.sort(key=item_sort_key)

    # 均匀选择：冲/稳/保各选最多 15 个组合，总共最多 45 个推荐项
    final_selected = []
    num_per_cat = 15
    final_selected.extend(chong_list[:num_per_cat])
    final_selected.extend(wen_list[:num_per_cat])
    final_selected.extend(bao_list[:num_per_cat])
    print(f"[RuleEngine] 最终返回候选数: {len(final_selected)}", flush=True)

    # 再次按最近一年最低录取位次进行排序，让前端展现更有序
    def final_sort_key(item):
        last = item["admission_history"][-1] if item["admission_history"] else {}
        return last.get("min_rank", 999999)

    final_selected.sort(key=final_sort_key)
    return final_selected
