import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('observe_worker_resources', ROOT/'scripts/observe_worker_resources.py')
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def test_stopped_container_stats_are_safe():
    result = observer.resource_sample({}, {'RestartCount': 2, 'State': {'OOMKilled': False, 'Running': False}}, rss_bytes=None,
                                      previous_cpu=(1, 1))
    assert result['memory_bytes'] is None and result['cpu_percent'] is None
    assert result['running'] is False and result['next_cpu'] is None


def test_cpu_and_memory_sample():
    stats={'cpu_stats':{'cpu_usage':{'total_usage':300},'system_cpu_usage':200,'online_cpus':2},
           'memory_stats':{'usage':1000,'stats':{'inactive_file':200}}}
    result=observer.resource_sample(stats, {'RestartCount':0,'State':{'OOMKilled':False,'Running':True}}, 500,
                                    previous_cpu=(100, 100))
    assert result['memory_bytes']==800 and result['cpu_percent']==400
