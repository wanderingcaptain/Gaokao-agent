import os
import json
import logging
from sqlalchemy.orm import sessionmaker

# 确保能导入 models
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import University, AdmissionRecord, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Session = sessionmaker(bind=engine)
session = Session()

def run_import():
    # 1. 清空旧数据
    logger.info("Clearing old data...")
    session.query(AdmissionRecord).delete()
    session.query(University).delete()
    session.commit()
    logger.info("Old data cleared.")

    # 2. 读取官方院校代码映射
    logger.info("Loading school codes...")
    codes_path = os.path.join(os.path.dirname(__file__), 'school_codes.json')
    with open(codes_path, 'r', encoding='utf-8') as f:
        codes_data = json.load(f)
    # { "school_name": "univ_id" }
    code_map = {item["univ_name"].strip(): item["univ_id"] for item in codes_data}

    # 3. 读取高考录取数据
    logger.info("Loading gaokao scores data...")
    scores_path = os.path.join(os.path.dirname(__file__), 'guangdong_major_scores_2021_2025.json')
    with open(scores_path, 'r', encoding='utf-8') as f:
        scores_data = json.load(f)

    # 4. 收集需要写入的院校和录取记录
    universities_to_insert = {}
    records_to_insert = []
    
    for item in scores_data:
        school_name = item.get("school_name", "").strip()
        if not school_name:
            continue
            
        # 查找代码
        univ_id = code_map.get(school_name)
        if not univ_id:
            # 回退策略：使用学校名称作为 ID
            univ_id = school_name
            
        if univ_id not in universities_to_insert:
            school_type = item.get("school_type", "未知")
            if not school_type:
                school_type = "未知"
            school_nature = item.get("school_nature", "")
            is_sino_foreign = 1 if "中外合作" in school_nature or "中外合作" in item.get("genre", "") else 0
            
            universities_to_insert[univ_id] = University(
                university_id=univ_id,
                name=school_name,
                province=item.get("school_province", "未知") or "未知",
                city=item.get("school_city", "未知") or "未知",
                university_type=school_type,
                is_sino_foreign=is_sino_foreign
            )

        genre = item.get("genre", "")
        subject_type = "未知"
        if "历史" in genre:
            subject_type = "历史类"
        elif "物理" in genre:
            subject_type = "物理类"

        major_name = item.get("major", "")
        
        for record in item.get("history_scores", []):
            subtitle = record.get("major_subtitle", "").strip()
            specialized = record.get("specialized", "").strip()
            
            full_major_name = major_name
            if subtitle:
                full_major_name += f" {subtitle}"
                
            enrollment_type = "普通类"
            if "中外合作" in specialized or "中外合作" in subtitle:
                enrollment_type = "中外合作办学"
            elif "地方专项" in specialized:
                enrollment_type = "地方专项"
            elif "协同培养" in specialized:
                enrollment_type = "协同培养"

            new_record = AdmissionRecord(
                university_id=univ_id,
                university_name=school_name,
                major_name=full_major_name.strip(),
                enrollment_type=enrollment_type,
                province="广东",
                subject_type=subject_type,
                year=record.get("year"),
                min_score=record.get("min_score"),
                min_rank=record.get("min_rank"),
                enrollment_count=record.get("enroll_count")
            )
            records_to_insert.append(new_record)

    logger.info(f"Inserting {len(universities_to_insert)} universities...")
    session.bulk_save_objects(list(universities_to_insert.values()))
    session.commit()
    
    logger.info(f"Inserting {len(records_to_insert)} admission records...")
    
    # 为了避免联合唯一约束冲突，在内存中去重
    # 冲突原因：同一所学校、同一专业、同样招生类型、省份、类别、年份 有多条数据（可能是不同的 specialized 被折叠到了一个 enrollment_type）
    unique_records = {}
    for r in records_to_insert:
        key = (r.university_id, r.major_name, r.enrollment_type, r.province, r.subject_type, r.year)
        if key not in unique_records:
            unique_records[key] = r
        else:
            # 累加录取人数
            unique_records[key].enrollment_count = (unique_records[key].enrollment_count or 0) + (r.enrollment_count or 0)

    chunk_size = 5000
    records_list = list(unique_records.values())
    for i in range(0, len(records_list), chunk_size):
        session.bulk_save_objects(records_list[i:i+chunk_size])
        session.commit()
        logger.info(f"Inserted {i+len(records_list[i:i+chunk_size])} / {len(records_list)} records")

    logger.info("Import completed successfully.")

if __name__ == "__main__":
    run_import()
