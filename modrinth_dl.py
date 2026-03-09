#!/usr/bin/env python3
"""Modrinth Downloader - CLI for downloading Modrinth projects."""

import argparse
import csv
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request
from urllib.parse import quote_plus, urlencode

# Python version guard

if sys.version_info < (3, 10):
    sys.exit(
        f"Python 3.10+ required (running {sys.version_info.major}.{sys.version_info.minor})"
    )

# Version

__version__         = "1.2.0"
__author__          = "rvnka"
__python_requires__ = ">=3.10"

# Constants

API_BASE = "https://api.modrinth.com"

# User-Agent format per Modrinth docs: github_username/project_name/version
DEFAULT_USER_AGENT = (
    f"{__author__}/ModrinthDownloader/{__version__} "
    f"({__author__})"
)

# Cap workers: network I/O doesn't scale linearly and can hit rate limits.
DEFAULT_MAX_WORKERS = min(os.cpu_count() or 4, 8)

DEFAULT_TIMEOUT     = 30
DEFAULT_MAX_RETRIES = 3

# 64 KB chunks - faster than 8 KB for mod files (typically 1-50 MB).
DOWNLOAD_CHUNK_SIZE = 65_536

# Pause when X-Ratelimit-Remaining drops below this threshold.
# Modrinth allows 300 req/min; pausing at 10 remaining leaves a safety buffer.
RATELIMIT_SAFETY_MARGIN = 10

CSV_FILE = "projectid.csv"
LOG_FILE = "modrinth_dl.log"

# Project IDs that skip strict MC-version matching (backward-compatible libs).
DEFAULT_IGNORE_VERSION_IDS: list[str] = ["WwbubTsV", "sGmHWmeL"]

# Exceptions


class ModrinthError(Exception):
    """Base exception for all Modrinth-related errors."""


class NetworkError(ModrinthError):
    """Raised on network/connectivity failures."""


class APIError(ModrinthError):
    """Raised when an API request returns unexpected or erroneous data."""


class DownloadError(ModrinthError):
    """Raised when a file download fails permanently."""


class ValidationError(ModrinthError):
    """Raised when parsed data fails validation."""


class ConfigurationError(ModrinthError):
    """Raised when provided configuration is invalid."""


class RateLimitError(NetworkError):
    """Raised when the API rate limit is exceeded and cannot be recovered."""


# Enums and lookup tables


class ProjectType(str, Enum):
    """
    Project types as returned by the Modrinth v2 API.

    The official API enum is [mod, modpack, resourcepack, shader].
    'datapack' exists in practice. 'plugin' is not a valid API
    project_type - plugins are 'mod' entries with plugin-specific loaders.
    'misc' is an internal fallback for unknown values.
    """
    MOD          = "mod"
    MODPACK      = "modpack"
    RESOURCEPACK = "resourcepack"
    SHADER       = "shader"
    DATAPACK     = "datapack"
    MISC         = "misc"

    def get_folder_name(self) -> str:
        return TYPE_FOLDERS.get(self.value, "misc")


# Output sub-folder for each project type.
TYPE_FOLDERS: dict[str, str] = {
    ProjectType.MOD.value:          "mods",
    ProjectType.MODPACK.value:      "modpacks",
    ProjectType.RESOURCEPACK.value: "resourcepacks",
    ProjectType.SHADER.value:       "shaderpacks",
    ProjectType.DATAPACK.value:     "datapacks",
}

# Types that accept any MC version <= target (no exact match required).
BACKWARD_COMPAT_TYPES = frozenset({
    ProjectType.RESOURCEPACK.value,
    ProjectType.SHADER.value,
})

# Types for which we must NOT send a loader filter to the API.
# Resource packs use "minecraft" loader; shaders use their own; datapacks use "datapack".
# Sending the user-supplied loader (e.g. "fabric") would return zero results.
LOADER_AGNOSTIC_TYPES = BACKWARD_COMPAT_TYPES | {ProjectType.DATAPACK.value}

# Loader names that map downloaded mods to "plugins/" instead of "mods/".
PLUGIN_LOADERS = frozenset({
    "bukkit", "folia", "paper", "purpur", "spigot", "sponge",
})

# Valid sort indices accepted by the Modrinth search endpoint.
VALID_SORT_INDICES = ("relevance", "downloads", "follows", "newest", "updated")

# Data models


@dataclass(frozen=True)
class AppConfig:
    """Immutable, validated application configuration."""
    user_agent:         str
    max_workers:        int
    timeout:            int
    max_retries:        int
    ignore_version_ids: tuple[str, ...]
    stable_only:        bool = False  # restrict to release-type versions only

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ConfigurationError("max_workers must be >= 1")
        if self.timeout < 1:
            raise ConfigurationError("timeout must be >= 1 second")
        if self.max_retries < 1:
            raise ConfigurationError("max_retries must be >= 1")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "AppConfig":
        ignore_ids = (
            tuple(args.ignore_version_ids.split(","))
            if args.ignore_version_ids
            else tuple(DEFAULT_IGNORE_VERSION_IDS)
        )
        return cls(
            user_agent         = args.user_agent,
            max_workers        = args.workers,
            timeout            = args.timeout,
            max_retries        = args.max_retries,
            ignore_version_ids = ignore_ids,
            stable_only        = args.stable_only,
        )


@dataclass(frozen=True)
class SearchResult:
    """Immutable search result from Modrinth."""
    project_id:   str
    slug:         str
    title:        str
    description:  str
    project_type: ProjectType
    downloads:    int
    url:          str
    updated:      str              = ""         # YYYY-MM-DD
    followers:    int              = 0
    categories:   tuple[str, ...] = field(default_factory=tuple)
    client_side:  str              = "unknown"
    server_side:  str              = "unknown"
    author:       str              = ""
    license_id:   str              = ""

    def __str__(self) -> str:
        return f"{self.title} ({self.slug}) — {self.url}"


@dataclass(frozen=True)
class FileInfo:
    """Immutable descriptor for a downloadable file."""
    filename: str
    url:      str
    size:     int             = 0
    hashes:   dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadTask:
    """Immutable specification for one download job."""
    project_id:       str
    project_type:     ProjectType
    file_info:        FileInfo
    destination_path: Path
    old_path:         Path | None = None


@dataclass
class DownloadResult:
    """Outcome of a single DownloadTask execution."""
    task:    DownloadTask
    success: bool


# Protocols


class HTTPClient(Protocol):
    def get(self, url: str, headers: dict[str, str] | None = None) -> bytes: ...
    def post(self, url: str, body: dict[str, Any],
             headers: dict[str, str] | None = None) -> bytes: ...
    def download_file(self, url: str, destination: Path,
                      headers: dict[str, str] | None = None) -> bool: ...


class ProjectRepository(Protocol):
    def save(self, project_id: str, relative_path: str) -> None: ...
    def get(self, project_id: str) -> str | None: ...
    def delete(self, project_id: str) -> None: ...
    def flush(self) -> None: ...


class ModrinthAPIClient(Protocol):
    def get_project(self, project_id: str) -> dict[str, Any]: ...
    def get_projects_batch(self, ids: list[str]) -> dict[str, dict[str, Any]]: ...
    def get_versions(self, project_id: str, loader: str | None = None,
                     game_version: str | None = None,
                     project_type: ProjectType | None = None) -> list[dict[str, Any]]: ...
    def search_projects(self, query: str, filters: dict[str, str] | None = None,
                        limit: int = 10, offset: int = 0,
                        sort: str = "relevance") -> tuple[list[dict[str, Any]], int]: ...
    def get_collection(self, collection_id: str) -> list[str]: ...
    def get_latest_versions_from_hashes(
        self, hashes: list[str], algorithm: str,
        loaders: list[str], game_versions: list[str]
    ) -> dict[str, Any]: ...


# HTTP client


class StandardHTTPClient:
    """
    urllib-based HTTP client with retry logic and rate-limit handling.

    Features: exponential back-off, HTTP 429/410 handling,
    proactive throttle via X-Ratelimit-Remaining, resume support
    for downloads (HTTP Range), and SHA-512/SHA-1 hash verification.
    """

    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self._config = config
        self._logger = logger

    # helpers

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self._config.user_agent}
        if extra:
            headers.update(extra)
        return headers

    def _handle_rate_limit_headers(self, resp_headers: Any) -> None:
        """Pause if X-Ratelimit-Remaining is low."""
        remaining_raw = resp_headers.get("X-Ratelimit-Remaining")
        if remaining_raw is None:
            return
        try:
            remaining = int(remaining_raw)
        except ValueError:
            return
        if remaining < RATELIMIT_SAFETY_MARGIN:
            try:
                reset_in = max(1, int(resp_headers.get("X-Ratelimit-Reset", 5)))
            except ValueError:
                reset_in = 5
            self._logger.debug(
                f"Rate limit low ({remaining} remaining) - pausing {reset_in}s"
            )
            time.sleep(reset_in)

    # public API

    def get(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        """GET url with retry, rate-limit handling, and HTTP error distinction."""
        req_headers = self._build_headers(headers)

        for attempt in range(self._config.max_retries):
            try:
                req = request.Request(url, headers=req_headers)
                with request.urlopen(req, timeout=self._config.timeout) as resp:
                    self._handle_rate_limit_headers(resp.headers)
                    return resp.read()

            except error.HTTPError as exc:
                if exc.code == 410:
                    raise APIError(
                        f"Resource permanently gone (HTTP 410): {url} - "
                        "this API version may be deprecated"
                    ) from exc

                if exc.code == 429:
                    try:
                        retry_after = int(
                            exc.headers.get("Retry-After")
                            or exc.headers.get("X-Ratelimit-Reset")
                            or 60
                        )
                    except (ValueError, AttributeError):
                        retry_after = 60
                    self._logger.warning(
                        f"Rate limited (HTTP 429) - waiting {retry_after}s"
                    )
                    time.sleep(retry_after)
                    # 429 does not consume a retry slot
                    continue

                self._logger.warning(
                    f"HTTP {exc.code} on attempt {attempt + 1}"
                    f"/{self._config.max_retries}: {url}"
                )
                if attempt < self._config.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise NetworkError(
                        f"HTTP {exc.code} after {self._config.max_retries} "
                        f"attempts: {url}"
                    ) from exc

            except (error.URLError, OSError) as exc:
                self._logger.warning(
                    f"GET attempt {attempt + 1}/{self._config.max_retries} "
                    f"failed ({url}): {exc}"
                )
                if attempt < self._config.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise NetworkError(
                        f"Failed to fetch {url} after {self._config.max_retries} attempts"
                    ) from exc

        raise NetworkError(f"Failed to fetch {url}")  # unreachable

    def post(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """POST JSON body to url with retry and rate-limit handling."""
        req_headers = self._build_headers(headers)
        req_headers["Content-Type"] = "application/json"
        encoded = json.dumps(body).encode("utf-8")

        for attempt in range(self._config.max_retries):
            try:
                req = request.Request(
                    url, data=encoded, headers=req_headers, method="POST"
                )
                with request.urlopen(req, timeout=self._config.timeout) as resp:
                    self._handle_rate_limit_headers(resp.headers)
                    return resp.read()

            except error.HTTPError as exc:
                if exc.code == 410:
                    raise APIError(
                        f"Resource permanently gone (HTTP 410): {url}"
                    ) from exc
                if exc.code == 429:
                    try:
                        retry_after = int(
                            exc.headers.get("Retry-After")
                            or exc.headers.get("X-Ratelimit-Reset")
                            or 60
                        )
                    except (ValueError, AttributeError):
                        retry_after = 60
                    self._logger.warning(
                        f"Rate limited on POST (HTTP 429) - waiting {retry_after}s"
                    )
                    time.sleep(retry_after)
                    continue

                self._logger.warning(
                    f"HTTP {exc.code} on POST attempt {attempt + 1}"
                    f"/{self._config.max_retries}: {url}"
                )
                if attempt < self._config.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise NetworkError(
                        f"HTTP {exc.code} on POST after "
                        f"{self._config.max_retries} attempts: {url}"
                    ) from exc

            except (error.URLError, OSError) as exc:
                self._logger.warning(
                    f"POST attempt {attempt + 1}/{self._config.max_retries} "
                    f"failed ({url}): {exc}"
                )
                if attempt < self._config.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise NetworkError(
                        f"POST failed after {self._config.max_retries} attempts: {url}"
                    ) from exc

        raise NetworkError(f"POST failed: {url}")  # unreachable

    def download_file(
        self,
        url: str,
        destination: Path,
        headers: dict[str, str] | None = None,
        expected_hashes: dict[str, str] | None = None,
    ) -> bool:
        """
        Download url to destination with resume support.

        On success, verifies the file against expected_hashes (SHA-512
        preferred, SHA-1 as fallback). Removes the destination if
        verification fails.
        """
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._logger.error(
                f"Cannot create directory {destination.parent}: {exc}"
            )
            raise DownloadError(
                f"Failed to create directory {destination.parent}"
            ) from exc

        temp_path = destination.with_suffix(destination.suffix + ".part")

        for attempt in range(self._config.max_retries):
            try:
                req_headers = self._build_headers(headers)

                if temp_path.exists() and temp_path.stat().st_size > 0:
                    req_headers["Range"] = f"bytes={temp_path.stat().st_size}-"
                    open_mode = "ab"
                else:
                    open_mode = "wb"

                req = request.Request(url, headers=req_headers)
                with request.urlopen(req, timeout=self._config.timeout) as resp:
                    with open(temp_path, open_mode) as fh:
                        while chunk := resp.read(DOWNLOAD_CHUNK_SIZE):
                            fh.write(chunk)

                # Atomic promotion: temp -> final
                if destination.exists():
                    destination.unlink()
                temp_path.rename(destination)

                # Hash verification
                if expected_hashes and not self._verify_hash(
                    destination, expected_hashes
                ):
                    self._logger.error(
                        f"Hash mismatch for '{destination.name}' - "
                        "removing corrupt file"
                    )
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                    raise DownloadError(
                        f"Hash verification failed for '{destination.name}'"
                    )

                return True

            except (error.URLError, error.HTTPError, OSError) as exc:
                self._logger.warning(
                    f"Download attempt {attempt + 1}/{self._config.max_retries} "
                    f"failed for '{destination.name}': {exc}"
                )
                if attempt < self._config.max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except OSError:
                            pass
                    raise DownloadError(
                        f"Failed to download '{destination.name}' after "
                        f"{self._config.max_retries} attempts"
                    ) from exc

        return False  # unreachable

    @staticmethod
    def _verify_hash(path: Path, hashes: dict[str, str]) -> bool:
        """Return True if the file at path matches a known hash. Prefers SHA-512."""
        if "sha512" in hashes:
            algo, expected = "sha512", hashes["sha512"]
            h = hashlib.sha512()
        elif "sha1" in hashes:
            algo, expected = "sha1", hashes["sha1"]
            h = hashlib.sha1()
        else:
            return True  # No recognised algorithm - skip

        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(DOWNLOAD_CHUNK_SIZE):
                    h.update(chunk)
        except OSError:
            return False

        return h.hexdigest() == expected


# Project repository


class CSVProjectRepository:
    """
    Thread-safe CSV store mapping canonical project IDs to relative file paths.

    All mutations are staged in memory. Call flush() once after a
    download session to write a single atomic CSV update.
    """

    def __init__(self, directory: Path, logger: logging.Logger) -> None:
        self._csv_path = directory / CSV_FILE
        self._logger   = logger
        self._lock     = threading.Lock()
        self._data:  dict[str, str] = self._load()
        self._dirty: bool           = False

    def _load(self) -> dict[str, str]:
        if not self._csv_path.exists():
            return {}
        try:
            with open(self._csv_path, "r", encoding="utf-8") as fh:
                reader = csv.reader(fh, delimiter="|")
                try:
                    header = next(reader)
                    if header != ["path", "id"]:
                        self._logger.warning(
                            "Project CSV header mismatch - discarding existing data"
                        )
                        return {}
                except StopIteration:
                    return {}
                return {row[1]: row[0] for row in reader if len(row) == 2}
        except (IOError, OSError, UnicodeDecodeError) as exc:
            self._logger.warning(f"Cannot read project CSV: {exc}")
            return {}

    def _persist_locked(self) -> None:
        """Write current state to disk. Caller must hold _lock."""
        try:
            with open(self._csv_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh, delimiter="|")
                writer.writerow(["path", "id"])
                for pid, path in sorted(self._data.items()):
                    writer.writerow([path, pid])
            self._logger.debug(
                f"Project registry flushed ({len(self._data)} entries)"
            )
        except (IOError, OSError) as exc:
            self._logger.error(f"Cannot write project CSV: {exc}")
            raise ModrinthError("Failed to persist project registry") from exc

    def save(self, project_id: str, relative_path: str) -> None:
        """Stage a project->path mapping. Written to disk on flush()."""
        with self._lock:
            self._data[project_id] = relative_path
            self._dirty = True

    def get(self, project_id: str) -> str | None:
        with self._lock:
            return self._data.get(project_id)

    def get_all(self) -> dict[str, str]:
        with self._lock:
            return self._data.copy()

    def delete(self, project_id: str) -> None:
        with self._lock:
            if project_id in self._data:
                del self._data[project_id]
                self._dirty = True

    def flush(self) -> None:
        """Persist all staged changes (no-op if nothing changed)."""
        with self._lock:
            if self._dirty:
                self._persist_locked()
                self._dirty = False


# Modrinth API client


class ModrinthAPI:
    """
    Modrinth REST API v2 client.

    Includes in-memory response cache with thread-safe access,
    batch project fetch via /v2/projects?ids=[...] (up to 100 per call),
    and suppressed loader filter for loader-agnostic project types.
    """

    _BATCH_LIMIT = 100  # /v2/projects accepts at most 100 IDs per call.

    def __init__(self, http_client: HTTPClient, logger: logging.Logger) -> None:
        self._http        = http_client
        self._logger      = logger
        self._cache:      dict[str, Any] = {}
        self._cache_lock: threading.Lock = threading.Lock()

    # internal helpers

    def _get_json(self, endpoint: str) -> Any:
        """
        Fetch endpoint, parse JSON, and cache the result.

        The cache check and write are lock-protected. The HTTP fetch runs
        outside the lock to allow concurrent requests. A double-fetch for
        the same endpoint is possible but harmless since results are idempotent.
        """
        with self._cache_lock:
            if endpoint in self._cache:
                self._logger.debug(f"API cache hit: {endpoint}")
                return self._cache[endpoint]

        url = f"{API_BASE}{endpoint}"
        try:
            raw  = self._http.get(url)
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise APIError(f"Failed to parse JSON from {endpoint}") from exc

        with self._cache_lock:
            self._cache[endpoint] = data
        return data

    # public API

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Fetch a single project by canonical ID or slug."""
        result = self._get_json(f"/v2/project/{project_id}")
        if not isinstance(result, dict):
            raise APIError(f"Unexpected project response type for '{project_id}'")
        return result

    def get_projects_batch(
        self, project_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch up to _BATCH_LIMIT projects per request using /v2/projects?ids=[...].

        Returns a dict keyed by both canonical id and slug for flexible
        lookup regardless of whether the caller used IDs or slugs.
        """
        if not project_ids:
            return {}

        results: dict[str, dict[str, Any]] = {}

        for offset in range(0, len(project_ids), self._BATCH_LIMIT):
            chunk    = project_ids[offset : offset + self._BATCH_LIMIT]
            endpoint = f"/v2/projects?ids={quote_plus(json.dumps(chunk))}"

            try:
                raw = self._get_json(endpoint)
            except (APIError, NetworkError) as exc:
                self._logger.warning(
                    f"Batch fetch failed for chunk "
                    f"{offset}-{offset + len(chunk)}: {exc}"
                )
                continue

            if not isinstance(raw, list):
                self._logger.warning(
                    f"Unexpected batch response type: {type(raw).__name__}"
                )
                continue

            for project in raw:
                if not isinstance(project, dict):
                    continue
                pid  = project.get("id", "")
                slug = project.get("slug", "")
                if pid:
                    results[pid] = project
                if slug and slug != pid:
                    results[slug] = project

        unique = len({p.get("id", "") for p in results.values()})
        self._logger.debug(
            f"Batch fetch: {len(project_ids)} requested -> {unique} unique resolved"
        )
        return results

    def get_versions(
        self,
        project_id:   str,
        loader:       str | None = None,
        game_version: str | None = None,
        project_type: ProjectType | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch all versions of project_id, filtered server-side where safe.

        The loader filter is omitted for loader-agnostic types (resource packs,
        shaders, datapacks) since passing a mod loader (e.g. 'fabric') for
        those types causes the API to return an empty list.
        """
        params: dict[str, str] = {}

        is_agnostic = (
            project_type is not None
            and project_type.value in LOADER_AGNOSTIC_TYPES
        )

        if loader and not is_agnostic:
            params["loaders"] = json.dumps([loader])
        if game_version:
            params["game_versions"] = json.dumps([game_version])

        endpoint = f"/v2/project/{project_id}/version"
        if params:
            endpoint += "?" + urlencode(params)

        result = self._get_json(endpoint)
        if not isinstance(result, list):
            raise APIError(f"Unexpected versions response type for '{project_id}'")
        return result

    def search_projects(
        self,
        query:   str,
        filters: dict[str, str] | None = None,
        limit:   int = 10,
        offset:  int = 0,
        sort:    str = "relevance",
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Search for projects.

        The 'plugin' project type is virtual on Modrinth - plugins are stored
        as 'mod' with plugin-specific loaders. If project_type is 'plugin',
        this method translates it to 'mod' and adds a plugin-loader facet.

        Returns (hits, total_hits).
        """
        facets: list[list[str]] = []

        if filters:
            pt = filters.get("project_type")

            if pt == "plugin":
                # Translate: plugin -> mod + loader category for any plugin loader
                facets.append(["project_type:mod"])
                facets.append([f"categories:{ldr}" for ldr in sorted(PLUGIN_LOADERS)])
            elif pt:
                facets.append([f"project_type:{pt}"])

            if "loader" in filters:
                facets.append([f"categories:{filters['loader']}"])
            if "version" in filters:
                facets.append([f"versions:{filters['version']}"])

        params: dict[str, Any] = {
            "query":  query,
            "limit":  limit,
            "offset": offset,
            "index":  sort if sort in VALID_SORT_INDICES else "relevance",
        }
        if facets:
            params["facets"] = json.dumps(facets)

        endpoint = "/v2/search?" + urlencode(params)
        result   = self._get_json(endpoint)

        if not isinstance(result, dict) or "hits" not in result:
            raise APIError("Invalid search response structure")

        hits:       list[dict[str, Any]] = result.get("hits", [])
        total_hits: int                  = result.get("total_hits", len(hits))
        return hits, total_hits

    def get_collection(self, collection_id: str) -> list[str]:
        """
        Return the project IDs belonging to collection_id.

        Uses the v3 API endpoint, which is the only place collections
        are available in the Modrinth API.
        """
        result = self._get_json(f"/v3/collection/{collection_id}")
        if not isinstance(result, dict) or "projects" not in result:
            raise APIError(f"Invalid collection response for '{collection_id}'")
        return result.get("projects", [])

    def get_latest_versions_from_hashes(
        self,
        hashes:        list[str],
        algorithm:     str,
        loaders:       list[str],
        game_versions: list[str],
    ) -> dict[str, Any]:
        """
        POST /v2/version_files/update to bulk-check for newer compatible versions.

        Used by --update mode to check which installed files have newer releases
        available, using a single API call instead of N per-project calls.

        Returns a {hash: Version} mapping.
        """
        if not hashes:
            return {}

        url  = f"{API_BASE}/v2/version_files/update"
        body = {
            "hashes":        hashes,
            "algorithm":     algorithm,
            "loaders":       loaders,
            "game_versions": game_versions,
        }

        try:
            raw    = self._http.post(url, body)
            result = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise APIError("Failed to parse bulk version update response") from exc

        if not isinstance(result, dict):
            raise APIError(
                f"Unexpected bulk version update response type: {type(result).__name__}"
            )

        self._logger.debug(
            f"Bulk hash update: {len(hashes)} hashes -> "
            f"{len(result)} version responses"
        )
        return result


# Version matching


class VersionMatcher:
    """
    Selects the best compatible version from an API version list.

    Preference order:
    1. Featured versions are checked first (two-pass approach).
    2. Only 'listed' status versions are accepted.
    3. stable_only=True restricts to version_type 'release'.
    4. Regular types: exact MC version + loader match required.
    5. Backward-compat types (resource packs, shaders): any version <= target.
    6. Datapacks: exact MC version match, loader-agnostic.
    """

    @staticmethod
    def _parse(version: str) -> tuple[int, ...]:
        try:
            return tuple(
                int(x)
                for x in str(version).replace("-", ".").split(".")
                if x.isdigit()
            )
        except (ValueError, AttributeError):
            return (0,)

    @classmethod
    def compare(cls, v1: str, v2: str) -> int:
        """Return -1 / 0 / 1 for v1 < / = / > v2."""
        p1, p2 = cls._parse(v1), cls._parse(v2)
        return 0 if p1 == p2 else (-1 if p1 < p2 else 1)

    @classmethod
    def _is_compatible(
        cls,
        ver:                dict[str, Any],
        mc_version:         str,
        loader:             str,
        project_type:       ProjectType,
        ignore_ver:         bool,
        is_backward_compat: bool,
        is_datapack:        bool,
        stable_only:        bool,
    ) -> bool:
        """Return True if ver is compatible with the given criteria."""
        # Only accept listed versions.
        status = ver.get("status", "listed")
        if status not in ("listed", "unknown"):
            return False

        # Version type filter.
        if stable_only and ver.get("version_type") != "release":
            return False

        # Loader check (skipped for loader-agnostic types).
        if not is_backward_compat and not is_datapack:
            loaders = ver.get("loaders", [])
            if loaders and loader not in loaders:
                return False

        # MC version check.
        if not ignore_ver:
            game_versions = ver.get("game_versions", [])
            if not isinstance(game_versions, list):
                return False

            if is_backward_compat:
                if not any(
                    cls.compare(gv, mc_version) <= 0 for gv in game_versions
                ):
                    return False
            else:
                if mc_version not in game_versions:
                    return False

        return True

    @classmethod
    def find_best_version(
        cls,
        versions:           list[dict[str, Any]],
        mc_version:         str,
        loader:             str,
        project_type:       ProjectType,
        project_id:         str,
        ignore_version_ids: tuple[str, ...],
        stable_only:        bool = False,
    ) -> dict[str, Any] | None:
        """
        Two-pass search: featured-compatible versions first, then any compatible.

        Returns the first match or None.
        """
        if not versions:
            return None

        ignore_ver         = project_id in ignore_version_ids
        is_backward_compat = project_type.value in BACKWARD_COMPAT_TYPES
        is_datapack        = project_type == ProjectType.DATAPACK

        kwargs = dict(
            mc_version         = mc_version,
            loader             = loader,
            project_type       = project_type,
            ignore_ver         = ignore_ver,
            is_backward_compat = is_backward_compat,
            is_datapack        = is_datapack,
            stable_only        = stable_only,
        )

        # Pass 1: prefer featured versions.
        for ver in versions:
            if not isinstance(ver, dict):
                continue
            if ver.get("featured") and cls._is_compatible(ver, **kwargs):
                return ver

        # Pass 2: any compatible version.
        for ver in versions:
            if not isinstance(ver, dict):
                continue
            if cls._is_compatible(ver, **kwargs):
                return ver

        return None


# Modpack extractor


class ModpackExtractor:
    """Extracts .mrpack archives and downloads listed dependency files."""

    def __init__(
        self,
        http_client: HTTPClient,
        config:      AppConfig,
        logger:      logging.Logger,
    ) -> None:
        self._http   = http_client
        self._config = config
        self._logger = logger

    def extract(self, mrpack_path: Path, output_dir: Path) -> bool:
        if not mrpack_path.exists():
            self._logger.error(f"Modpack not found: {mrpack_path}")
            return False

        try:
            with zipfile.ZipFile(mrpack_path, "r") as zf:
                try:
                    with zf.open("modrinth.index.json") as fh:
                        index = json.loads(fh.read().decode("utf-8"))
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        "Invalid modpack: missing or corrupt modrinth.index.json"
                    ) from exc

                extract_path = output_dir / mrpack_path.stem
                extract_path.mkdir(parents=True, exist_ok=True)
                zf.extractall(extract_path)
                self._logger.info(f"Modpack extracted -> {extract_path}")

                self._download_dependencies(index, extract_path)
                return True

        except (zipfile.BadZipFile, OSError) as exc:
            self._logger.error(f"Modpack extraction failed: {exc}")
            raise DownloadError(f"Failed to extract {mrpack_path.name}") from exc

    def _download_dependencies(
        self, index: dict[str, Any], extract_path: Path
    ) -> None:
        files = index.get("files", [])
        if not isinstance(files, list):
            return

        self._logger.info(f"Modpack dependency files: {len(files)}")

        for entry in files:
            if not isinstance(entry, dict):
                continue
            path      = entry.get("path")
            downloads = entry.get("downloads", [])
            env       = entry.get("env", {})

            if not path or not isinstance(downloads, list):
                continue

            # Download all files regardless of env hints (users can remove unwanted ones).

            dest = extract_path / path
            file_hashes: dict[str, str] = entry.get("hashes", {})
            self._logger.debug(f"Modpack dependency: {path}")

            for url in downloads:
                if isinstance(url, str):
                    try:
                        if self._http.download_file(
                            url, dest, expected_hashes=file_hashes or None
                        ):
                            break
                    except DownloadError as exc:
                        self._logger.warning(
                            f"Dependency '{path}' download failed: {exc}"
                        )


# Formatting helpers


def _fmt_downloads(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1_024:
        return f"{n / 1_024:.1f} KB"
    return f"{n} B"


def _fmt_side(value: str) -> str:
    """Abbreviate client/server side strings for compact display."""
    return {"required": "req", "optional": "opt", "unsupported": "—"}.get(
        value, value
    )


# Search service


class SearchService:
    """Executes searches and renders results to stdout."""

    def __init__(self, api: ModrinthAPI, logger: logging.Logger) -> None:
        self._api    = api
        self._logger = logger

    def search(
        self,
        query:   str,
        filters: dict[str, str] | None = None,
        limit:   int = 10,
        offset:  int = 0,
        sort:    str = "relevance",
    ) -> tuple[list[SearchResult], int]:
        self._logger.debug(
            f"Search: query={query!r}  filters={filters}  "
            f"limit={limit}  offset={offset}  sort={sort}"
        )

        hits, total_hits = self._api.search_projects(
            query, filters, limit, offset, sort
        )
        self._logger.debug(
            f"Search response: {len(hits)} hits / {total_hits} total"
        )

        results: list[SearchResult] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            try:
                pt_raw = hit.get("project_type", "mod")
                try:
                    pt = ProjectType(pt_raw)
                except ValueError:
                    pt = ProjectType.MISC

                results.append(SearchResult(
                    project_id   = hit.get("project_id", ""),
                    slug         = hit.get("slug", ""),
                    title        = hit.get("title", "Unknown"),
                    description  = hit.get("description", ""),
                    project_type = pt,
                    downloads    = hit.get("downloads", 0),
                    url          = (
                        f"https://modrinth.com/{pt_raw}/{hit.get('slug','')}"
                    ),
                    updated      = hit.get("date_modified", "")[:10],
                    followers    = hit.get("follows", 0),
                    categories   = tuple(hit.get("categories", [])),
                    client_side  = hit.get("client_side", "unknown"),
                    server_side  = hit.get("server_side", "unknown"),
                    author       = hit.get("author", ""),
                    license_id   = hit.get("license", ""),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                self._logger.debug(f"Skipping malformed search hit: {exc}")

        return results, total_hits

    def display_results(
        self,
        results:    list[SearchResult],
        total_hits: int,
        query:      str,
        filters:    dict[str, str] | None = None,
        offset:     int = 0,
    ) -> None:
        if not results:
            print("No results found.")
            return

        ctx_parts: list[str] = []
        if filters:
            if "loader"       in filters: ctx_parts.append(filters["loader"])
            if "version"      in filters: ctx_parts.append(filters["version"])
            if "project_type" in filters: ctx_parts.append(filters["project_type"])
        ctx = "  ·  ".join(ctx_parts) if ctx_parts else "all types"

        SEP   = "─" * 60
        start = offset + 1
        end   = offset + len(results)
        print(
            f"\nShowing {start}-{end} of {total_hits:,} results "
            f'for "{query}"  ({ctx})\n'
        )

        for i, r in enumerate(results, start):
            desc = (
                (r.description[:87] + "...") if len(r.description) > 90
                else r.description
            )

            meta: list[str] = [f"{_fmt_downloads(r.downloads)} downloads"]
            if r.followers:
                meta.append(f"{_fmt_downloads(r.followers)} followers")
            if r.updated:
                meta.append(f"updated {r.updated}")

            side_info = ""
            if r.client_side != "unknown" or r.server_side != "unknown":
                side_info = (
                    f"  client:{_fmt_side(r.client_side)}"
                    f"  server:{_fmt_side(r.server_side)}"
                )

            tags = ", ".join(r.categories[:4]) if r.categories else ""

            print(SEP)
            # Line 1: index, title (truncated), type badge
            title_col = r.title[:43]
            print(f"  {i:<4}{title_col:<44}[{r.project_type.value}]")
            # Line 2: slug, canonical ID, author
            author_str = f"  by {r.author}" if r.author else ""
            print(f"       {r.slug}  ·  {r.project_id}{author_str}")
            # Line 3: description
            if desc:
                print(f"       {desc}")
            # Line 4: stats + side info
            stat_line = "  ·  ".join(meta)
            if r.license_id:
                stat_line += f"  ·  {r.license_id}"
            print(f"       {stat_line}{side_info}")
            # Line 5: categories
            if tags:
                print(f"       tags: {tags}")
            # Line 6: URL
            print(f"       {r.url}")

        print(SEP)
        if total_hits > end:
            next_offset = end
            print(
                f"\n  {total_hits - end:,} more results - "
                f"use --offset {next_offset} to see next page\n"
            )
        else:
            print()


# Download service


class DownloadService:
    """Prepares DownloadTasks and executes file downloads."""

    def __init__(
        self,
        api:               ModrinthAPI,
        http_client:       StandardHTTPClient,
        repository:        CSVProjectRepository,
        version_matcher:   VersionMatcher,
        modpack_extractor: ModpackExtractor,
        config:            AppConfig,
        logger:            logging.Logger,
    ) -> None:
        self._api               = api
        self._http              = http_client
        self._repo              = repository
        self._version_matcher   = version_matcher
        self._modpack_extractor = modpack_extractor
        self._config            = config
        self._logger            = logger

    # task preparation

    def prepare_download_task(
        self,
        project_id:         str,
        mc_version:         str,
        loader:             str,
        output_dir:         Path,
        project_info_cache: dict[str, Any] | None = None,
        prefetched_version: dict[str, Any] | None = None,
        only_type:          str | None = None,
        update:             bool = False,
    ) -> tuple[DownloadTask | None, list[str]]:
        """
        Build a DownloadTask for project_id.

        Returns (task, required_dep_ids) where task is None when the project
        should be skipped (type mismatch, already up-to-date, or no compatible
        version found).

        required_dep_ids lists project IDs that are required dependencies
        of the resolved version (dependency_type: required).

        Parameters
        ----------
        project_info_cache:
            Pre-fetched project metadata from get_projects_batch.
        prefetched_version:
            Pre-resolved version data (e.g. from bulk hash update check).
            When supplied, skips get_versions to save an API call.
        """
        try:
            # Prefer batch-pre-fetched project info.
            project_info: dict[str, Any] = (
                project_info_cache
                if project_info_cache is not None
                else self._api.get_project(project_id)
            )

            # Use canonical ID from the API, not the user-supplied slug.
            # Slugs can change; canonical IDs are permanent.
            canonical_id = project_info.get("id") or project_id
            title        = project_info.get("title", canonical_id)

            try:
                project_type = ProjectType(project_info.get("project_type", "mod"))
            except ValueError:
                project_type = ProjectType.MISC

            # type filter
            if only_type:
                if only_type == "plugin":
                    # "plugin" = mod + plugin loader (not an API project_type)
                    if (
                        project_type != ProjectType.MOD
                        or loader.lower() not in PLUGIN_LOADERS
                    ):
                        self._logger.debug(
                            f"Skip '{title}': only_type=plugin but "
                            f"type={project_type.value}, loader={loader}"
                        )
                        return None, []
                elif only_type != project_type.value:
                    self._logger.debug(
                        f"Skip '{title}': type={project_type.value}, "
                        f"wanted={only_type}"
                    )
                    return None, []

            # version resolution
            if prefetched_version is not None:
                best = prefetched_version
                self._logger.debug(f"Using pre-fetched version for '{title}'")
            else:
                versions = self._api.get_versions(
                    canonical_id, loader, mc_version, project_type
                )
                best = self._version_matcher.find_best_version(
                    versions, mc_version, loader, project_type,
                    canonical_id, self._config.ignore_version_ids,
                    stable_only=self._config.stable_only,
                )

            if best is None:
                self._logger.warning(
                    f"No compatible version for '{title}' "
                    f"(MC {mc_version} + {loader})"
                )
                return None, []

            # required dependencies
            required_dep_ids: list[str] = [
                dep["project_id"]
                for dep in best.get("dependencies", [])
                if (
                    isinstance(dep, dict)
                    and dep.get("dependency_type") == "required"
                    and dep.get("project_id")
                )
            ]
            if required_dep_ids:
                self._logger.debug(
                    f"'{title}' has {len(required_dep_ids)} required "
                    f"dep(s): {required_dep_ids}"
                )

            # primary file
            files = best.get("files", [])
            # Per API spec: if no file has primary=true, first file is primary.
            primary_file: dict[str, Any] | None = next(
                (f for f in files if isinstance(f, dict) and f.get("primary")),
                files[0] if files and isinstance(files[0], dict) else None,
            )

            if (
                not primary_file
                or "filename" not in primary_file
                or "url" not in primary_file
            ):
                self._logger.warning(
                    f"No usable file attachment for '{title}'"
                )
                return None, []

            # output folder
            folder = (
                "plugins"
                if loader.lower() in PLUGIN_LOADERS
                and project_type == ProjectType.MOD
                else project_type.get_folder_name()
            )
            destination  = output_dir / folder / primary_file["filename"]
            old_path_str = self._repo.get(canonical_id)
            old_path     = Path(old_path_str) if old_path_str else None

            # Already tracked and not in update mode -> skip.
            if old_path_str and not update:
                self._logger.debug(f"Skip '{title}': already tracked")
                return None, required_dep_ids

            # Same filename and file exists -> already up-to-date.
            if (
                old_path
                and old_path.name == primary_file["filename"]
                and destination.exists()
            ):
                self._logger.debug(f"Skip '{title}': already up-to-date")
                return None, required_dep_ids

            return DownloadTask(
                project_id       = canonical_id,
                project_type     = project_type,
                file_info        = FileInfo(
                    filename = primary_file["filename"],
                    url      = primary_file["url"],
                    size     = primary_file.get("size", 0),
                    hashes   = primary_file.get("hashes", {}),
                ),
                destination_path = destination,
                old_path         = old_path,
            ), required_dep_ids

        except (APIError, NetworkError, ValueError) as exc:
            self._logger.error(f"Prepare failed for '{project_id}': {exc}")
            return None, []

    # task execution

    def execute_download(self, task: DownloadTask, base_dir: Path) -> bool:
        """
        Download the file described by task, verify its hash, remove the
        superseded file, and stage the new registry entry.
        """
        size_str = (
            f" ({_fmt_size(task.file_info.size)})"
            if task.file_info.size else ""
        )
        self._logger.info(
            f"Downloading  [{task.project_type.value}]  "
            f"{task.file_info.filename}{size_str}"
        )

        try:
            if not self._http.download_file(
                task.file_info.url,
                task.destination_path,
                expected_hashes=task.file_info.hashes or None,
            ):
                return False

            # Remove superseded file.
            if (
                task.old_path
                and task.old_path.exists()
                and task.old_path != task.destination_path
            ):
                try:
                    task.old_path.unlink()
                    self._logger.debug(f"Removed old file: {task.old_path.name}")
                except OSError as exc:
                    self._logger.warning(f"Could not remove old file: {exc}")

            # Stage new registry entry (written to disk on flush()).
            try:
                rel = str(
                    task.destination_path.relative_to(base_dir)
                ).replace("\\", "/")
            except ValueError:
                rel = str(task.destination_path)
            self._repo.save(task.project_id, rel)

            # Auto-extract .mrpack modpacks.
            if (
                task.project_type == ProjectType.MODPACK
                and task.destination_path.suffix == ".mrpack"
            ):
                try:
                    self._modpack_extractor.extract(
                        task.destination_path,
                        task.destination_path.parent,
                    )
                except DownloadError as exc:
                    self._logger.warning(f"Modpack extraction failed: {exc}")

            return True

        except (DownloadError, OSError) as exc:
            self._logger.error(
                f"Download failed for '{task.file_info.filename}': {exc}"
            )
            return False


# Logging setup


def setup_logging(directory: Path, level: int = logging.INFO) -> logging.Logger:
    """
    Configure two log handlers:

    Console: compact HH:MM:SS  LEVEL  message format at the given level.
    File: verbose with function name and line number, always at DEBUG.
    """
    logger = logging.getLogger("modrinth_dl")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(console)

    try:
        fh = logging.FileHandler(directory / LOG_FILE, "w", "utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  "
                "%(funcName)s:%(lineno)d  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(fh)
    except (IOError, OSError) as exc:
        logger.warning(f"Could not create log file: {exc}")

    return logger


# Network check


def check_network(logger: logging.Logger, timeout: int = 3) -> bool:
    """Return True if a TCP connection to api.modrinth.com:443 succeeds."""
    logger.debug("Checking connectivity to api.modrinth.com:443...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("api.modrinth.com", 443))
        sock.close()
        logger.debug("Network check: OK")
        return True
    except (socket.error, OSError) as exc:
        logger.debug(f"Network check: FAILED - {exc}")
        return False


# Application orchestrator


class Application:
    """
    Top-level orchestrator.

    Download pipeline (non-update):
    1. Batch-fetch all project metadata (1 API call for up to 100 projects).
    2. Submit all prepare_download_task jobs to prepare_pool.
    3. As each prepare completes, submit its task to download_pool immediately.
    4. Collect required-dependency IDs; run a single extra prepare pass.
    5. Flush registry once at the end.

    Download pipeline (--update):
    1. Compute SHA-1 of every tracked file on disk.
    2. POST /v2/version_files/update with all hashes to get latest versions
       in 1 request instead of N GET /v2/project/{id}/version calls.
    3. Skip files already at the latest version.
    4. Use normal prepare flow for projects needing update or not yet tracked.
    5. Same dependency and flush steps as above.
    """

    def __init__(
        self,
        config:     AppConfig,
        logger:     logging.Logger,
        output_dir: Path,
    ) -> None:
        self._config     = config
        self._logger     = logger
        self._output_dir = output_dir

        http = StandardHTTPClient(config, logger)
        repo = CSVProjectRepository(output_dir, logger)
        api  = ModrinthAPI(http, logger)
        vm   = VersionMatcher()
        mpx  = ModpackExtractor(http, config, logger)

        self._repository = repo
        self._api        = api
        self._http       = http

        self._search_service   = SearchService(api, logger)
        self._download_service = DownloadService(
            api, http, repo, vm, mpx, config, logger,
        )

    # public helpers

    def get_collection_ids(self, collection_id: str) -> list[str]:
        """Fetch project IDs from a Modrinth collection."""
        return self._api.get_collection(collection_id)

    # search

    def search(
        self,
        query:        str,
        loader:       str | None = None,
        mc_version:   str | None = None,
        project_type: str | None = None,
        limit:        int = 10,
        offset:       int = 0,
        sort:         str = "relevance",
    ) -> int:
        self._logger.info(
            f"Search: {query!r}  limit={limit}  offset={offset}  sort={sort}"
        )
        try:
            filters: dict[str, str] = {}
            if loader:       filters["loader"]       = loader
            if mc_version:   filters["version"]      = mc_version
            if project_type: filters["project_type"] = project_type

            results, total = self._search_service.search(
                query, filters, limit, offset, sort
            )
            self._search_service.display_results(
                results, total, query, filters, offset
            )
            self._logger.info(
                f"Search complete - {len(results)} shown of {total} total"
            )
            return 0

        except APIError as exc:
            self._logger.error(f"Search failed: {exc}")
            return 1
        except Exception as exc:
            self._logger.error(
                f"Unexpected error during search: {exc}", exc_info=True
            )
            return 1

    # update helpers

    def _collect_hashes_for_tracked(
        self, project_ids: list[str]
    ) -> dict[str, str]:
        """
        For each project_id that has a tracked file on disk, compute its SHA-1 hash.

        Returns {sha1_hex: canonical_project_id}.
        """
        hash_to_pid: dict[str, str] = {}

        for pid in project_ids:
            rel = self._repository.get(pid)
            if not rel:
                continue
            path = self._output_dir / rel
            if not path.exists():
                continue
            try:
                h = hashlib.sha1()
                with open(path, "rb") as fh:
                    while chunk := fh.read(DOWNLOAD_CHUNK_SIZE):
                        h.update(chunk)
                hash_to_pid[h.hexdigest()] = pid
            except OSError as exc:
                self._logger.debug(f"Cannot hash '{path.name}': {exc}")

        self._logger.debug(
            f"Hashed {len(hash_to_pid)} tracked file(s) for bulk update check"
        )
        return hash_to_pid

    def _run_bulk_update_check(
        self,
        project_ids: list[str],
        mc_version:  str,
        loader:      str,
    ) -> tuple[dict[str, Any], set[str]]:
        """
        Use POST /v2/version_files/update to bulk-check for newer versions.

        Returns:
        - prefetched: {canonical_id: latest_version_data} for projects with a newer version.
        - skip_ids: canonical IDs already at the latest version (no update needed).
        """
        hash_to_pid = self._collect_hashes_for_tracked(project_ids)
        if not hash_to_pid:
            return {}, set()

        try:
            latest_map = self._api.get_latest_versions_from_hashes(
                list(hash_to_pid.keys()), "sha1", [loader], [mc_version]
            )
        except (APIError, NetworkError) as exc:
            self._logger.warning(
                f"Bulk hash update check failed: {exc} - "
                "falling back to per-project version fetch"
            )
            return {}, set()

        prefetched: dict[str, Any] = {}
        skip_ids:   set[str]       = set()

        for file_hash, version_data in latest_map.items():
            pid = hash_to_pid.get(file_hash)
            if not pid or not isinstance(version_data, dict):
                continue

            # Determine the latest primary file name.
            files = version_data.get("files", [])
            latest_primary = next(
                (f for f in files if isinstance(f, dict) and f.get("primary")),
                files[0] if files and isinstance(files[0], dict) else None,
            )
            if not latest_primary:
                continue

            # Find the current file name on disk.
            rel          = self._repository.get(pid)
            current_name = Path(rel).name if rel else ""

            if latest_primary.get("filename") == current_name:
                self._logger.debug(
                    f"'{current_name}' already up-to-date (bulk check)"
                )
                skip_ids.add(pid)
            else:
                self._logger.debug(
                    f"Update available: {current_name} -> "
                    f"{latest_primary.get('filename')}"
                )
                prefetched[pid] = version_data

        # Projects not in hash_to_pid (no tracked file) are handled normally.
        self._logger.info(
            f"Bulk update check: {len(skip_ids)} up-to-date, "
            f"{len(prefetched)} to update, "
            f"{len(project_ids) - len(hash_to_pid)} not tracked"
        )
        return prefetched, skip_ids

    # dependency resolution

    def _resolve_dependencies(
        self,
        dep_ids:    set[str],
        processed:  set[str],
        mc_version: str,
        loader:     str,
        only_type:  str | None,
    ) -> list[DownloadResult]:
        """
        One-level required-dependency resolution.

        Fetches and downloads required deps not already being processed.
        Transitive deps (deps-of-deps) are logged but not recursed into,
        to prevent infinite chains.
        """
        new_ids = dep_ids - processed
        if not new_ids:
            return []

        self._logger.info(
            f"Resolving {len(new_ids)} required dependency(ies): "
            f"{', '.join(sorted(new_ids))}"
        )

        dep_batch        = self._api.get_projects_batch(list(new_ids))
        results:         list[DownloadResult] = []
        transitive_deps: set[str]             = set()

        with (
            ThreadPoolExecutor(max_workers=self._config.max_workers) as pp,
            ThreadPoolExecutor(max_workers=self._config.max_workers) as dp,
        ):
            prepare_futures = {
                pp.submit(
                    self._download_service.prepare_download_task,
                    dep_id,
                    mc_version,
                    loader,
                    self._output_dir,
                    dep_batch.get(dep_id),
                    None,
                    only_type,
                    False,  # deps are always fresh downloads
                ): dep_id
                for dep_id in new_ids
            }

            download_futures: dict[Any, DownloadTask] = {}
            for future in as_completed(prepare_futures):
                dep_id = prepare_futures[future]
                try:
                    task, sub_dep_ids = future.result()
                except Exception as exc:
                    self._logger.error(
                        f"Prepare error for dep '{dep_id}': {exc}"
                    )
                    continue
                transitive_deps.update(sub_dep_ids)
                if task is not None:
                    df = dp.submit(
                        self._download_service.execute_download,
                        task, self._output_dir,
                    )
                    download_futures[df] = task

            for future in as_completed(download_futures):
                task = download_futures[future]
                try:
                    ok = future.result()
                except Exception as exc:
                    self._logger.error(
                        f"Dep download error '{task.file_info.filename}': {exc}"
                    )
                    ok = False
                results.append(DownloadResult(task=task, success=ok))

        if transitive_deps - processed - new_ids:
            self._logger.warning(
                "Some transitive dependencies were not resolved "
                "(only 1 level of required deps is auto-fetched): "
                + ", ".join(sorted(transitive_deps - processed - new_ids))
            )

        return results

    # download

    def download(
        self,
        project_ids: list[str],
        mc_version:  str,
        loader:      str,
        only_type:   str | None = None,
        update:      bool = False,
    ) -> int:
        self._logger.info(
            f"Download session: {len(project_ids)} project(s) - "
            f"MC {mc_version}  loader={loader}"
        )

        try:
            # Phase 1: batch project metadata (1 API call).
            self._logger.info("Fetching project metadata...")
            batch_info = self._api.get_projects_batch(project_ids)
            self._logger.debug(
                f"Batch resolved {len(batch_info)} entries "
                f"for {len(project_ids)} IDs"
            )

            # Phase 2: update mode - bulk hash check.
            prefetched_versions: dict[str, Any] = {}
            skip_ids:            set[str]       = set()

            if update:
                self._logger.info(
                    "Checking for updates via bulk hash endpoint..."
                )
                prefetched_versions, skip_ids = self._run_bulk_update_check(
                    project_ids, mc_version, loader
                )

            # Phase 3: pipeline - prepare -> download.
            dl_results:   list[DownloadResult] = []
            all_dep_ids:  set[str]             = set()
            processed_ids = set(project_ids)

            with (
                ThreadPoolExecutor(
                    max_workers=self._config.max_workers
                ) as prepare_pool,
                ThreadPoolExecutor(
                    max_workers=self._config.max_workers
                ) as download_pool,
            ):
                prepare_futures = {
                    prepare_pool.submit(
                        self._download_service.prepare_download_task,
                        pid,
                        mc_version,
                        loader,
                        self._output_dir,
                        batch_info.get(pid),
                        prefetched_versions.get(pid),
                        only_type,
                        update,
                    ): pid
                    for pid in project_ids
                    if pid not in skip_ids
                }

                download_futures: dict[Any, DownloadTask] = {}
                for future in as_completed(prepare_futures):
                    pid = prepare_futures[future]
                    try:
                        task, dep_ids = future.result()
                    except Exception as exc:
                        self._logger.error(f"Prepare error for '{pid}': {exc}")
                        continue
                    all_dep_ids.update(dep_ids)
                    if task is not None:
                        df = download_pool.submit(
                            self._download_service.execute_download,
                            task, self._output_dir,
                        )
                        download_futures[df] = task

                for future in as_completed(download_futures):
                    task = download_futures[future]
                    try:
                        ok = future.result()
                    except Exception as exc:
                        self._logger.error(
                            f"Uncaught error downloading "
                            f"'{task.file_info.filename}': {exc}"
                        )
                        ok = False
                    dl_results.append(DownloadResult(task=task, success=ok))

            # Phase 4: resolve required dependencies (one level).
            if all_dep_ids:
                dep_results = self._resolve_dependencies(
                    all_dep_ids, processed_ids, mc_version, loader, only_type
                )
                dl_results.extend(dep_results)

            # Phase 5: persist registry.
            self._repository.flush()

            # Phase 6: summary.
            total_skipped = len(skip_ids) + (
                len(project_ids) - len(prepare_futures) - len(skip_ids)
            )
            self._print_summary(dl_results, len(project_ids), total_skipped)

            failed = sum(1 for r in dl_results if not r.success)
            return 1 if failed else 0

        except Exception as exc:
            self._logger.error(f"Download session failed: {exc}", exc_info=True)
            return 1

    def _print_summary(
        self,
        results:         list[DownloadResult],
        total_requested: int,
        extra_skipped:   int = 0,
    ) -> None:
        ok      = [r for r in results if r.success]
        failed  = [r for r in results if not r.success]
        skipped = total_requested - len(results) + extra_skipped

        self._logger.info(
            f"Session complete - "
            f"{len(ok)} downloaded, "
            f"{len(failed)} failed, "
            f"{skipped} skipped"
        )
        for r in ok:
            self._logger.info(f"  ✓  {r.task.file_info.filename}")
        for r in failed:
            self._logger.warning(f"  ✗  {r.task.file_info.filename}")
        if skipped:
            self._logger.debug(
                f"  {skipped} project(s) already up-to-date or filtered"
            )


# CLI


def create_parser() -> argparse.ArgumentParser:
    _valid_types = list(TYPE_FOLDERS.keys()) + ["misc", "plugin"]

    parser = argparse.ArgumentParser(
        prog="modrinth_dl",
        description=(
            "Modrinth Downloader - download mods, plugins, resource packs, "
            "shaders, and more. Requires Python 3.10+."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
examples:
  %(prog)s -s "sodium" -l fabric -v 1.20.1
  %(prog)s -s "optimization" -l fabric -v 1.20.1 -t mod --limit 20 --sort downloads
  %(prog)s -s "shaders" -l iris -v 1.20.1 --offset 10
  %(prog)s -c COLLECTION_ID -l fabric -v 1.20.1
  %(prog)s -p "sodium,lithium,phosphor" -l fabric -v 1.20.1
  %(prog)s -c COLLECTION_ID -l fabric -v 1.20.1 --update
  %(prog)s -p "sodium" -l fabric -v 1.20.1 -d ./minecraft --stable-only
        """,
    )

    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    parser.add_argument(
        "-s", "--search", metavar="QUERY",
        help="search for projects (shows results, no download)",
    )

    g = parser.add_argument_group("download sources")
    g.add_argument(
        "-c", "--collection", metavar="ID",
        help="collection ID to download",
    )
    g.add_argument(
        "-p", "--projects", metavar="IDS",
        help="comma-separated project IDs or slugs",
    )

    g = parser.add_argument_group("required options")
    g.add_argument(
        "-l", "--loader", required=True, metavar="LOADER",
        help="mod loader (fabric / forge / quilt / paper / spigot / neoforge / ...)",
    )
    g.add_argument(
        "-v", "--mcversion", dest="mcversion", required=True,
        metavar="VERSION",
        help="Minecraft version, e.g. 1.20.1",
    )

    g = parser.add_argument_group("output options")
    g.add_argument(
        "-d", "--directory", type=Path, default=Path.cwd(),
        metavar="DIR",
        help="base download directory (default: current directory)",
    )
    g.add_argument(
        "-ot", "--only-type",
        choices=_valid_types,
        metavar="TYPE",
        help=f"restrict downloads to one type: {', '.join(_valid_types)}",
    )

    g = parser.add_argument_group("search filters & display")
    g.add_argument(
        "-t", "--type", dest="project_type",
        choices=_valid_types,
        metavar="TYPE",
        help=f"filter search by project type: {', '.join(_valid_types)}",
    )
    g.add_argument(
        "--limit", type=int, default=10, metavar="N",
        help="max search results to display (default: 10)",
    )
    g.add_argument(
        "--offset", type=int, default=0, metavar="N",
        help="search result offset for pagination (default: 0)",
    )
    g.add_argument(
        "--sort",
        choices=VALID_SORT_INDICES,
        default="relevance",
        metavar="KEY",
        help=(
            f"sort search results by: "
            f"{', '.join(VALID_SORT_INDICES)} (default: relevance)"
        ),
    )

    g = parser.add_argument_group("download options")
    g.add_argument(
        "-u", "--update", action="store_true",
        help="check for and download newer compatible versions",
    )
    g.add_argument(
        "--stable-only", action="store_true",
        help="only download release-type versions (skip beta and alpha)",
    )
    g.add_argument(
        "-i", "--ignore-version-ids", metavar="IDS",
        help="comma-separated project IDs that skip strict version checks",
    )

    g = parser.add_argument_group("network options")
    g.add_argument(
        "-w", "--workers", type=int, default=DEFAULT_MAX_WORKERS,
        metavar="N",
        help=f"parallel workers (default: {DEFAULT_MAX_WORKERS})",
    )
    g.add_argument(
        "-m", "--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
        metavar="N",
        help=f"retry attempts per request (default: {DEFAULT_MAX_RETRIES})",
    )
    g.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    g.add_argument(
        "-ua", "--user-agent", default=DEFAULT_USER_AGENT,
        help="override the User-Agent header",
    )

    g = parser.add_argument_group("debug options")
    g.add_argument(
        "--verbose", action="store_true",
        help="print DEBUG-level log messages to console",
    )

    return parser


def validate_arguments(
    args:   argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    if not args.search and not args.collection and not args.projects:
        parser.error(
            "Provide --search, or at least one of --collection / --projects"
        )

    if args.search and (args.collection or args.projects):
        parser.error(
            "--search cannot be combined with --collection or --projects"
        )

    if not (1 <= args.workers <= 32):
        parser.error("--workers must be between 1 and 32")

    if args.max_retries < 1:
        parser.error("--max-retries must be >= 1")

    if args.timeout < 1:
        parser.error("--timeout must be >= 1")

    if args.offset < 0:
        parser.error("--offset must be >= 0")

    if args.limit < 1:
        parser.error("--limit must be >= 1")

    if not args.search:
        try:
            args.directory.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as exc:
            parser.error(
                f"Cannot access or create directory '{args.directory}': {exc}"
            )


def main() -> int:
    parser = create_parser()
    args   = parser.parse_args()

    validate_arguments(args, parser)

    # Resolve working directory: <base>/<mcversion>-<loader>/
    args.directory = (
        args.directory / f"{args.mcversion}-{args.loader}"
    ).resolve()
    try:
        args.directory.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as exc:
        print(f"error: cannot create output directory: {exc}", file=sys.stderr)
        return 1

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logger    = setup_logging(args.directory, log_level)
    logger.info(
        f"Modrinth Downloader v{__version__}  "
        f"(Python {sys.version_info.major}.{sys.version_info.minor})"
    )

    try:
        if not check_network(logger):
            logger.error("No internet connection - aborting")
            return 1

        config = AppConfig.from_args(args)
        app    = Application(config, logger, args.directory)

        # search mode
        if args.search:
            return app.search(
                query        = args.search,
                loader       = args.loader,
                mc_version   = args.mcversion,
                project_type = args.project_type,
                limit        = args.limit,
                offset       = args.offset,
                sort         = args.sort,
            )

        # download mode: resolve project IDs
        if args.collection:
            logger.info(f"Fetching collection: {args.collection}")
            try:
                project_ids = app.get_collection_ids(args.collection)
            except (APIError, NetworkError) as exc:
                logger.error(f"Failed to fetch collection: {exc}")
                return 1
        else:
            project_ids = [
                p.strip() for p in args.projects.split(",") if p.strip()
            ]

        if not project_ids:
            logger.error("No project IDs to process - exiting")
            return 1

        logger.info(f"Projects to process: {len(project_ids)}")

        return app.download(
            project_ids = project_ids,
            mc_version  = args.mcversion,
            loader      = args.loader,
            only_type   = args.only_type,
            update      = args.update,
        )

    except ConfigurationError as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130

    except ModrinthError as exc:
        logger.error(f"Modrinth error: {exc}")
        return 1

    except Exception as exc:
        logger.error(f"Unexpected fatal error: {exc}")
        if args.verbose:
            logger.debug(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
