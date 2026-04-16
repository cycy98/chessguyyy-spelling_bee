"""Validate that every word in wordlist.json has a corresponding audio file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
AUDIOS = ROOT / "audios"
WORDLIST = ROOT / "wordlist.json"

with WORDLIST.open() as f:
    data: dict = json.load(f)

all_words: set[str] = set()
for key, value in data.items():
    if key == "info" or not isinstance(value, dict):
        continue
    all_words.update(word.lower() for word in value)

missing = sorted(w for w in all_words if not (AUDIOS / f"{w}.mp3").exists())

total = len(all_words)
found = total - len(missing)
print(f"Words: {total}  |  Audio found: {found}  |  Missing: {len(missing)}")

if missing:
    print("\nMissing audio files:")
    for word in missing:
        print(f"  {word}")
    sys.exit(1)
else:
    print("All words have audio.")
