"""
Audio Clip Triage Tool

Sort through WAV clips recorded by the ESP32Watch dog monitor.
Listen to each file, decide to keep or delete, and optionally add labels
for building a ML training dataset.

Usage:
    python sort_clips.py                          # process ./clips/
    python sort_clips.py C:\\path\\to\\clips       # process specific folder

Controls:
    K = Keep (move to sorted/, optionally add a label)
    D = Delete (move to sorted/trash/, recoverable)
    R = Replay the current clip
    S = Skip (leave in place, come back later)
    Q = Quit (progress is saved)

Output:
    sorted/              - kept WAV files
    sorted/trash/        - deleted WAV files (recoverable)
    sorted/annotations.csv - labels: filename, label, duration_sec
"""

import os
import sys
import csv
import wave
import shutil
import argparse


def get_wav_duration(filepath):
    """Get duration in seconds from a WAV file."""
    try:
        with wave.open(filepath, 'rb') as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate > 0:
                return frames / rate
    except Exception:
        pass
    return 0.0


def play_wav(filepath):
    """Play a WAV file using Windows winsound (blocking)."""
    import winsound
    try:
        winsound.PlaySound(filepath, winsound.SND_FILENAME)
    except Exception as e:
        print(f"  Playback error: {e}")


def format_size(size_bytes):
    """Format file size in human-readable form."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def load_existing_annotations(csv_path):
    """Load existing annotations.csv if resuming."""
    annotations = []
    if os.path.exists(csv_path):
        with open(csv_path, 'r', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                annotations.append(row)
    return annotations


def save_annotations(csv_path, annotations):
    """Write annotations to CSV."""
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['filename', 'label', 'duration_sec'])
        writer.writeheader()
        writer.writerows(annotations)


def get_already_processed(sorted_dir, trash_dir):
    """Get set of filenames already in sorted/ or trash/ (for resume)."""
    processed = set()
    for d in [sorted_dir, trash_dir]:
        if os.path.exists(d):
            for f in os.listdir(d):
                if f.endswith('.wav'):
                    processed.add(f)
    return processed


def main():
    parser = argparse.ArgumentParser(
        description='Sort and label WAV clips for ML training')
    parser.add_argument('folder', nargs='?', default='clips',
                        help='Folder containing WAV files (default: clips)')
    args = parser.parse_args()

    source_dir = os.path.abspath(args.folder)
    if not os.path.isdir(source_dir):
        print(f"Error: '{source_dir}' is not a directory")
        sys.exit(1)

    # Setup output directories
    sorted_dir = os.path.join(source_dir, 'sorted')
    trash_dir = os.path.join(sorted_dir, 'trash')
    csv_path = os.path.join(sorted_dir, 'annotations.csv')

    os.makedirs(sorted_dir, exist_ok=True)
    os.makedirs(trash_dir, exist_ok=True)

    # Find all WAV files in source (not in sorted/ or trash/)
    all_wavs = sorted([f for f in os.listdir(source_dir)
                       if f.lower().endswith('.wav')])

    if not all_wavs:
        print(f"No WAV files found in {source_dir}")
        sys.exit(0)

    # Check which files were already processed (for resume)
    already_done = get_already_processed(sorted_dir, trash_dir)
    wavs = [f for f in all_wavs if f not in already_done]

    # Load existing annotations
    annotations = load_existing_annotations(csv_path)

    total = len(all_wavs)
    skipped_prior = len(already_done)

    if skipped_prior > 0:
        print(f"Resuming: {skipped_prior} files already processed, "
              f"{len(wavs)} remaining")

    if not wavs:
        print("All files already processed!")
        sys.exit(0)

    print(f"\nFound {len(wavs)} WAV files to process in: {source_dir}")
    print(f"Sorted files go to: {sorted_dir}")
    print(f"Controls: [K]eep  [D]elete  [R]eplay  [S]kip  [Q]uit\n")

    kept = 0
    deleted = 0
    skipped = 0

    for i, filename in enumerate(wavs):
        filepath = os.path.join(source_dir, filename)
        duration = get_wav_duration(filepath)
        size = os.path.getsize(filepath)
        file_num = skipped_prior + i + 1

        print(f"{'=' * 50}")
        print(f"[{file_num}/{total}] {filename}  "
              f"({duration:.1f}s, {format_size(size)})")
        print(f"{'=' * 50}")

        action = None
        while action is None:
            print("  Playing...", end='\r')
            play_wav(filepath)
            print("             ", end='\r')  # clear "Playing..."

            choice = input("  [K]eep  [D]elete  [R]eplay  "
                          "[S]kip  [Q]uit: ").strip().lower()

            if choice == 'r':
                continue  # replay
            elif choice == 'k':
                # Ask for label
                label = input("  Label (Enter to skip): ").strip()
                # Move to sorted/
                dest = os.path.join(sorted_dir, filename)
                shutil.move(filepath, dest)
                annotations.append({
                    'filename': filename,
                    'label': label,
                    'duration_sec': f"{duration:.2f}",
                })
                save_annotations(csv_path, annotations)
                print(f"  -> Kept: sorted/{filename}"
                      + (f"  [{label}]" if label else ""))
                kept += 1
                action = 'keep'
            elif choice == 'd':
                # Move to trash
                dest = os.path.join(trash_dir, filename)
                shutil.move(filepath, dest)
                print(f"  -> Trashed: sorted/trash/{filename}")
                deleted += 1
                action = 'delete'
            elif choice == 's':
                print(f"  -> Skipped")
                skipped += 1
                action = 'skip'
            elif choice == 'q':
                print(f"\nQuitting. Progress saved.")
                action = 'quit'
            else:
                print("  Invalid choice. Use K, D, R, S, or Q.")

        if action == 'quit':
            break

        print()

    # Summary
    print(f"\n{'=' * 50}")
    print(f"SUMMARY")
    print(f"{'=' * 50}")
    print(f"  Kept:    {kept}")
    print(f"  Deleted: {deleted}")
    print(f"  Skipped: {skipped}")
    print(f"  Total annotations: {len(annotations)}")
    if annotations:
        print(f"  Annotations saved: {csv_path}")
    print()


if __name__ == '__main__':
    main()
