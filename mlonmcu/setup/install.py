import logging
import time

from .tasks import Tasks
from .task import TaskGraph

logger = logging.getLogger('mlonmcu')

def install_dependencies(tasks_factory=Tasks, context=None, progress=False, write_cache=True):
    assert context is not None
    print("context.environment", context.environment)
    print("tasks_factory.params", tasks_factory.params)
    tasks_factory.reset_changes()
    # print("registry", tasks_factory.registry)
    # print("dependencies", tasks_factory.dependencies)
    # print("providers", tasks_factory.providers)
    task_graph = TaskGraph(tasks_factory.registry.keys(), tasks_factory.dependencies, tasks_factory.providers)
    V, E = task_graph.get_graph()
    # print("(V, E)", (V, E))
    order = task_graph.get_order()
    logger.debug("Determined dependency order: %s" % str(order))

    # skip = 1
    # print("num tasks:", len(tasks_factory.registry))
    # print("num skip:", skip)
    if progress:
        from tqdm import tqdm
        pbar = tqdm(total=len(tasks_factory.registry), desc="Installing dependencies", ncols=100, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}')
    else:
        pbar = None
    for task in order:
        func = tasks_factory.registry[task]
        func(context, progress=progress)
        time.sleep(0.1)
        if pbar:
            pbar.update(1)
    if pbar:
        pbar.close()
    if write_cache:
        cache_file = context.environment.paths["deps"].path / "cache.ini"
        context.cache.write_to_file(cache_file)

    # print(ctx._vars)
