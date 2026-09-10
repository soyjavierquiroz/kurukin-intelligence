"""Safe helpers for Phase 2A container resource sampling.

Docker can omit CPU system usage and memory fields once a container exits.  A
validation observer must record an unavailable value instead of failing and
masking the actual worker outcome.
"""

def resource_sample(stats, inspect, rss_bytes=0, previous_cpu=None):
    cpu = (stats or {}).get('cpu_stats') or {}
    usage = cpu.get('cpu_usage') or {}
    total = usage.get('total_usage')
    system = cpu.get('system_cpu_usage')
    percent = None
    if previous_cpu and isinstance(total, int) and isinstance(system, int):
        old_total, old_system = previous_cpu
        delta, system_delta = total - old_total, system - old_system
        online = cpu.get('online_cpus') or 1
        if system_delta > 0:
            percent = 100 * delta / system_delta * online
    memory = (stats or {}).get('memory_stats') or {}
    memory_stats = memory.get('stats') or {}
    usage_bytes = memory.get('usage')
    inactive = memory_stats.get('inactive_file', memory_stats.get('total_inactive_file', 0)) or 0
    return {
        'memory_bytes': max(0, usage_bytes - inactive) if isinstance(usage_bytes, int) else None,
        'rss_bytes': rss_bytes if isinstance(rss_bytes, int) else None,
        'cpu_percent': percent,
        'restart_count': (inspect or {}).get('RestartCount'),
        'oom': ((inspect or {}).get('State') or {}).get('OOMKilled'),
        'running': ((inspect or {}).get('State') or {}).get('Running'),
        'next_cpu': (total, system) if isinstance(total, int) and isinstance(system, int) else None,
    }
