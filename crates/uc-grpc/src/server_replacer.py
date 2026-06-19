#!/usr/bin/env python3
"""Mechanical replacements for TaskStore refactor in server.rs"""
import re

filepath = "crates/uc-grpc/src/server.rs"

with open(filepath, "r") as f:
    content = f.read()

# 1. Replace all store.events.retain(|e| { !matches!(...) }); with store.drain_events();
# This handles multi-line retain patterns
retain_pattern = r'store\.events\.retain\(\|e\|\s*\{\s*\n\s*!matches!\(e,\s*uc_engine::AgentEventType::TaskCreated\s*\{\s*task_id,\s*\.\.\s*\}\s*if\s*task_id\.0\s*==\s*task_id_str\)\s*\n\s*\}\);'
content = re.sub(retain_pattern, "store.drain_events();", content)

# 2. Replace store.events.len() with store.pending_event_count()
content = content.replace("store.events.len()", "store.pending_event_count()")

# 3. Replace store.events[event_count_before..] slicing + iter/cloned/map patterns
# Pattern A: store.events[event_count_before..]\n    .iter()\n    .cloned()\n    .map(|e| -> TaskEvent { e.into() })\n    .collect::<Vec<_>>()
slice_pattern_a = r'store\.events\[event_count_before\.\.\]\s*\n\s*\.iter\(\)\s*\n\s*\.cloned\(\)\s*\n\s*\.map\(\|e\|\s*->\s*TaskEvent\s*\{\s*e\.into\(\)\s*\}\)\s*\n\s*\.collect::<Vec<_>>\(\)'
content = re.sub(slice_pattern_a, "store.drain_events().into_iter().map(|e| -> TaskEvent { e.into() }).collect::<Vec<_>>()", content)

# Pattern B: store.events[event_count_before..]\n    .iter()\n    .cloned()\n    .map(|e| e.into())\n    .collect()
slice_pattern_b = r'store\.events\[event_count_before\.\.\]\s*\n\s*\.iter\(\)\s*\n\s*\.cloned\(\)\s*\n\s*\.map\(\|e\|\s*e\.into\(\)\)\s*\n\s*\.collect\(\)'
content = re.sub(slice_pattern_b, "store.drain_events().into_iter().map(|e| e.into()).collect()", content)

# 4. Replace store.events.iter().cloned().map(|e| e.into()).collect()
content = content.replace(
    "store.events.iter().cloned().map(|e| e.into()).collect()",
    "store.drain_events().into_iter().map(|e| e.into()).collect()"
)

# 5. Remove event_count_before variable declarations where they become unused
# Pattern: let event_count_before = store.pending_event_count();
# followed by operations that no longer use event_count_before
# We need to be careful - only remove if event_count_before is no longer referenced

with open(filepath, "w") as f:
    f.write(content)

# Verify no old patterns remain
with open(filepath, "r") as f:
    final = f.read()

checks = [
    ("store.events.retain", "retain"),
    ("store.events.len()", "len()"),
    ("store.events[event_count_before", "slicing"),
    ("store.events.iter()", "iter()"),
]

all_clean = True
for pattern, name in checks:
    if pattern in final:
        all_clean = False
        # Find line numbers
        for i, line in enumerate(final.split("\n"), 1):
            if pattern in line:
                print(f"REMAINING {name} at line {i}: {line.strip()}")

if all_clean:
    print("All old patterns replaced successfully!")
else:
    print("Some patterns remain - check above.")
