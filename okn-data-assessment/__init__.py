import json
from pathlib import Path
import pandas as pd

REQUIRED = ["object_id", "name", "address", "latitude", "longitude", "object_type", "heritage_category", "dating", "protection_status", "area", "building_info", "land_info", "technical_condition", "subject_of_protection", "legal_restrictions", "current_use", "urban_context", "transport_access", "available_premises", "owner_info", "source", "publication_date", "acquired_at"]
GROUPS = {
    "technical": ["technical_condition", "building_info", "area"],
    "legal": ["protection_status", "subject_of_protection", "legal_restrictions"],
    "urban": ["urban_context", "transport_access", "land_info"],
    "investment": ["current_use", "available_premises", "owner_info"],
    "spatial": ["latitude", "longitude", "address"],
    "provenance": ["source", "publication_date", "acquired_at"],
}
DEFAULT_WEIGHTS = {"technical": .25, "legal": .2, "urban": .15, "investment": .15, "spatial": .1, "provenance": .15}


def empty_frame():
    return pd.DataFrame(columns=REQUIRED)


def load_file(uploaded_file):
    name = uploaded_file.name.lower()
    if name.endswith('.csv'):
        return pd.read_csv(uploaded_file)
    if name.endswith(('.xlsx', '.xls')):
        return pd.read_excel(uploaded_file)
    if name.endswith('.json'):
        raw = json.load(uploaded_file)
        return pd.DataFrame(raw if isinstance(raw, list) else raw.get('records', raw))
    raise ValueError('Поддерживаются CSV, JSON и Excel')


def normalize(df):
    out = df.copy()
    for col in REQUIRED:
        if col not in out.columns: out[col] = pd.NA
    return out[REQUIRED].copy()


def audit(df, stale_days=730):
    if df.empty:
        return {"total": 0, "with_id": 0, "with_coordinates": 0, "fill_rate": 0.0, "stale": 0, "contradictions": 0, "unverified": 0, "missing_fields": REQUIRED.copy()}
    x = normalize(df)
    present = x.notna() & x.astype(str).ne('') & x.astype(str).ne('<NA>')
    dates = pd.to_datetime(x['publication_date'], errors='coerce')
    stale = int((dates.notna() & ((pd.Timestamp.now(tz=None)-dates).dt.days > stale_days)).sum())
    contradictions = int(((x['latitude'].notna() ^ x['longitude'].notna())).sum())
    missing = [c for c in REQUIRED if not bool(present[c].all())]
    return {"total": len(x), "with_id": int(present['object_id'].sum()), "with_coordinates": int((present['latitude'] & present['longitude']).sum()), "fill_rate": float(present.mean().mean()), "stale": stale, "contradictions": contradictions, "unverified": int((x['source'].isna() | x['source'].astype(str).eq('')).sum()), "missing_fields": missing}


def calculate(df, weights=None):
    x = normalize(df)
    weights = weights or DEFAULT_WEIGHTS
    rows=[]
    for _, r in x.iterrows():
        group_scores = {g: float(r[fields].notna().mean()) for g, fields in GROUPS.items()}
        completeness = sum(group_scores[g]*weights.get(g,0) for g in GROUPS)
        missing = [c for c in REQUIRED if pd.isna(r[c]) or str(r[c]).strip() in ('','<NA>')]
        stale = False
        if pd.notna(r['publication_date']):
            try: stale = (pd.Timestamp.now()-pd.to_datetime(r['publication_date'])).days > 730
            except Exception: pass
        uncertainty = min(1.0, len(missing)/len(REQUIRED) + (0.15 if stale else 0) + (0.1 if not r['source'] else 0))
        potential = None if completeness < .6 else round(100*(.55*completeness + .45*(1-uncertainty)),1)
        rows.append({"object_id":r['object_id'], "name":r['name'], "completeness":round(completeness*100,1), "uncertainty":round(uncertainty*100,1), "potential":potential, "missing":', '.join(missing), **{f"score_{g}":round(v*100,1) for g,v in group_scores.items()}})
    return pd.DataFrame(rows)


def apply_updates(df, selected_id, updates):
    out=normalize(df)
    mask=out['object_id'].astype(str).eq(str(selected_id))
    for key, value in updates.items():
        if key in out.columns: out.loc[mask, key] = value
    return out


def priority_table(df, updates=None):
    before=calculate(df)
    after=calculate(apply_updates(df, updates[0], updates[1])) if updates else before
    if updates:
        merged=before[['object_id','name','completeness','potential']].rename(columns={'completeness':'before_completeness','potential':'before_potential'}).merge(after[['object_id','completeness','potential']],on=['object_id','name'],suffixes=('','_after'))
        merged['gain_completeness']=merged['completeness']-merged['before_completeness']
        merged['gain_potential']=merged['potential'].fillna(merged['completeness'])-merged['before_potential'].fillna(merged['before_completeness'])
        return merged.sort_values(['gain_completeness','gain_potential'],ascending=False)
    return before.sort_values(['completeness','uncertainty'],ascending=[True,False])
