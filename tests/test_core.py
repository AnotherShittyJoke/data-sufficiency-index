import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'okn-data-assessment'))
from core import audit, calculate, empty_frame

def test_empty_audit():
    assert audit(empty_frame())['total'] == 0

def test_calculate_missing_data():
    result = calculate(__import__('pandas').DataFrame([{'object_id':'1','name':'X'}]))
    assert result.iloc[0]['potential'] is None
