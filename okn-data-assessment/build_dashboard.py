"""Build a self-contained browser dashboard from a local OKN snapshot."""
from __future__ import annotations
import argparse, json
from pathlib import Path
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).parent))
from core import calculate

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
    frame = pd.read_csv(path) if path else pd.DataFrame()
    metrics = calculate(frame)
    payload = json.dumps(frame.fillna('').merge(metrics[['object_id', 'assessment_index', 'index_status', 'baseline_factor', 'completeness', 'actuality', 'uncertainty', 'potential']], on='object_id', how='left').fillna('').to_dict(orient='records'), ensure_ascii=False)
    excluded_path = ROOT / 'data/processed/egrkn_pskov_excluded.csv'
    excluded = pd.read_csv(excluded_path).fillna('').to_dict('records') if excluded_path.exists() else []
    html = (Path(__file__).with_name('dashboard_template_v4.html').read_text(encoding='utf-8')
            .replace('/*__DATA__*/[]', payload).replace('/*__EXCLUDED__*/[]', json.dumps(excluded, ensure_ascii=False)))
    Path(args.output).write_text(html, encoding='utf-8')
    object_html = (Path(__file__).with_name('object_template.html').read_text(encoding='utf-8').replace('/*__DATA__*/[]', payload))
    Path(args.output).with_name('object.html').write_text(object_html, encoding='utf-8')
    print(f'Создан {args.output}: {len(frame)} объектов')

if __name__ == '__main__': main()
