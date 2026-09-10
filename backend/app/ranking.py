from decimal import Decimal
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
