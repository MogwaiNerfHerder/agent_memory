"""Standalone verification for the sensitivity_gate.py wiring (no pytest in
this repo). Run: python verify_sensitivity_gate.py

Checks:
  1. A real cached "Prospecting Meeting" transcript (normal business content)
     comes back eligible.
  2. A meeting typed "Personal 1:1" is excluded outright.
  3. An unrecognized/unknown meeting type is held, not guessed eligible.
  4. An otherwise-eligible meeting type whose transcript contains a
     performance-review/HR cue is held (mixed-sensitive), not partially let
     through.
"""
import glob
import json

from sensitivity_gate import classify_sensitivity

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


# 1. Real cached transcript, normal business content.
paths = glob.glob("C:/Work/marketing_commercial_intelligence/data/transcripts/*.json")
real_meeting = None
for p in paths:
    m = json.loads(open(p, encoding="utf-8").read())
    if m.get("meeting_type") == "Prospecting Meeting":
        real_meeting = m
        break
check("real transcript found for live check", real_meeting is not None)
if real_meeting:
    result = classify_sensitivity(real_meeting)
    print(f"    -> {result}")
    check("real Prospecting Meeting is eligible", result.decision == "eligible")

# 2. Excluded meeting type outright.
personal = {"meeting_type": "Personal 1:1", "transcript": [{"text": "how are you feeling this quarter"}]}
result = classify_sensitivity(personal)
print(f"    -> {result}")
check("Personal 1:1 is excluded", result.decision == "excluded")

# 3. Unknown meeting type defaults to held, never guessed eligible.
unknown = {"meeting_type": "Some New Meeting Type Nobody Has Seen", "transcript": [{"text": "totally normal sales talk"}]}
result = classify_sensitivity(unknown)
print(f"    -> {result}")
check("unrecognized meeting type is held", result.decision == "held")

# 4. Mixed-sensitive content inside an otherwise-eligible meeting type is held
#    wholesale, not partially processed.
mixed = {
    "meeting_type": "Client Working Session",
    "transcript": [
        {"text": "let's review the pipeline forecast for Q3"},
        {"text": "separately, we need to discuss his performance improvement plan"},
    ],
}
result = classify_sensitivity(mixed)
print(f"    -> {result}")
check("mixed-sensitive content is held, not partially processed", result.decision == "held")

# 5. Missing meeting_type degrades to held, not eligible.
missing = {"transcript": [{"text": "some text"}]}
result = classify_sensitivity(missing)
print(f"    -> {result}")
check("missing meeting_type is held", result.decision == "held")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    raise SystemExit(1)
print("All checks passed.")
