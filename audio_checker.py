from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


LEVELS = [
    "starter",
    "easy",
    "tricky",
    "advanced",
    "insane",
    "expert",
    "master",
    "sesquipedalian",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check which wordlist entries are missing .mp3 audio and which .mp3 files do not match the wordlist."
    )
    parser.add_argument(
        "--wordlist",
        type=Path,
        default=Path("wordlist.json"),
        help="Path to the wordlist JSON file. Default: wordlist.json",
    )
    parser.add_argument(
        "--audio-dir",
        type=Path,
        default=Path("audio"),
        help="Directory containing .mp3 files. Default: audio",
    )
    parser.add_argument(
        "--match-mode",
        choices=("exact", "casefold", "slug"),
        default="casefold",
        help="How to compare audio filenames to words. Default: casefold",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search the audio directory recursively.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON instead of plain text.",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-")
    return cleaned.casefold()


def normalize_name(value: str, mode: str) -> str:
    if mode == "exact":
        return value
    if mode == "casefold":
        return value.casefold()
    if mode == "slug":
        return slugify(value)
    raise ValueError(f"Unsupported match mode: {mode}")


def load_wordlist(wordlist_path: Path) -> dict[str, Any]:
    with wordlist_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def collect_expected_words(catalog: dict[str, Any], match_mode: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    expected_by_key: dict[str, str] = {}
    collisions: dict[str, list[str]] = defaultdict(list)

    for level in LEVELS:
        entries = catalog.get(level, {})
        if not isinstance(entries, dict):
            continue

        for word in entries:
            key = normalize_name(word, match_mode)
            collisions[key].append(word)
            expected_by_key.setdefault(key, word)

    duplicate_keys = {
        key: sorted(words, key=str.casefold)
        for key, words in collisions.items()
        if len(words) > 1
    }
    return expected_by_key, duplicate_keys


def collect_audio_files(audio_dir: Path, recursive: bool, match_mode: str) -> tuple[dict[str, list[str]], list[str]]:
    pattern = "**/*.mp3" if recursive else "*.mp3"
    audio_by_key: dict[str, list[str]] = defaultdict(list)

    if audio_dir.exists():
        for audio_path in sorted(audio_dir.glob(pattern)):
            if not audio_path.is_file():
                continue
            key = normalize_name(audio_path.stem, match_mode)
            audio_by_key[key].append(str(audio_path))

    duplicate_audio_keys = sorted(
        key for key, paths in audio_by_key.items() if len(paths) > 1
    )
    return dict(audio_by_key), duplicate_audio_keys


def build_report(wordlist_path: Path, audio_dir: Path, recursive: bool, match_mode: str) -> dict[str, Any]:
    catalog = load_wordlist(wordlist_path)
    expected_by_key, duplicate_word_keys = collect_expected_words(catalog, match_mode)
    audio_by_key, duplicate_audio_keys = collect_audio_files(audio_dir, recursive, match_mode)

    missing_words = sorted(
        word
        for key, word in expected_by_key.items()
        if key not in audio_by_key
    )

    unexpected_audio = sorted(
        path
        for key, paths in audio_by_key.items()
        if key not in expected_by_key
        for path in paths
    )

    duplicate_audios = {
        key: sorted(paths)
        for key, paths in audio_by_key.items()
        if len(paths) > 1
    }

    missing_by_level: dict[str, list[str]] = {}
    for level in LEVELS:
        entries = catalog.get(level, {})
        if not isinstance(entries, dict):
            continue
        level_missing = [
            word
            for word in sorted(entries.keys(), key=str.casefold)
            if normalize_name(word, match_mode) not in audio_by_key
        ]
        if level_missing:
            missing_by_level[level] = level_missing

    return {
        "wordlist": str(wordlist_path),
        "audio_dir": str(audio_dir),
        "audio_dir_exists": audio_dir.exists(),
        "recursive": recursive,
        "match_mode": match_mode,
        "expected_word_count": len(expected_by_key),
        "audio_file_count": sum(len(paths) for paths in audio_by_key.values()),
        "missing_word_count": len(missing_words),
        "unexpected_audio_count": len(unexpected_audio),
        "missing_words": missing_words,
        "missing_words_by_level": missing_by_level,
        "unexpected_audio": unexpected_audio,
        "duplicate_word_keys": duplicate_word_keys,
        "duplicate_audio_keys": duplicate_audios,
        "duplicate_audio_key_names": duplicate_audio_keys,
    }


def print_text_report(report: dict[str, Any]) -> None:
    print(f"Wordlist: {report['wordlist']}")
    print(f"Audio dir: {report['audio_dir']}")
    print(f"Match mode: {report['match_mode']}")
    print(f"Recursive: {report['recursive']}")
    print()

    if not report["audio_dir_exists"]:
        print("Audio directory does not exist.")
        print()

    print(f"Expected words: {report['expected_word_count']}")
    print(f"Audio files: {report['audio_file_count']}")
    print(f"Missing audio: {report['missing_word_count']}")
    print(f"Unexpected audio files: {report['unexpected_audio_count']}")
    print()

    if report["duplicate_word_keys"]:
        print("Word naming collisions after normalization:")
        for key, words in report["duplicate_word_keys"].items():
            print(f"  {key}: {', '.join(words)}")
        print()

    if report["duplicate_audio_keys"]:
        print("Duplicate audio matches after normalization:")
        for key, paths in report["duplicate_audio_keys"].items():
            print(f"  {key}:")
            for path in paths:
                print(f"    - {path}")
        print()

    if report["missing_words_by_level"]:
        print("Missing audio by level:")
        for level in LEVELS:
            words = report["missing_words_by_level"].get(level)
            if not words:
                continue
            print(f"  {level} ({len(words)}):")
            for word in words:
                print(f"    - {word}")
        print()
    else:
        print("No missing audio files.")
        print()

    if report["unexpected_audio"]:
        print("Unexpected audio files:")
        for path in report["unexpected_audio"]:
            print(f"  - {path}")
    else:
        print("No unexpected audio files.")


def main() -> int:
    args = parse_args()

    if not args.wordlist.exists():
        print(f"Wordlist not found: {args.wordlist}", file=sys.stderr)
        return 1

    report = build_report(
        wordlist_path=args.wordlist,
        audio_dir=args.audio_dir,
        recursive=args.recursive,
        match_mode=args.match_mode,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
