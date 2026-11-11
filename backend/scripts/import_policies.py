"""Import reviewed annual admission policies: python -m scripts.import_policies FILE [--dry-run]."""
import argparse
import json
from pathlib import Path
from database.models import init_db, SessionLocal
from services.qualification import import_policies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    payloads = json.loads(args.file.read_text())
    if not isinstance(payloads, list):
        parser.error('输入必须是规则对象数组')
    init_db()
    with SessionLocal() as db:
        count = import_policies(db, payloads)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    print(json.dumps({'validated': count, 'committed': not args.dry_run}, ensure_ascii=False))


if __name__ == '__main__':
    main()
