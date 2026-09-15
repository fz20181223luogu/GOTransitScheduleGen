'''
Generate the duplicate-station replacement list using the original matching rules.

IMPORTANT: This script preserves the original matching logic used by
duplicate_stn_gen_simp.py so that the simple mapping duplicate_stn.json
remains identical to previous outputs. Additionally it writes a structured
JSON file duplicate_stn_struct.json that contains per-replacement metadata
that the schedule generator can use (for example, override priority and
rule source).

Do NOT change matching heuristics in this file unless explicitly requested.
'''

import pandas as pd
import re
import json

stops = pd.read_csv('GO-GTFS/stops.txt', usecols=['stop_id', 'stop_name'])

# Keep same variable names/logic as the original simple generator to preserve behavior
fa = dict()  # disjoint set of representative stop names, mapping each stop id to its representative stop id
id = dict()   # name -> id
name = dict() # id -> name

for index, row in stops.iterrows():
    stop_id = row['stop_id']
    stop_name = row['stop_name']
    stop_name = stop_name.replace("Brampton Innovation District","Brampton")
    id[stop_name] = stop_id
    fa[stop_id] = stop_id
    name[stop_id] = stop_name

# Original matching rules (kept verbatim):
# 1) "... GO Bus" -> replace with "GO" and map to that GO stop
# 2) "... (xxx GO)" -> extract parenthetical and map to that name
# 3) "... Bus Terminal" -> replace with "GO" and map if corresponding GO stop exists and is not already remapped
# 4) "... (xxx Bus Terminal)" -> extract parenthetical, replace "Bus Terminal"->"GO" and map if valid

for index, row in stops.iterrows():
    stop_id = row['stop_id']
    stop_name = row['stop_name']
    stop_name = stop_name.replace("Brampton Innovation District","Brampton")
    if stop_name.endswith("GO Bus"): # xxx GO Bus
        # if stop_name == "Bramalea GO Bus": print(f"Found {stop_name} with id {stop_id}")
        new_name = stop_name.replace("GO Bus","GO")
        fa[stop_id ] = fa[id[new_name]]
    elif stop_name.endswith("GO)"): # xxx @ xxx (xxx GO)
        new_name = re.search(r'\(([^()]+)\)(?!.*?\([^()]*\))', stop_name).group(1)
        fa[stop_id] = fa[id[new_name]]

for index, row in stops.iterrows():
    stop_id = row['stop_id']
    stop_name = row['stop_name']
    stop_name = stop_name.replace("Brampton Innovation District","Brampton")
    if stop_name.endswith("Bus Terminal"): # xxx Bus Terminal (Except Bramalea GO being mapped already)
        new_name = stop_name.replace("Bus Terminal","GO")
        if new_name not in id: continue # skip if the corresponding GO station does not exist
        if fa[id[new_name]] != id[new_name]: continue # skip if the GO station is already mapped to another station
        fa[stop_id] = fa[id[new_name]]
    elif stop_name.endswith("Bus Terminal)"): # xxx @ xxx (xxx Bus Terminal)
        new_name = re.search(r'\(([^()]+)\)(?!.*?\([^()]*\))', stop_name).group(1)
        new_name = new_name.replace("Bus Terminal","GO")
        if new_name not in id: continue # skip if the corresponding GO station does not exist
        if fa[id[new_name]] != id[new_name]: continue # skip if the GO station is already mapped to another station
        fa[stop_id] = fa[id[new_name]]

# Create the simple legacy mapping identical to prior outputs
simple_fa = {k: v for k, v in fa.items() if k != v}

# Also create a structured mapping with metadata for each replacement entry.
# Fields:
# - duplicate_stop_id: the stop_id to replace
# - canonical_stop_id: the stop_id it maps to
# - duplicate_name: original stop_name for duplicate_stop_id
# - canonical_name: stop_name for canonical_stop_id
# - priority: integer override priority (higher -> stronger override). Default provided per rule.
# - rule: which rule produced the mapping ("GO Bus", "Paren GO", "Bus Terminal", ...)

structured = []

# Helper to determine rule and default priority (these do not affect the simple mapping)
def detect_rule_and_priority(dup_name, can_name):
    dn = dup_name.lower()
    if dn.endswith('go bus'):
        return 'GO Bus', 100
    m = re.search(r'\(([^()]+)\)(?!.*?\([^()]*\))', dup_name)
    if m and 'go' in m.group(1).lower():
        return 'Paren GO', 90
    if dn.endswith('bus terminal') or 'bus terminal' in dup_name:
        return 'Bus Terminal', 80
    return 'Heuristic', 50

for dup_id, can_id in simple_fa.items():
    dup_name = name.get(dup_id)
    can_name = name.get(can_id)
    rule, priority = detect_rule_and_priority(dup_name, can_name)
    structured.append({
        'duplicate_stop_id': dup_id,
        'canonical_stop_id': can_id,
        'duplicate_name': dup_name,
        'canonical_name': can_name,
        'priority': priority,
        'rule': rule
    })

# Write both files: the legacy simple mapping and the new structured file
with open('duplicate_stn.json', 'w', encoding='utf-8') as f:
    json.dump(simple_fa, f, indent=4, ensure_ascii=False)

with open('duplicate_stn_struct.json', 'w', encoding='utf-8') as f:
    json.dump(structured, f, indent=4, ensure_ascii=False)

# Print same human-friendly output as before for diagnostics
for key,value in simple_fa.items():
    stop_ori = name[key]
    stop_new = name[value]
    print(f"{key} -> {value}: {stop_ori} -> {stop_new}")

print(f"Wrote {len(simple_fa)} mappings to duplicate_stn.json and duplicate_stn_struct.json")
