"""
Unsupervised Audio Clip Clustering

Automatically groups similar WAV clips using MFCC features and KMeans
clustering. Much faster than labeling 282 clips one-by-one  -- label
entire clusters at once.

Pipeline:
  1. Extract audio features (MFCCs, energy, spectral) from each clip
  2. Reduce dimensions (PCA + t-SNE) for visualization
  3. Cluster with KMeans (auto-selects K via silhouette score)
  4. Interactively label clusters by listening to representative samples
  5. Output: scatter plot, annotations CSV, optional labeled folders

Dependencies (all stdlib or pre-installed):
  numpy, scipy, scikit-learn, matplotlib, wave, winsound (Windows)

Usage:
  python cluster_clips.py clips                # auto-detect best K
  python cluster_clips.py clips --clusters 5   # force 5 clusters
  python cluster_clips.py clips --no-play      # skip playback, just cluster + plot
"""

import os
import sys
import csv
import wave
import struct
import shutil
import argparse

import numpy as np
from scipy.fft import dct
from scipy.signal import get_window
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
import matplotlib
import matplotlib.pyplot as plt


# ─── Audio Loading ───────────────────────────────────────────────

def load_wav(filepath):
    """Load a WAV file and return (samples_float, sample_rate).

    Returns samples normalized to [-1.0, 1.0].
    """
    with wave.open(filepath, 'rb') as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    if sampwidth == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        samples /= 32768.0
    elif sampwidth == 1:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
        samples = (samples - 128.0) / 128.0
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

    # If stereo, take left channel
    if n_channels == 2:
        samples = samples[::2]

    return samples, rate


# ─── MFCC Feature Extraction (from scratch, no librosa) ─────────

def hz_to_mel(hz):
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def mel_to_hz(mel):
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def mel_filterbank(n_filters, n_fft, sample_rate):
    """Create a mel-spaced triangular filterbank."""
    low_mel = hz_to_mel(0)
    high_mel = hz_to_mel(sample_rate / 2)
    mel_points = np.linspace(low_mel, high_mel, n_filters + 2)
    hz_points = mel_to_hz(mel_points)

    bin_points = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)

    filters = np.zeros((n_filters, n_fft // 2 + 1))
    for i in range(n_filters):
        left = bin_points[i]
        center = bin_points[i + 1]
        right = bin_points[i + 2]

        for j in range(left, center):
            if center != left:
                filters[i, j] = (j - left) / (center - left)
        for j in range(center, right):
            if right != center:
                filters[i, j] = (right - j) / (right - center)

    return filters


def compute_mfcc(samples, sample_rate, n_mfcc=13, n_fft=512,
                 hop_length=256, n_mels=40):
    """Compute MFCCs from raw audio samples.

    Returns: (n_frames, n_mfcc) array of MFCC coefficients.
    """
    # Pre-emphasis
    emphasized = np.append(samples[0], samples[1:] - 0.97 * samples[:-1])

    # Frame the signal
    n_samples = len(emphasized)
    n_frames = 1 + (n_samples - n_fft) // hop_length
    if n_frames <= 0:
        # Too short  -- pad and take one frame
        padded = np.zeros(n_fft)
        padded[:len(emphasized)] = emphasized
        emphasized = padded
        n_frames = 1

    # Create frames using stride tricks for efficiency
    frames = np.zeros((n_frames, n_fft))
    for i in range(n_frames):
        start = i * hop_length
        end = start + n_fft
        chunk = emphasized[start:min(end, len(emphasized))]
        frames[i, :len(chunk)] = chunk

    # Window
    window = get_window('hann', n_fft)
    frames *= window

    # FFT -> power spectrum
    mag = np.abs(np.fft.rfft(frames, n=n_fft))
    power = (mag ** 2) / n_fft

    # Mel filterbank
    fb = mel_filterbank(n_mels, n_fft, sample_rate)
    mel_spec = np.dot(power, fb.T)

    # Log mel (with floor to avoid log(0))
    mel_spec = np.maximum(mel_spec, 1e-10)
    log_mel = np.log(mel_spec)

    # DCT -> MFCCs
    mfccs = dct(log_mel, type=2, axis=1, norm='ortho')[:, :n_mfcc]

    return mfccs


# ─── Additional Audio Features ───────────────────────────────────

def compute_spectral_centroid(samples, sample_rate, n_fft=512,
                               hop_length=256):
    """Compute spectral centroid per frame."""
    n_samples = len(samples)
    n_frames = max(1, 1 + (n_samples - n_fft) // hop_length)

    centroids = np.zeros(n_frames)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    for i in range(n_frames):
        start = i * hop_length
        end = start + n_fft
        frame = np.zeros(n_fft)
        chunk = samples[start:min(end, len(samples))]
        frame[:len(chunk)] = chunk

        mag = np.abs(np.fft.rfft(frame))
        mag_sum = mag.sum()
        if mag_sum > 0:
            centroids[i] = np.sum(freqs * mag) / mag_sum

    return centroids


def compute_zcr(samples, frame_length=512, hop_length=256):
    """Compute zero-crossing rate per frame."""
    n_samples = len(samples)
    n_frames = max(1, 1 + (n_samples - frame_length) // hop_length)

    zcr = np.zeros(n_frames)
    for i in range(n_frames):
        start = i * hop_length
        end = min(start + frame_length, n_samples)
        frame = samples[start:end]
        if len(frame) > 1:
            zcr[i] = np.sum(np.abs(np.diff(np.sign(frame)))) / (2 * len(frame))

    return zcr


def compute_rms(samples, frame_length=512, hop_length=256):
    """Compute RMS energy per frame."""
    n_samples = len(samples)
    n_frames = max(1, 1 + (n_samples - frame_length) // hop_length)

    rms = np.zeros(n_frames)
    for i in range(n_frames):
        start = i * hop_length
        end = min(start + frame_length, n_samples)
        frame = samples[start:end]
        rms[i] = np.sqrt(np.mean(frame ** 2))

    return rms


# ─── Feature Aggregation ─────────────────────────────────────────

def extract_features(filepath):
    """Extract a fixed-length feature vector from a WAV file.

    Returns: 1D numpy array of features (32 dimensions).
    Features: MFCC mean(13) + MFCC std(13) + energy stats(2) +
              spectral centroid stats(2) + ZCR stats(2) = 32
    """
    samples, rate = load_wav(filepath)

    # MFCCs (13 coefficients × n_frames) -> mean + std
    mfccs = compute_mfcc(samples, rate, n_mfcc=13)
    mfcc_mean = np.mean(mfccs, axis=0)   # 13
    mfcc_std = np.std(mfccs, axis=0)     # 13

    # RMS energy
    rms = compute_rms(samples)
    rms_mean = np.mean(rms)               # 1
    rms_std = np.std(rms)                  # 1

    # Spectral centroid
    centroid = compute_spectral_centroid(samples, rate)
    cent_mean = np.mean(centroid)          # 1
    cent_std = np.std(centroid)            # 1

    # Zero-crossing rate
    zcr = compute_zcr(samples)
    zcr_mean = np.mean(zcr)               # 1
    zcr_std = np.std(zcr)                  # 1

    return np.concatenate([
        mfcc_mean, mfcc_std,               # 26
        [rms_mean, rms_std],               # 2
        [cent_mean, cent_std],             # 2
        [zcr_mean, zcr_std],              # 2
    ])  # Total: 32


# ─── Clustering ──────────────────────────────────────────────────

def find_best_k(X, k_range=range(3, 11)):
    """Find optimal K for KMeans using silhouette score."""
    best_k = k_range.start
    best_score = -1

    print("  Finding optimal K...")
    for k in k_range:
        if k >= len(X):
            break
        km = KMeans(n_clusters=k, n_init=10, random_state=42)
        labels = km.fit_predict(X)
        score = silhouette_score(X, labels)
        print(f"    K={k}: silhouette={score:.3f}")
        if score > best_score:
            best_score = score
            best_k = k

    print(f"  Best K={best_k} (silhouette={best_score:.3f})")
    return best_k


# ─── Visualization ───────────────────────────────────────────────

def plot_clusters(coords_2d, labels, cluster_labels_map, output_path,
                  filenames, durations):
    """Create a scatter plot of clusters in 2D t-SNE space."""
    n_clusters = len(set(labels))
    cmap = plt.colormaps.get_cmap('tab10').resampled(n_clusters)

    fig, ax = plt.subplots(figsize=(12, 8))

    for k in range(n_clusters):
        mask = labels == k
        name = cluster_labels_map.get(k, f"Cluster {k}")
        count = np.sum(mask)
        ax.scatter(coords_2d[mask, 0], coords_2d[mask, 1],
                   c=[cmap(k)], label=f"{name} ({count})",
                   alpha=0.7, s=40, edgecolors='white', linewidth=0.5)

    ax.set_title('Dog Audio Clips  -- Unsupervised Clusters', fontsize=14)
    ax.set_xlabel('t-SNE 1')
    ax.set_ylabel('t-SNE 2')
    ax.legend(loc='best', fontsize=10)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    print(f"\nPlot saved: {output_path}")
    plt.show()


# ─── Interactive Labeling ────────────────────────────────────────

def label_clusters(filenames, labels, features_scaled, durations,
                   source_dir, play_audio=True):
    """Interactively label each cluster by playing representative samples."""
    n_clusters = len(set(labels))
    cluster_labels = {}

    if play_audio:
        import winsound

    for k in range(n_clusters):
        mask = np.where(labels == k)[0]
        count = len(mask)
        avg_dur = np.mean([durations[i] for i in mask])
        min_dur = np.min([durations[i] for i in mask])
        max_dur = np.max([durations[i] for i in mask])

        print(f"\n{'=' * 55}")
        print(f"CLUSTER {k}   --  {count} clips  "
              f"({avg_dur:.1f}s avg, {min_dur:.1f}-{max_dur:.1f}s range)")
        print(f"{'=' * 55}")

        # Find 3 samples closest to cluster centroid
        centroid = np.mean(features_scaled[mask], axis=0)
        dists = np.linalg.norm(features_scaled[mask] - centroid, axis=1)
        closest_idx = mask[np.argsort(dists)[:3]]

        print("  Representative samples:")
        for j, idx in enumerate(closest_idx):
            fname = filenames[idx]
            dur = durations[idx]
            print(f"    {j + 1}. {fname} ({dur:.1f}s)")

        if play_audio:
            for j, idx in enumerate(closest_idx):
                filepath = os.path.join(source_dir, filenames[idx])
                print(f"\n  Playing sample {j + 1}/3...", end='', flush=True)
                try:
                    winsound.PlaySound(filepath, winsound.SND_FILENAME)
                    print(" done")
                except Exception as e:
                    print(f" error: {e}")

            # Allow replay
            while True:
                choice = input("\n  [R]eplay samples, or type a label: ").strip()
                if choice.lower() == 'r':
                    for idx in closest_idx:
                        filepath = os.path.join(source_dir, filenames[idx])
                        print(f"  Playing {filenames[idx]}...", end='',
                              flush=True)
                        try:
                            winsound.PlaySound(filepath,
                                               winsound.SND_FILENAME)
                            print(" done")
                        except Exception:
                            print(" error")
                else:
                    cluster_labels[k] = choice if choice else f"cluster_{k}"
                    break
        else:
            label = input("  Label for this cluster: ").strip()
            cluster_labels[k] = label if label else f"cluster_{k}"

        print(f"  -> Labeled as: \"{cluster_labels[k]}\"")

    return cluster_labels


# ─── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Cluster WAV clips by audio similarity')
    parser.add_argument('folder', nargs='?', default='clips',
                        help='Folder containing WAV files (default: clips)')
    parser.add_argument('--clusters', '-k', type=int, default=0,
                        help='Number of clusters (0=auto, default: auto)')
    parser.add_argument('--no-play', action='store_true',
                        help='Skip audio playback during labeling')
    parser.add_argument('--no-organize', action='store_true',
                        help='Skip moving files into labeled subdirectories')
    parser.add_argument('--skip-label', action='store_true',
                        help='Skip interactive labeling (just cluster + plot)')
    args = parser.parse_args()

    source_dir = os.path.abspath(args.folder)
    if not os.path.isdir(source_dir):
        print(f"Error: '{source_dir}' is not a directory")
        sys.exit(1)

    sorted_dir = os.path.join(source_dir, 'sorted')
    os.makedirs(sorted_dir, exist_ok=True)

    # ── Find WAV files ──
    wav_files = sorted([f for f in os.listdir(source_dir)
                        if f.lower().endswith('.wav')])
    if not wav_files:
        print(f"No WAV files found in {source_dir}")
        sys.exit(0)

    print(f"Found {len(wav_files)} WAV clips in {source_dir}\n")

    # ── Step 1: Extract features ──
    print("Step 1: Extracting audio features...")
    features = []
    durations = []
    valid_files = []

    for i, fname in enumerate(wav_files):
        filepath = os.path.join(source_dir, fname)
        try:
            feat = extract_features(filepath)
            dur = 0.0
            with wave.open(filepath, 'rb') as w:
                dur = w.getnframes() / w.getframerate()
            features.append(feat)
            durations.append(dur)
            valid_files.append(fname)

            if (i + 1) % 25 == 0 or i == len(wav_files) - 1:
                print(f"  Processed {i + 1}/{len(wav_files)}")
        except Exception as e:
            print(f"  Skipping {fname}: {e}")

    X = np.array(features)
    durations = np.array(durations)
    print(f"  Feature matrix: {X.shape[0]} clips × {X.shape[1]} features\n")

    # ── Step 2: Normalize + reduce dimensions ──
    print("Step 2: Dimensionality reduction...")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # PCA to 10 dims (speeds up t-SNE and removes noise)
    n_pca = min(10, X_scaled.shape[1], X_scaled.shape[0])
    pca = PCA(n_components=n_pca, random_state=42)
    X_pca = pca.fit_transform(X_scaled)
    variance_kept = sum(pca.explained_variance_ratio_) * 100
    print(f"  PCA: {X_scaled.shape[1]}D -> {n_pca}D "
          f"({variance_kept:.1f}% variance retained)")

    # t-SNE to 2D for visualization
    perp = min(30, max(5, len(valid_files) // 5))
    tsne = TSNE(n_components=2, random_state=42, perplexity=perp)
    X_2d = tsne.fit_transform(X_pca)
    print(f"  t-SNE: {n_pca}D -> 2D (perplexity={perp})\n")

    # ── Step 3: Clustering ──
    print("Step 3: Clustering...")
    if args.clusters > 0:
        n_clusters = args.clusters
        print(f"  Using K={n_clusters} (user-specified)")
    else:
        n_clusters = find_best_k(X_pca)

    km = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    labels = km.fit_predict(X_pca)

    # Print cluster sizes
    print(f"\n  Cluster sizes:")
    for k in range(n_clusters):
        count = np.sum(labels == k)
        avg_d = np.mean(durations[labels == k])
        print(f"    Cluster {k}: {count:3d} clips  (avg {avg_d:.1f}s)")

    # ── Step 4: Label clusters ──
    if args.skip_label:
        print("\nStep 4: Skipping labeling (--skip-label)")
        cluster_labels = {k: f"cluster_{k}" for k in range(n_clusters)}
    else:
        print("\nStep 4: Label clusters")
        cluster_labels = label_clusters(
            valid_files, labels, X_scaled, durations,
            source_dir, play_audio=not args.no_play)

    # ── Step 5: Output ──
    print("\nStep 5: Saving results...")

    # Save annotations CSV
    csv_path = os.path.join(sorted_dir, 'annotations.csv')
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(
            f, fieldnames=['filename', 'cluster_id', 'label', 'duration_sec'])
        writer.writeheader()
        for i, fname in enumerate(valid_files):
            writer.writerow({
                'filename': fname,
                'cluster_id': int(labels[i]),
                'label': cluster_labels.get(labels[i], ''),
                'duration_sec': f"{durations[i]:.2f}",
            })
    print(f"  Annotations: {csv_path}")

    # Optionally organize files into labeled subdirectories
    if not args.no_organize:
        organize = input("\nMove files into labeled folders? [Y/n]: ").strip()
        if organize.lower() != 'n':
            for i, fname in enumerate(valid_files):
                label = cluster_labels.get(labels[i], f"cluster_{labels[i]}")
                # Sanitize folder name
                safe_label = "".join(
                    c if c.isalnum() or c in '-_ ' else '_' for c in label
                ).strip()
                if not safe_label:
                    safe_label = f"cluster_{labels[i]}"

                dest_dir = os.path.join(sorted_dir, safe_label)
                os.makedirs(dest_dir, exist_ok=True)
                src = os.path.join(source_dir, fname)
                dst = os.path.join(dest_dir, fname)
                if os.path.exists(src):
                    shutil.move(src, dst)

            print(f"  Files organized into {sorted_dir}/")

    # Plot
    plot_path = os.path.join(sorted_dir, 'clusters.png')
    plot_clusters(X_2d, labels, cluster_labels, plot_path,
                  valid_files, durations)

    # Summary
    print(f"\n{'=' * 55}")
    print("DONE!")
    print(f"{'=' * 55}")
    print(f"  Clips processed: {len(valid_files)}")
    print(f"  Clusters found:  {n_clusters}")
    for k in range(n_clusters):
        count = np.sum(labels == k)
        label = cluster_labels.get(k, '?')
        print(f"    {label}: {count} clips")
    print(f"  Annotations: {csv_path}")
    print(f"  Plot: {plot_path}")


if __name__ == '__main__':
    main()
