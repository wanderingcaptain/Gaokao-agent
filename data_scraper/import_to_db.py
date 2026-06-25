import csv
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 添加 backend 路径到 sys.path 以便导入模型
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from database.models import University, AdmissionRecord

DB_PATH = project_root / "backend" / "database" / "gaokao.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

UNIVERSITY_CODES = {
    "北京邮电大学": "10013",
    "北京大学": "10001",
    "清华大学": "10003",
    "北京航空航天大学": "10006",
    "北京理工大学": "10007",
    "南开大学": "10055",
    "天津大学": "10056",
    "大连理工大学": "10141",
    "吉林大学": "10183",
    "复旦大学": "10246",
    "同济大学": "10247",
    "上海交通大学": "10248",
    "华东师范大学": "10269",
    "南京大学": "10284",
    "东南大学": "10286",
    "浙江大学": "10335",
    "中国科学技术大学": "10358",
    "厦门大学": "10384",
    "山东大学": "10422",
    "武汉大学": "10486",
    "华中科技大学": "10487",
    "中山大学": "10558",
    "华南理工大学": "10561",
    "重庆大学": "10611",
    "西北工业大学": "10699",
    "西安交通大学": "10701",
    "中国人民大学": "10002",
    "中南大学": "10533",
    "四川大学": "10610",
    "郑州大学": "10459",
    "中南财经政法大学": "10520",
}

def get_latest_csv() -> Path | None:
    output_dir = Path(__file__).resolve().parent / "output"
    csv_files = list(output_dir.glob("all_records_*.csv"))
    if not csv_files:
        return None
    # 获取修改时间最新的 CSV 文件
    return max(csv_files, key=lambda f: f.stat().st_mtime)

def import_data():
    csv_path = get_latest_csv()
    if not csv_path:
        print("❌ 未在 output 目录中找到汇总 CSV 文件。")
        return

    print(f"📖 正在读取最新的 CSV 数据文件: {csv_path.name}")
    
    # 1. 初始化数据库引擎
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # 2. 读取并去重 CSV 数据
        unique_records = {}
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                uni_name = row["university_name"]
                major_name = row["major_name"]
                enrollment_type = row["enrollment_type"]
                province = row["province"]
                subject_type = row["subject_type"]
                year = int(row["year"])
                
                key = (uni_name, major_name, enrollment_type, province, subject_type, year)
                
                min_score = int(row["min_score"]) if row["min_score"] else None
                min_rank = int(row["min_rank"]) if row["min_rank"] else None
                enrollment_count = int(row["enrollment_count"]) if row["enrollment_count"] else None
                score_diff = int(row["score_diff"]) if row["score_diff"] else None

                unique_records[key] = {
                    "university_name": uni_name,
                    "major_name": major_name,
                    "enrollment_type": enrollment_type,
                    "province": province,
                    "subject_type": subject_type,
                    "year": year,
                    "min_score": min_score,
                    "min_rank": min_rank,
                    "enrollment_count": enrollment_count,
                    "score_diff": score_diff
                }

        print(f"🧹 去重后共有 {len(unique_records)} 条记录待导入")

        # 3. 补全缺失的高校基础信息
        for key in unique_records.keys():
            uni_name = key[0]
            uni_code = UNIVERSITY_CODES.get(uni_name)
            if not uni_code:
                uni_code = "99999"
                print(f"⚠ 未知学校 '{uni_name}' 代码，分配临时代码 '{uni_code}'")
            
            exists = session.query(University).filter(University.university_id == uni_code).first()
            if not exists:
                print(f"🏫 补全高校基础数据: {uni_name} ({uni_code})")
                new_univ = University(
                    university_id=uni_code,
                    name=uni_name,
                    province="北京" if "北京" in uni_name else "广东",
                    city="北京" if "北京" in uni_name else "广州",
                    university_type="211" if uni_name == "北京邮电大学" else "普通本科",
                    is_sino_foreign=0,
                    official_website=f"https://www.{uni_code}.edu.cn"
                )
                session.add(new_univ)
                session.commit()

        # 4. 批量保存或更新录取记录
        inserted_count = 0
        updated_count = 0
        for rec in unique_records.values():
            uni_name = rec["university_name"]
            uni_code = UNIVERSITY_CODES.get(uni_name, "99999")
            
            # 查询是否存在该条记录
            existing_rec = session.query(AdmissionRecord).filter(
                AdmissionRecord.university_id == uni_code,
                AdmissionRecord.major_name == rec["major_name"],
                AdmissionRecord.enrollment_type == rec["enrollment_type"],
                AdmissionRecord.province == rec["province"],
                AdmissionRecord.subject_type == rec["subject_type"],
                AdmissionRecord.year == rec["year"]
            ).first()
            
            if existing_rec:
                existing_rec.min_score = rec["min_score"]
                existing_rec.min_rank = rec["min_rank"]
                existing_rec.enrollment_count = rec["enrollment_count"]
                existing_rec.score_diff = rec["score_diff"]
                updated_count += 1
            else:
                new_rec = AdmissionRecord(
                    university_id=uni_code,
                    university_name=uni_name,
                    major_name=rec["major_name"],
                    enrollment_type=rec["enrollment_type"],
                    province=rec["province"],
                    subject_type=rec["subject_type"],
                    year=rec["year"],
                    min_score=rec["min_score"],
                    min_rank=rec["min_rank"],
                    enrollment_count=rec["enrollment_count"],
                    score_diff=rec["score_diff"]
                )
                session.add(new_rec)
                inserted_count += 1

        session.commit()
        print(f"🎉 导入完成！新增 {inserted_count} 条，更新 {updated_count} 条录取记录。")

    except Exception as e:
        session.rollback()
        print(f"❌ 导入出错: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    import_data()
