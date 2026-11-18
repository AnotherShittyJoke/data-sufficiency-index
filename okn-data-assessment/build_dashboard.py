"""Build a self-contained browser dashboard from a local OKN snapshot."""
from __future__ import annotations
import argparse, json
from pathlib import Path
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).parent))
from core import calculate, filter_pskov_city, save_field_values

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = [ROOT / 'data/processed/okn.csv', ROOT / 'data/raw']

def find_input(path: str | None) -> Path | None:
    if path:
        return Path(path)
    for candidate in DEFAULT_INPUTS:
        if candidate.is_file(): return candidate
        if candidate.is_dir():
            files = sorted(candidate.glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
            for file in files:
                try:
                    sample = pd.read_csv(file, nrows=20)
                    if any(c in sample.columns for c in ('object_id', 'name')) and sample.astype(str).replace('nan', '').apply(lambda s: s.str.len()).sum().sum() > 0:
                        return file
                except Exception:
                    continue
    return None

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input')
    parser.add_argument('-o', '--output', default=str(ROOT / 'dashboard.html'))
    args = parser.parse_args()
    path = find_input(args.input)
    frame = filter_pskov_city(pd.read_csv(path)) if path else pd.DataFrame()
    field_rows = []
    for _, row in frame.iterrows():
        for field, value in row.items():
            if field == 'object_id' or pd.isna(value) or str(value).strip() == '':
                continue
            field_rows.append({'value_id': f"{row['object_id']}:{field}", 'object_id': row['object_id'], 'field_name': field, 'value': value, 'source_id': row.get('source', ''), 'source_url': row.get('photo_url', ''), 'document_title': '', 'document_date': row.get('publication_date', ''), 'acquired_at': row.get('acquired_at', ''), 'verification_status': row.get('verification_status', 'требует проверки'), 'confidence': 1.0 if 'ЕГРОКН' in str(row.get('source', '')) else 0.5, 'is_current': True, 'comment': ''})
    save_field_values(pd.DataFrame(field_rows))
    metrics = calculate(frame)
    metric_columns = [c for c in ['object_id', 'assessment_index', 'overall_data_index', 'index_status', 'completeness', 'actuality', 'uncertainty', 'potential', 'threats', 'opportunities'] if c in metrics.columns]
    payload = json.dumps(frame.fillna('').merge(metrics[metric_columns], on='object_id', how='left').fillna('').to_dict(orient='records'), ensure_ascii=False)
    excluded_path = ROOT / 'data/processed/egrkn_pskov_excluded.csv'
    excluded = pd.read_csv(excluded_path).fillna('').to_dict('records') if excluded_path.exists() else []
    html = (Path(__file__).with_name('dashboard_template_v4.html').read_text(encoding='utf-8')
            .replace('/*__DATA__*/[]', payload).replace('/*__EXCLUDED__*/[]', json.dumps(excluded, ensure_ascii=False)))
    Path(args.output).write_text(html, encoding='utf-8')
    object_html = (Path(__file__).with_name('object_template.html').read_text(encoding='utf-8').replace('/*__DATA__*/[]', payload))
    Path(args.output).with_name('object.html').write_text(object_html, encoding='utf-8')
    print(f'РЎРѕР·РґР°РЅ {args.output}: {len(frame)} РѕР±СЉРµРєС‚РѕРІ')

if __name__ == '__main__': main()

