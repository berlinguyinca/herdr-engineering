"""Bounded request diagnostics. Queries, entity IDs and credentials are never retained."""
import math
import threading
from collections import Counter, deque


class RequestMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._routes = {}
        self._requests = self._slow = 0
        self._total_ms = 0.0

    @staticmethod
    def route(path):
        parts = path.split('?', 1)[0].strip('/').split('/')
        if parts[:2] != ['api', 'v1'] or len(parts) < 3:
            return '/static'
        if parts[-1] == 'stream':
            return None
        # Keep resource identity and depth, never arbitrary path segments.
        resource = parts[2]
        if resource not in {'missions', 'sessions', 'hosts', 'services', 'health', 'ingest',
                            'mission-activity', 'activity', 'events', 'analytics', 'ci',
                            'github', 'fleet', 'reviews', 'artifacts', 'publications',
                            'scheduler', 'handoffs', 'llm', 'architecture', 'collab',
                            'attention', 'review-gate', 'graph', 'mission-outcomes'}:
            resource = 'other'
        return '/api/v1/' + resource + '/:id' * min(4, len(parts) - 3)

    def record(self, path, elapsed_ms, status=200, cause=None):
        route = self.route(path)
        if route is None:
            return
        with self._lock:
            self._requests += 1
            self._total_ms += elapsed_ms
            self._slow += int(elapsed_ms >= 2000)
            item = self._routes.setdefault(route, {'count': 0, 'errors': 0, 'timeouts': 0,
                                                   'samples': deque(maxlen=200), 'statuses': Counter()})
            item['count'] += 1
            item['errors'] += int(status >= 400)
            item['timeouts'] += int(cause in {'TimeoutError', 'TimeoutExpired'})
            item['samples'].append(elapsed_ms)
            item['statuses'][str(status)] += 1

    def snapshot(self):
        with self._lock:
            routes = {}
            for route, item in self._routes.items():
                samples = list(item['samples'])
                ordered = sorted(samples)
                routes[route] = {k: item[k] for k in ('count', 'errors', 'timeouts')}
                routes[route]['statuses'] = dict(item['statuses'])
                routes[route]['latency_ms'] = {
                    'last': round(samples[-1], 2), 'max': round(max(samples), 2),
                    'p95': round(ordered[max(0, math.ceil(len(ordered) * .95) - 1)], 2),
                }
            return {'requests': self._requests, 'slow_requests': self._slow,
                    'total_ms': round(self._total_ms, 2), 'by_route': routes,
                    'by_path': {route: item['count'] for route, item in routes.items()}}
