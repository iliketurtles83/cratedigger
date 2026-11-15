#!/usr/bin/env python3
"""07_recommend.py — Lab tool for evaluation, weight tuning, and feature export (Phase 6).

The primary consumer of recommendations is the phone music player app (offline dot product).
This CLI serves as a lab environment to:
1. Evaluate feature representations using a 20% holdout benchmark on 0faves.
2. Tune hybrid vector component weights (embedding, tempo, harmony, scalars).
3. Export the client-ready feature store (features_export.json.gz) for the server/app.
4. Inspect taste archetypes/clusters in 0faves.
5. Test track-to-track similarity queries with explainable feature breakdowns.

Usage:
    python 07_recommend.py --evaluate
    python 07_recommend.py --tune-weights
    python 07_recommend.py --export --output features_export.json.gz
    python 07_recommend.py --clusters
    python 07_recommend.py --seed "Kingdom of Heaven"
    python 07_recommend.py --backfill-hashes
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

import config
from lib.audiohash import audio_hash
from lib.embeddings import extract_embedding, get_style_classes
from lib.logger import setup_logger
from lib.recommender import (
    DEFAULT_WEIGHTS,
    RECIPE_VERSION,
    HoldoutEvaluator,
    VectorBaker,
    export_features,
)

log = setup_logger("07_recommend")


def _load_features(store_path: Path) -> pd.DataFrame:
    """Load features DataFrame from disk."""
    if not store_path.exists():
        log.error("Feature store not found at %s. Run 06_analyze.py first.", store_path)
        sys.exit(1)
    df = pd.read_pickle(store_path)
    return df


def backfill_audio_hashes(store_path: Path) -> pd.DataFrame:
    """Ensure all rows in features.pkl have audio_hash populated."""
    df = _load_features(store_path)
    log.info("Checking audio_hash for %d tracks in %s...", len(df), store_path)

    updated = 0
    hashes = list(df.get("audio_hash", [None] * len(df)))

    for i, path_str in enumerate(df["path"]):
        if pd.isna(hashes[i]) or not hashes[i]:
            try:
                h = audio_hash(path_str)
                hashes[i] = h
                updated += 1
            except Exception as exc:
                log.warning("Could not hash %s: %s", Path(path_str).name, exc)

    df["audio_hash"] = hashes
    df["schema_version"] = 8

    if updated > 0:
        df.to_pickle(store_path)
        log.info("Backfilled %d audio_hash(es) and saved to %s", updated, store_path)
    else:
        log.info("All tracks already have audio_hash populated.")

    return df


def backfill_embeddings(df: pd.DataFrame, store_path: Path, max_tracks: Optional[int] = None) -> pd.DataFrame:
    """Compute and attach Discogs-EffNet embeddings for tracks missing them."""
    if "embedding" not in df.columns:
        df["embedding"] = None

    missing_mask = df["embedding"].apply(lambda v: v is None or not isinstance(v, np.ndarray))
    missing_indices = df[missing_mask].index.tolist()

    if not missing_indices:
        log.info("All tracks already have embeddings.")
        return df

    target_indices = missing_indices[:max_tracks] if max_tracks else missing_indices
    log.info("Extracting embeddings for %d track(s)...", len(target_indices))

    for idx, i in enumerate(target_indices, start=1):
        path = df.at[i, "path"]
        try:
            emb = extract_embedding(path)
            df.at[i, "embedding"] = emb
            log.info("[%d/%d] Extracted embedding for %s", idx, len(target_indices), Path(path).name)
        except Exception as exc:
            log.warning("[%d/%d] Embedding failed for %s: %s", idx, len(target_indices), Path(path).name, exc)

    df.to_pickle(store_path)
    log.info("Saved updated embeddings to %s", store_path)
    return df


def _get_faves_dataframe(df: pd.DataFrame, playlist_path: Optional[Path] = None) -> pd.DataFrame:
    """Identify favorite tracks using rating (5 stars / >= 0.8), playlist, or 0faves path."""
    # 1. From explicit playlist file if provided
    if playlist_path and playlist_path.exists():
        try:
            lines = [l.strip() for l in playlist_path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
            stems = {Path(l).name for l in lines}
            faves = df[df["path"].apply(lambda p: Path(p).name in stems)]
            if len(faves) >= 10:
                return faves
        except Exception:
            pass

    # 2. Check Playlists/Faves.m3u8 under MUSIC_ROOT
    default_pl = getattr(config, "MUSIC_ROOT", Path(".")) / "Playlists" / "Faves.m3u8"
    if default_pl.exists():
        try:
            lines = [l.strip() for l in default_pl.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
            stems = {Path(l).name for l in lines}
            faves = df[df["path"].apply(lambda p: Path(p).name in stems)]
            if len(faves) >= 10:
                return faves
        except Exception:
            pass

    # 3. From rating column (5-star or >= 0.8)
    if "rating" in df.columns and df["rating"].notna().sum() >= 10:
        faves = df[df["rating"] >= 0.8]
        if len(faves) >= 10:
            return faves

    # 4. From path containing '0faves'
    faves = df[df["path"].str.contains("0faves", case=False, na=False)]
    if len(faves) >= 10:
        return faves

    # Fallback to entire df if no distinct subset found
    return df


def run_evaluation(df: pd.DataFrame) -> None:
    """Run holdout evaluation comparing Scalars, Embedding, and Hybrid representations."""
    faves_df = _get_faves_dataframe(df)
    print("\n" + "=" * 70)
    print(f"  PHASE 6 HOLDOUT EVALUATION (20% Holdout on {len(faves_df)} favorites)")
    print("=" * 70)

    configurations = [
        ("Scalars Only (librosa proxies)", {"scalars": 1.0, "embedding": 0.0, "tempo": 0.0, "harmonic": 0.0}),
        ("Embedding Only (Discogs-EffNet)", {"embedding": 1.0, "scalars": 0.0, "tempo": 0.0, "harmonic": 0.0}),
        ("Hybrid (Proposed Recipe)", DEFAULT_WEIGHTS),
    ]

    results = []
    for name, weights in configurations:
        baker = VectorBaker(weights=weights)
        baker.fit_scalars(df)
        evaluator = HoldoutEvaluator(baker, faves_df, holdout_ratio=0.20, random_seed=42)
        metrics = evaluator.evaluate(k_neighbors=5, top_k_list=(5, 10, 20))
        results.append((name, metrics))

    # Print markdown table
    print(f"\n| {'Representation':<32} | {'Recall@5':<10} | {'Recall@10':<10} | {'Recall@20':<10} | {'MRR':<8} | {'Mean Rank':<10} |")
    print(f"|{'-'*34}|{'-'*12}|{'-'*12}|{'-'*12}|{'-'*10}|{'-'*12}|")

    for name, m in results:
        print(
            f"| {name:<32} | {m.get('recall@5', 0.0):<10.4f} | {m.get('recall@10', 0.0):<10.4f} | "
            f"{m.get('recall@20', 0.0):<10.4f} | {m.get('mrr', 0.0):<8.4f} | {m.get('mean_rank', 0.0):<10.2f} |"
        )
    print()


def run_weight_tuning(df: pd.DataFrame) -> None:
    """Grid search over component weights to maximize holdout Recall@10 and MRR."""
    faves_df = _get_faves_dataframe(df)
    print("\n" + "=" * 70)
    print(f"  HYBRID COMPONENT WEIGHT TUNING (20% Holdout on {len(faves_df)} favorites)")
    print("=" * 70)

    candidates = [
        {"embedding": 0.80, "tempo": 0.10, "harmonic": 0.05, "scalars": 0.05},
        {"embedding": 0.70, "tempo": 0.15, "harmonic": 0.10, "scalars": 0.05},
        {"embedding": 0.60, "tempo": 0.20, "harmonic": 0.10, "scalars": 0.10},
        {"embedding": 0.50, "tempo": 0.25, "harmonic": 0.15, "scalars": 0.10},
    ]

    best_score = -1.0
    best_weights = None

    for weights in candidates:
        baker = VectorBaker(weights=weights)
        baker.fit_scalars(df)
        evaluator = HoldoutEvaluator(baker, faves_df, holdout_ratio=0.20, random_seed=42)
        m = evaluator.evaluate(k_neighbors=5, top_k_list=(10,))
        score = m.get("recall@10", 0.0) + m.get("mrr", 0.0)
        print(f"Weights {weights} -> Recall@10: {m.get('recall@10'):.4f}, MRR: {m.get('mrr'):.4f}")
        if score > best_score:
            best_score = score
            best_weights = weights

    print(f"\nBest Weights: {best_weights} (Score: {best_score:.4f})\n")


def inspect_clusters(
    df: pd.DataFrame,
    n_clusters: Optional[int] = None,
    all_tracks: bool = False,
) -> None:
    """Analyze taste clusters/archetypes across favorites or full collection."""
    target_df = df if all_tracks else _get_faves_dataframe(df)
    if n_clusters is None:
        n_clusters = 6 if all_tracks else 4

    label = "FULL COLLECTION" if all_tracks else "FAVORITES"
    print(f"\n=== TASTE CLUSTERS IN {label} (K={n_clusters}, N={len(target_df)}) ===")

    feature_cols = [
        "bpm", "energy", "loudness_lufs", "spectral_centroid",
        "zero_crossing_rate", "danceability", "speechiness",
        "acousticness", "instrumentalness", "liveness", "valence"
    ]
    X_df = target_df[feature_cols].copy()
    X_df["bpm"] = X_df["bpm"].clip(40, 260)
    X_df["energy"] = X_df["energy"].clip(0, 1)
    X_df["loudness_lufs"] = X_df["loudness_lufs"].clip(-40, 0)
    X_df["spectral_centroid"] = X_df["spectral_centroid"].clip(200, 8000)
    X_raw = X_df.fillna(X_df.median())

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_raw)

    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10).fit(X_scaled)
    df_clustered = target_df.copy()
    df_clustered["cluster"] = kmeans.labels_

    for c in range(n_clusters):
        c_df = df_clustered[df_clustered["cluster"] == c]
        avg_bpm = c_df["bpm"].clip(40, 260).mean()
        avg_energy = c_df["energy"].clip(0, 1).mean()
        avg_val = c_df["valence"].mean()
        avg_dance = c_df["danceability"].mean()
        avg_ac = c_df["acousticness"].mean()
        top_genres = c_df["genre_tag"].dropna().value_counts().head(3).to_dict()

        # Find closest track to cluster center
        center = kmeans.cluster_centers_[c]
        dists = np.linalg.norm(X_scaled[df_clustered["cluster"] == c] - center, axis=1)
        rep_idx = c_df.index[np.argmin(dists)]
        rep_row = df.loc[rep_idx]
        rep_title = Path(rep_row["path"]).name

        print(f"\n[Cluster {c}] ({len(c_df)} tracks)")
        print(f"  Avg Tempo: {avg_bpm:.1f} BPM | Avg Energy: {avg_energy:.2f} | Avg Dance: {avg_dance:.2f} | Avg Acoustic: {avg_ac:.2f} | Avg Valence: {avg_val:.2f}")
        print(f"  Top Genres: {top_genres}")
        print(f"  Representative Track: {rep_title}")


def run_query(df: pd.DataFrame, query: str, top_n: int = 5) -> None:
    """Search for a track and display the most similar tracks with dot product similarity."""
    print(f"\n=== QUERY: '{query}' (Top {top_n} Similar) ===")
    baker = VectorBaker()
    baker.fit_scalars(df)

    # Find query track in dataframe
    matches = df[df["path"].str.contains(query, case=False, na=False)]
    if matches.empty:
        matches = df[df["artist_tag"].str.contains(query, case=False, na=False)]

    if matches.empty:
        print(f"No tracks found matching query '{query}'.")
        return

    seed_idx = matches.index[0]
    seed_row = df.loc[seed_idx]
    seed_name = Path(seed_row["path"]).stem
    seed_vec = baker.bake_vector(seed_row)

    print(f"Seed Track: {seed_name}")
    print(f"  Genre: {seed_row.get('genre_tag')} | BPM: {seed_row.get('bpm')} | Key: {seed_row.get('key')} {seed_row.get('mode')}")

    vectors = np.array([baker.bake_vector(r) for _, r in df.iterrows()])
    sims = np.dot(vectors, seed_vec)

    # Exclude seed track itself
    sims[seed_idx] = -1.0
    top_indices = np.argsort(-sims)[:top_n]

    print("\nTop Recommendations:")
    for rank, idx in enumerate(top_indices, start=1):
        r = df.loc[idx]
        title = Path(r["path"]).stem
        score = sims[idx]
        r_bpm = r.get("bpm")
        s_bpm = seed_row.get("bpm")
        if pd.notna(r_bpm) and pd.notna(s_bpm):
            delta_str = f"Δ{abs(float(r_bpm) - float(s_bpm)):.1f}"
        else:
            delta_str = "ΔN/A"
        print(f" {rank}. [{score*100:.1f}% match] {title}")
        print(f"    Genre: {r.get('genre_tag')} | BPM: {r_bpm} ({delta_str}) | Key: {r.get('key')} {r.get('mode')}")


def run_visualization(
    df: pd.DataFrame,
    out_prefix: Optional[str] = None,
    n_clusters: Optional[int] = None,
    all_tracks: bool = False,
) -> None:
    """Generate both static PNG and interactive HTML visualizations of clusters."""
    import json
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import seaborn as sns
    from sklearn.manifold import TSNE
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    target_df = df.copy() if all_tracks else _get_faves_dataframe(df)
    if n_clusters is None:
        n_clusters = 6 if all_tracks else 4
    if out_prefix is None:
        out_prefix = "library_clusters" if all_tracks else "0faves_clusters"

    feature_cols = [
        "bpm", "energy", "loudness_lufs", "spectral_centroid",
        "zero_crossing_rate", "danceability", "speechiness",
        "acousticness", "instrumentalness", "liveness", "valence"
    ]

    X_df = target_df[feature_cols].copy()
    X_df["bpm"] = X_df["bpm"].clip(40, 260)
    X_df["energy"] = X_df["energy"].clip(0, 1)
    X_df["loudness_lufs"] = X_df["loudness_lufs"].clip(-40, 0)
    X_df["spectral_centroid"] = X_df["spectral_centroid"].clip(200, 8000)
    X_raw = X_df.fillna(X_df.median())

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_raw)

    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10).fit(X_scaled)
    df_plot = target_df.copy()
    df_plot["cluster"] = kmeans.labels_

    # Dimensionality reduction: use PCA initialization for speed on large datasets
    if len(target_df) > 1000:
        pca = PCA(n_components=min(10, len(feature_cols)), random_state=42)
        X_reduced = pca.fit_transform(X_scaled)
        tsne = TSNE(n_components=2, perplexity=50, max_iter=300, init="pca", n_jobs=-1, random_state=42)
        tsne_2d = tsne.fit_transform(X_reduced)
    else:
        tsne = TSNE(n_components=2, perplexity=30, random_state=42, max_iter=1000)
        tsne_2d = tsne.fit_transform(X_scaled)

    df_plot["x"] = np.round(tsne_2d[:, 0], 2)
    df_plot["y"] = np.round(tsne_2d[:, 1], 2)

    palette = sns.color_palette("tab10" if n_clusters <= 10 else "tab20", n_clusters)
    colors = [mcolors.to_hex(c) for c in palette]

    if all_tracks:
        cluster_names = {}
        for c in range(n_clusters):
            sub = df_plot[df_plot["cluster"] == c]
            top_genres = list(sub["genre_tag"].dropna().value_counts().head(2).index)
            genre_str = " / ".join(g.split(" / ")[0] for g in top_genres) if top_genres else f"Cluster {c}"
            avg_energy = sub["energy"].clip(0, 1).mean()
            avg_ac = sub["acousticness"].mean()
            avg_dance = sub["danceability"].mean()
            avg_bpm = sub["bpm"].clip(40, 260).mean()

            if avg_ac > 0.55:
                trait = "Acoustic / Ambient"
            elif avg_energy > 0.22:
                trait = "High Energy"
            elif avg_dance > 0.52 and avg_bpm < 130:
                trait = "Groove & Dance"
            elif avg_bpm > 150:
                trait = "Fast Tempo"
            else:
                trait = "Melodic Drive"
            cluster_names[c] = f"{genre_str} ({trait})"
    else:
        preset_names = {
            0: "Punk & Urban Alternative",
            1: "Uptempo Driving Rock",
            2: "Electronic & Chilled Melodic",
            3: "High-Impact Bass & Dub",
        }
        cluster_names = {c: preset_names.get(c, f"Cluster {c}") for c in range(n_clusters)}

    df_plot["cluster_name"] = df_plot["cluster"].map(cluster_names)

    # 1. Static 2-panel plot (PNG)
    fig, axes = plt.subplots(1, 2, figsize=(18, 7.5), dpi=150)
    point_size = 6 if all_tracks else 60
    point_alpha = 0.35 if all_tracks else 0.75

    ax1 = axes[0]
    for c in range(n_clusters):
        subset = df_plot[df_plot["cluster"] == c]
        ax1.scatter(
            subset["x"], subset["y"],
            label=f"{cluster_names[c]} ({len(subset):,})",
            color=palette[c], alpha=point_alpha, s=point_size, edgecolors="none",
        )

    for c in range(n_clusters):
        subset = df_plot[df_plot["cluster"] == c]
        center_x = subset["x"].median()
        center_y = subset["y"].median()
        dists = np.sqrt((subset["x"] - center_x) ** 2 + (subset["y"] - center_y) ** 2)
        top_indices = dists.nsmallest(2).index
        for idx in top_indices:
            row = df_plot.loc[idx]
            title = Path(row["path"]).stem[:25]
            ax1.annotate(
                title, (row["x"], row["y"]), fontsize=8, alpha=0.9,
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.7, edgecolor="none"),
            )

    dataset_label = f"Full Library ({len(target_df):,} tracks)" if all_tracks else f"0faves ({len(target_df):,} tracks)"
    ax1.set_title(f"{dataset_label} 2D Audio Manifold (t-SNE)", fontsize=13, fontweight="bold", pad=12)
    ax1.set_xlabel("t-SNE Dimension 1", fontsize=10)
    ax1.set_ylabel("t-SNE Dimension 2", fontsize=10)
    ax1.legend(loc="upper right", frameon=True, fontsize=8 if n_clusters > 5 else 9)
    ax1.grid(True, linestyle="--", alpha=0.3)

    ax2 = axes[1]
    compare_cols = ["danceability", "valence", "acousticness", "energy", "speechiness"]
    means_df = df_plot.groupby("cluster_name")[compare_cols].mean().T
    means_df.plot(kind="bar", ax=ax2, colormap="tab10" if n_clusters <= 10 else "tab20", width=0.8, edgecolor="black", linewidth=0.5)
    ax2.set_title("Cluster Acoustic Feature Profiles", fontsize=13, fontweight="bold", pad=12)
    ax2.set_ylabel("Mean Value (0.0 - 1.0)", fontsize=10)
    ax2.set_xlabel("Feature", fontsize=10)
    ax2.tick_params(axis="x", rotation=30)
    ax2.legend(title="Cluster", fontsize=7.5 if n_clusters > 5 else 8, title_fontsize=8.5)
    ax2.grid(True, axis="y", linestyle="--", alpha=0.3)

    plt.tight_layout()
    png_path = Path(f"{out_prefix}.png")
    plt.savefig(png_path, bbox_inches="tight")
    plt.close()
    log.info("Saved static plot to %s", png_path)

    # 2. Interactive HTML plot
    traces = []
    scatter_type = "scattergl" if all_tracks else "scatter"
    marker_size = 4 if all_tracks else 9
    marker_opacity = 0.55 if all_tracks else 0.85

    for c in range(n_clusters):
        sub = df_plot[df_plot["cluster"] == c]
        hover_texts = []
        for _, row in sub.iterrows():
            title = Path(row["path"]).stem
            artist = row.get("artist_tag") or "Unknown"
            genre = row.get("genre_tag") or "Unknown"
            bpm = f"{row.get('bpm', 0):.1f}"
            key_mode = f"{row.get('key', '')} {row.get('mode', '')}"
            energy = f"{row.get('energy', 0):.2f}"
            valence = f"{row.get('valence', 0):.2f}"
            dance = f"{row.get('danceability', 0):.2f}"
            hover = (
                f"<b>{title}</b><br>"
                f"Artist: {artist}<br>"
                f"Genre: {genre}<br>"
                f"Tempo: {bpm} BPM | Key: {key_mode}<br>"
                f"Energy: {energy} | Valence: {valence} | Dance: {dance}"
            )
            hover_texts.append(hover)

        trace = {
            "x": sub["x"].tolist(),
            "y": sub["y"].tolist(),
            "text": hover_texts,
            "hoverinfo": "text",
            "mode": "markers",
            "type": scatter_type,
            "name": f"{cluster_names[c]} ({len(sub):,})",
            "marker": {
                "size": marker_size,
                "color": colors[c],
                "opacity": marker_opacity,
            },
        }
        traces.append(trace)

    layout = {
        "title": {
            "text": f"<b>{dataset_label} Audio Manifold & Clusters</b><br><span style='font-size:12px;color:#666'>Interactive 2D t-SNE projection of {len(target_df):,} tracks</span>",
            "x": 0.05,
        },
        "hovermode": "closest",
        "plot_bgcolor": "#f9fafb",
        "paper_bgcolor": "#ffffff",
        "xaxis": {"title": "t-SNE Dimension 1", "showgrid": True, "gridcolor": "#e5e7eb"},
        "yaxis": {"title": "t-SNE Dimension 2", "showgrid": True, "gridcolor": "#e5e7eb"},
        "legend": {"title": {"text": "<b>Archetype</b>"}, "x": 1.02, "y": 1},
        "margin": {"l": 60, "r": 200, "t": 80, "b": 60},
    }

    stat_boxes = []
    for c in range(n_clusters):
        sub = df_plot[df_plot["cluster"] == c]
        top_g = ", ".join(list(sub["genre_tag"].dropna().value_counts().head(3).index))
        if len(top_g) > 40:
            top_g = top_g[:37] + "..."
        stat_boxes.append(f"""
            <div class="stat-box" style="border-color: {colors[c]};">
                <h4>{cluster_names[c]}</h4>
                <div><b>{len(sub):,} tracks</b></div>
                <div style="font-size: 11px; color: #555; margin-top: 4px;">Top: {top_g}</div>
            </div>""")

    stat_boxes_html = "\n".join(stat_boxes)
    grid_cols = min(n_clusters, 6)

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{dataset_label} Cluster Visualization</title>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 20px; background: #f3f4f6; }}
        .card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); max-width: 1400px; margin: 0 auto; }}
        #chart {{ width: 100%; height: 750px; }}
        .stats {{ display: grid; grid-template-columns: repeat({grid_cols}, 1fr); gap: 15px; margin-top: 20px; }}
        .stat-box {{ background: #f9fafb; border-left: 4px solid #ccc; padding: 12px 16px; border-radius: 6px; font-size: 13px; }}
        .stat-box h4 {{ margin: 0 0 6px 0; font-size: 13px; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="card">
        <div id="chart"></div>
        <div class="stats">
{stat_boxes_html}
        </div>
    </div>
    <script>
        var traces = {json.dumps(traces)};
        var layout = {json.dumps(layout)};
        Plotly.newPlot('chart', traces, layout, {{responsive: true}});
    </script>
</body>
</html>"""

    html_path = Path(f"{out_prefix}.html")
    html_path.write_text(html_content, encoding="utf-8")
    log.info("Saved interactive HTML plot to %s", html_path)
    print(f"\nVisualizations created successfully:\n  Static Image: {png_path}\n  Interactive HTML: {html_path}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 6 Recommender Lab & Feature Export Tool.")
    parser.add_argument("--store", type=Path, default=getattr(config, "FEATURES_STORE_PATH", Path("features.pkl")), help="Path to features.pkl")
    parser.add_argument("--backfill-hashes", action="store_true", help="Backfill audio_hash in features.pkl")
    parser.add_argument("--backfill-embeddings", action="store_true", help="Extract pretrained embeddings for features.pkl")
    parser.add_argument("--max-embeddings", type=int, default=None, help="Max tracks to compute embeddings for in this run")
    parser.add_argument("--evaluate", action="store_true", help="Run 20%% holdout evaluation benchmark")
    parser.add_argument("--tune-weights", action="store_true", help="Search for optimal hybrid vector weights")
    parser.add_argument("--export", action="store_true", help="Export features for phone/server")
    parser.add_argument("--output", type=Path, default=Path("features_export.json.gz"), help="Export output path")
    parser.add_argument("--clusters", action="store_true", help="Inspect taste archetypes in 0faves or entire collection")
    parser.add_argument("--visualize", action="store_true", help="Generate 2D static PNG and interactive HTML cluster visualizations")
    parser.add_argument("--all", action="store_true", help="Scope clusters or visualization to entire library instead of favorites")
    parser.add_argument("--n-clusters", type=int, default=None, help="Number of clusters (default: 4 for faves, 6 for --all)")
    parser.add_argument("--seed", type=str, help="Search for seed track and display similar tracks")
    parser.add_argument("--top-n", type=int, default=5, help="Number of similar tracks to return")

    args = parser.parse_args()

    if args.backfill_hashes:
        backfill_audio_hashes(args.store)
        return

    df = _load_features(args.store)

    if args.visualize:
        run_visualization(df, n_clusters=args.n_clusters, all_tracks=args.all)
        return

    if args.backfill_embeddings:
        backfill_embeddings(df, args.store, max_tracks=args.max_embeddings)
        return

    if args.evaluate:
        run_evaluation(df)
        return

    if args.tune_weights:
        run_weight_tuning(df)
        return

    if args.clusters:
        inspect_clusters(df, n_clusters=args.n_clusters, all_tracks=args.all)
        return

    if args.seed:
        run_query(df, args.seed, top_n=args.top_n)
        return

    if args.export:
        res = export_features(df, args.output)
        print(f"\nSuccessfully exported {res['track_count']} tracks ({res['dims']} dims) to {res['output_path']} ({res['size_mb']} MB)")
        return

    # If no flags provided, run a quick summary
    print(f"Loaded {len(df)} tracks from {args.store}.")
    print("Use --evaluate, --tune-weights, --clusters, --seed <query>, or --export.")


if __name__ == "__main__":
    main()
