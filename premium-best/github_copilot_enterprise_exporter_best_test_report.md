# Exporter best test report

- PASS compile check
- PASS normalize_scalar_label_value('power_user') -> 'power_user' expected 'power_user'
- PASS normalize_scalar_label_value("{'power_user'}") -> 'power_user' expected 'power_user'
- PASS normalize_scalar_label_value('["power_user"]') -> 'power_user' expected 'power_user'
- PASS normalize_scalar_label_value('{"ai_adoption_phase":"power_user"}') -> 'power_user' expected 'power_user'
- PASS normalize_scalar_label_value({'ai_adoption_phase': 'power_user'}) -> 'power_user' expected 'power_user'