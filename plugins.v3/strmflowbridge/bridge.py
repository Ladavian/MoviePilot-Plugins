"""Pure protocol helpers; this module does not import MoviePilot or start services."""
from copy import copy
from enum import Enum
from pathlib import PurePosixPath

MEDIA_EXTENSIONS = {'.mkv','.mp4','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts','.m2ts','.mts','.mpg','.mpeg','.vob','.iso','.rmvb','.rm','.asf','.divx','.f4v'}


def field(obj, key, default=None):
    """Read the same field from a V3 snapshot, runtime object, or mapping."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def value(item):
    """Return an enum's declared value without guessing media identity."""
    return item.value if isinstance(item, Enum) else item


def write(obj, key, item):
    """Write an allowed event output to a mapping or typed event payload."""
    if isinstance(obj, dict):
        obj[key] = item
    else:
        setattr(obj, key, item)


def identity(media):
    """Only TMDB movie/TV identities can be compared with StrmFlow."""
    kind = value(field(media, 'type'))
    kind = {'电影':'movie','电视剧':'tv','MOVIE':'movie','TV':'tv'}.get(kind, kind)
    source = value(field(media, 'media_source'))
    raw = field(media, 'media_id') if source == 'tmdb' else field(media, 'tmdb_id')
    try:
        tmdb_id = int(raw or 0)
    except (TypeError, ValueError):
        return None
    return (tmdb_id, kind) if tmdb_id > 0 and kind in ('movie','tv') else None


def episode_scope(meta):
    """Return one unambiguous season and its explicit episode numbers."""
    seasons = field(meta, 'season_list')
    if seasons is None:
        first = field(meta, 'begin_season')
        last = field(meta, 'end_season') or first
        seasons = [first] if first is not None and first == last else []
    episodes = field(meta, 'episode_list')
    if episodes is None:
        first = field(meta, 'begin_episode')
        last = field(meta, 'end_episode') or first
        episodes = list(range(int(first), int(last)+1)) if first and last and 0 < int(first) <= int(last) <= 100000 and int(last)-int(first) < 1000 else []
    try:
        seasons = sorted({int(s) for s in seasons})
        episodes = sorted({int(e) for e in episodes if 0 < int(e) <= 100000})
    except (TypeError, ValueError):
        return None, []
    return (seasons[0], episodes) if len(seasons) == 1 and seasons[0] >= 0 else (None, [])


def remaining_context(context, available):
    """Filter already acquired files, retaining an exact allowed-episode set for packs."""
    key = identity(field(context, 'mediainfo'))
    found = available.get(key) if key else None
    if not found:
        return context
    if key[1] == 'movie':
        return None if found.get('movie') else context
    season, episodes = episode_scope(field(context, 'meta'))
    if season is None:
        return context
    if not episodes:
        # Full-season packs may have a known season list in the typed media snapshot.
        seasons = field(field(context, 'mediainfo'), 'seasons', {}) or {}
        episodes = seasons.get(season, seasons.get(str(season), []))
        if not isinstance(episodes, (list, tuple, set)) or not episodes:
            return context
        try:
            episodes = [int(e) for e in episodes if 0 < int(e) <= 100000]
        except (TypeError, ValueError):
            return context
    have = set(found.get('episodes', {}).get(str(season), []))
    allowed = field(context, 'allowed_episodes')
    requested = set(episodes) if allowed is None else set(episodes) & set(allowed)
    missing = requested - have
    if missing == requested:
        return context
    if not missing:
        return None
    if hasattr(context, 'model_copy'):
        return context.model_copy(update={'allowed_episodes': missing})
    clone = copy(context)
    write(clone, 'allowed_episodes', missing)
    return clone


def transfer_paths(info):
    """Read target files only; a source/download path is never an upload candidate."""
    if field(info, 'success', False) is not True:
        return []
    paths = list(field(info, 'file_list_new', []) or [])
    target = field(field(info, 'target_item'), 'path')
    if target:
        paths.append(target)
    return sorted({str(p) for p in paths if isinstance(p, str) and p.startswith('/') and PurePosixPath(p).suffix.lower() in MEDIA_EXTENSIONS})
