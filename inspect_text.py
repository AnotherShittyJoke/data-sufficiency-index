from pathlib import Path
s=Path('okn-data-assessment/dashboard_template_v4.html').read_text(encoding='utf8')
for line in s.splitlines():
 if '<header' in line or '<h1' in line or '<h2' in line or '<h3' in line or '<p>' in line or 'placeholder' in line or '<button' in line or '<option' in line:
  print(line[:1000])
