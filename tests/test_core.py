import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'okn-data-assessment'))
from core import audit, calculate, empty_frame, filter_pskov_city, match_records, compare_snapshots
import pandas as pd

def test_empty_audit():
    assert audit(empty_frame())['total'] == 0

def test_calculate_missing_data():
    result = calculate(pd.DataFrame([{'object_id':'1','name':'X'}]))
    assert result.iloc[0]['potential'] is None
def test_filter_pskov_city_excludes_region_records():
    result = filter_pskov_city(pd.DataFrame([{'object_id':'1','address':'г. Псков, ул. Ленина'}, {'object_id':'2','address':'г. Остров, Псковская область'}]))
    assert result.object_id.astype(str).tolist() == ['1']

def test_match_records_uses_registry_id():
    result = match_records(pd.DataFrame([{'object_id':'1','name':'A'}]), pd.DataFrame([{'object_id':'1','name':'B'}]))
    assert result.iloc[0]['status'] == 'matched'

def test_compare_snapshots_records_field_change():
    old = pd.DataFrame([{'object_id':'1','name':'A','address':'x'}])
    new = pd.DataFrame([{'object_id':'1','name':'A','address':'y'}])
    result = compare_snapshots(old, new)
    assert ((result.field_name == 'address') & (result.change_type == 'value_changed')).any()

