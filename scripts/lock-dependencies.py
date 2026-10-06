"""Pin the installed dependency closure for local, API, and worker environments."""
from importlib.metadata import distribution
from packaging.requirements import Requirement
from pathlib import Path


def lock(roots, filename):
    pending = [Requirement(root) for root in roots]
    seen, resolved = set(), {}
    while pending:
        requirement = pending.pop()
        key = (requirement.name.lower(), tuple(sorted(requirement.extras)))
        if key in seen:
            continue
        seen.add(key)
        dist = distribution(requirement.name)
        resolved[dist.metadata['Name'].lower().replace('_', '-')] = dist.version
        for raw in dist.requires or []:
            dependency = Requirement(raw)
            if dependency.marker is None or any(dependency.marker.evaluate({'extra': extra}) for extra in {'', *requirement.extras}):
                pending.append(dependency)
    Path(filename).write_text('# Tested dependency closure; regenerate with scripts/lock-dependencies.py\n' +
                             '\n'.join(f'{name}=={version}' for name, version in sorted(resolved.items())) + '\n')


api = ['fastapi', 'uvicorn', 'sqlalchemy', 'psycopg[binary]', 'redis', 'boto3', 'itsdangerous', 'greenlet']
lock(api, 'requirements-api.lock')
lock(api + ['yt-dlp[default]'], 'requirements-distributed.lock')
