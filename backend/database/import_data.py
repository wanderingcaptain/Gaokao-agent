"""Import the bundled Guangdong historical snapshot without replacing existing rows."""
import argparse
import json
from pathlib import Path

from sqlalchemy.dialects.sqlite import insert

from database.models import AdmissionRecord, SessionLocal, University, init_db

DATA_DIR = Path(__file__).resolve().parent


def bundled_rows(data_dir):
    codes = json.loads((data_dir / 'school_codes.json').read_text(encoding='utf-8'))
    schools = {item['univ_name'].strip(): item['univ_id'] for item in codes}
    scores = json.loads((data_dir / 'guangdong_major_scores_2021_2025.json').read_text(encoding='utf-8'))
    universities, records = {}, {}
    for item in scores:
        name, major = item['school_name'].strip(), item['major'].strip()
        subject = {'首选物理': '物理类', '首选历史': '历史类'}.get(item['genre'])
        if not name or not major or not subject:
            continue
        university_id = schools.get(name, name)
        universities.setdefault(university_id, {
            'university_id': university_id, 'name': name,
            'province': item.get('school_province') or '未知',
            'city': item.get('school_city') or '未知',
            'university_type': item.get('school_type') or '未知',
            'is_sino_foreign': int('中外合作' in item.get('school_nature', '')),
        })
        for row in item['history_scores']:
            subtitle = row.get('major_subtitle', '').strip()
            group = row.get('specialized', '').strip()
            enrollment = next((kind for term, kind in (
                ('中外合作', '中外合作办学'), ('地方专项', '地方专项'),
                ('协同培养', '协同培养'), ('定向', '定向'), ('强基', '强基计划'),
                ('民族班', '民族班'), ('预科', '预科'),
            ) if term in group or term in subtitle or term in major), '普通类')
            key = (university_id, major, enrollment, subject, row['year'])
            entry = {
                'university_id': university_id, 'university_name': name, 'major_name': major,
                'major_category': subtitle, 'enrollment_type': enrollment, 'province': '广东',
                'subject_type': subject, 'year': row['year'],
                'min_score': row['min_score'] or None, 'min_rank': row['min_rank'] or None,
                'enrollment_count': row.get('enroll_count') or None,
            }
            previous = records.get(key)
            if previous is not None and previous != entry:
                # Multiple groups can share a key. Keep one observed rank/score pair
                # and leave the combined headcount unknown.
                entry = max((previous, entry), key=lambda r: (r['min_rank'] or 0, -(r['min_score'] or float('inf'))))
                entry['enrollment_count'] = None
            records[key] = entry
    return list(universities.values()), list(records.values())


def run_import(data_dir=DATA_DIR):
    universities, records = bundled_rows(data_dir)
    init_db()
    counts = []
    with SessionLocal.begin() as db:
        for model, rows in ((University, universities), (AdmissionRecord, records)):
            total = 0
            for offset in range(0, len(rows), 500):
                result = db.execute(insert(model.__table__).values(rows[offset:offset + 500]).on_conflict_do_nothing())
                total += result.rowcount
            counts.append(total)
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DATA_DIR)
    args = parser.parse_args()
    schools, admissions = run_import(args.data_dir)
    print(f'新增 {schools} 所院校、{admissions} 条历史记录，已有记录已保留。')
