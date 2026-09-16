from decimal import Decimal
from math import ceil, floor
from statistics import median


def rank_videos(videos):
    median_views = median([Decimal(v.views) for v in videos])
    rows = []
    for v in videos:
        rates = {f'{name}_rate': Decimal(getattr(v, field)) / v.views if v.views else Decimal(0)
                 for name, field in [('like', 'likes'), ('comment', 'comments'),
                                     ('share', 'shares'), ('favorite', 'favorites')]}
        rates['engagement_rate'] = (Decimal(v.likes + v.comments + v.shares + v.favorites)
                                    / v.views if v.views else Decimal(0))
        rates['outlier_score'] = Decimal(v.views) / median_views if median_views else Decimal(0)
        rows.append((v, rates))
    # Stable input order resolves a complete tie; no hidden ranking weights.
    rows.sort(key=lambda row: (row[1]['outlier_score'], row[0].views,
                              row[1]['engagement_rate']), reverse=True)
    return median_views, rows


def rank_snapshots(rows):
    """Rank global videos from their latest observed public metrics.

    ``rows`` is an iterable of (video, snapshot) pairs.  Keeping this separate
    from ``rank_videos`` preserves the input-contract used by older callers.
    """
    rows = list(rows)
    median_views = median([Decimal(snapshot.views) for _, snapshot in rows]) if rows else Decimal(0)
    ranked = []
    for video, snapshot in rows:
        views = snapshot.views
        rates = {f'{name}_rate': Decimal(getattr(snapshot, field)) / views if views else Decimal(0)
                 for name, field in [('like', 'likes'), ('comment', 'comments'),
                                     ('share', 'shares'), ('favorite', 'favorites')]}
        rates['engagement_rate'] = (Decimal(snapshot.likes + snapshot.comments + snapshot.shares + snapshot.favorites)
                                    / views if views else Decimal(0))
        rates['outlier_score'] = Decimal(views) / median_views if median_views else Decimal(0)
        ranked.append((video, snapshot, rates))
    ranked.sort(key=lambda row: (row[2]['outlier_score'], row[1].views,
                                 row[2]['engagement_rate']), reverse=True)
    return median_views, ranked


def priority_view_cutoff(rows, ratio=Decimal('0.25')):
    """Return a top-quartile view cutoff for one completed analysis scan."""
    rows = list(rows)
    target = ceil(len(rows) * float(ratio))
    if not target:
        return target, None
    ordered = sorted(rows, key=lambda row: (-row[1].views, str(row[0].id)))
    return target, ordered[target - 1][1].views


def diversified_selection(rows, budget):
    """Select unique rows across deterministic views/outlier/engagement buckets."""
    budget = max(0, min(int(budget), len(rows)))
    quotas = {'views': floor(budget * .60), 'outlier': floor(budget * .25)}
    quotas['engagement'] = budget - quotas['views'] - quotas['outlier']
    keys = {
        'views': lambda row: (-row[1].views, -row[2]['outlier_score'], -row[2]['engagement_rate'], str(row[0].id)),
        'outlier': lambda row: (-row[2]['outlier_score'], -row[1].views, -row[2]['engagement_rate'], str(row[0].id)),
        'engagement': lambda row: (-row[2]['engagement_rate'], -row[2]['outlier_score'], -row[1].views, str(row[0].id)),
        'backfill': lambda row: (-row[2]['outlier_score'], -row[1].views, -row[2]['engagement_rate'], str(row[0].id)),
    }
    selected, selected_ids = [], set()
    for reason in ('views', 'outlier', 'engagement'):
        taken = 0
        for row in sorted(rows, key=keys[reason]):
            if len(selected) >= budget or taken >= quotas[reason]:
                break
            if row[0].id not in selected_ids:
                selected.append((row, reason)); selected_ids.add(row[0].id); taken += 1
    for row in sorted(rows, key=keys['backfill']):
        if len(selected) >= budget:
            break
        if row[0].id not in selected_ids:
            selected.append((row, 'backfill')); selected_ids.add(row[0].id)
    return selected, quotas
