"""Downloads the printer's current G-code once per print and caches the built model."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import gzip
import hashlib
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

import httpx

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.httpx_client import get_async_client

from . import gcode
from .const import (
    CACHE_KEEP,
    CONF_PRINTER,
    DOMAIN,
    DOWNLOAD_TIMEOUT,
    JOB_TIMEOUT,
    MAX_FILE_SIZE,
    MODEL_URL,
    PRUSALINK_SENSORS,
    RETRY_AFTER,
)

_LOGGER = logging.getLogger(__name__)

KEY = re.compile(r"^[0-9a-f]{16}$")
SUFFIX = ".pgv.gz"


class PrinterError(Exception):
    """The printer could not be asked for its job or file."""


@dataclass
class Job:
    key: str
    name: str
    download: str
    size: int | None


@dataclass
class Build:
    job: Job
    task: asyncio.Task[None] | None = None
    received: int = 0
    size: int | None = None
    stage: str = "downloading"
    error: str | None = None
    failed_at: float = 0.0


class ModelCache:
    """One per viewer config entry: answers the card's status calls and builds models."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.printer_entry_id: str = entry.data[CONF_PRINTER]
        self.directory = Path(hass.config.cache_path(DOMAIN, entry.entry_id))
        self._builds: dict[str, Build] = {}

    # --- card API ----------------------------------------------------------------

    async def async_status(self) -> dict[str, Any]:
        """What the card should show now; starts a build for a new print."""
        status: dict[str, Any] = {"entities": self.entities()}
        try:
            job = await self._async_current_job()
        except PrinterError as err:
            _LOGGER.debug("Printer unreachable: %s", err)
            return status | await self._latest(live=False, message=str(err))
        if job is None:
            return status | await self._latest(live=False)
        if await self.hass.async_add_executor_job(self.path(job.key).exists):
            return status | self._ready(job.key, job.name, live=True)

        build = self._builds.get(job.key)
        if build and build.error and time.monotonic() - build.failed_at < RETRY_AFTER:
            return status | {"state": "error", "name": job.name, "message": build.error}
        if not build or build.error:
            build = self._builds[job.key] = Build(job, size=job.size)
            build.task = self.entry.async_create_background_task(
                self.hass, self._async_build(build), f"{DOMAIN} build {job.key}"
            )
        return status | {
            "state": "building",
            "name": job.name,
            "stage": build.stage,
            "received": build.received,
            "size": build.size,
        }

    def path(self, key: str) -> Path:
        return self.directory / f"{key}{SUFFIX}"

    def entities(self) -> dict[str, str]:
        """The PrusaLink sensors of this printer, for the card to follow."""
        found = {}
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_config_entry(registry, self.printer_entry_id):
            for name, suffix in PRUSALINK_SENSORS.items():
                if entry.unique_id == f"{self.printer_entry_id}_{suffix}":
                    found[name] = entry.entity_id
        return found

    def _ready(self, key: str, name: str, live: bool) -> dict[str, Any]:
        return {
            "state": "ready",
            "key": key,
            "name": name,
            "live": live,
            "url": MODEL_URL.format(entry_id=self.entry.entry_id, key=key),
        }

    async def _latest(self, live: bool, message: str | None = None) -> dict[str, Any]:
        """The most recently built model, shown while nothing is printing."""
        newest = await self.hass.async_add_executor_job(self._newest_cached)
        if newest is None:
            return {"state": "idle", "message": message}
        key, name = newest
        return self._ready(key, name, live) | ({"message": message} if message else {})

    def _newest_cached(self) -> tuple[str, str] | None:
        if not self.directory.is_dir():
            return None
        files = sorted(self.directory.glob(f"*{SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            key = path.name.removesuffix(SUFFIX)
            if KEY.match(key):
                name_file = path.with_suffix(".name")
                name = name_file.read_text() if name_file.exists() else ""
                return key, name
        return None

    # --- printer ---------------------------------------------------------------

    def _printer(self) -> tuple[str, httpx.DigestAuth]:
        printer = self.hass.config_entries.async_get_entry(self.printer_entry_id)
        if printer is None:
            raise PrinterError("The PrusaLink printer is no longer set up")
        data = printer.data
        if CONF_USERNAME not in data:
            raise PrinterError("The PrusaLink entry has no username/password")
        host = data[CONF_HOST].rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host, httpx.DigestAuth(data[CONF_USERNAME], data[CONF_PASSWORD])

    async def _async_current_job(self) -> Job | None:
        host, auth = self._printer()
        try:
            response = await get_async_client(self.hass).get(f"{host}/api/v1/job", auth=auth, timeout=JOB_TIMEOUT)
        except httpx.HTTPError as err:
            raise PrinterError(f"Can't reach the printer: {err}") from err
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise PrinterError(f"Printer answered {response.status_code} for the current job")
        job = response.json()
        file = job.get("file") or {}
        download = (file.get("refs") or {}).get("download")
        if not download:
            return None
        name = file.get("display_name") or file.get("name") or "print"
        # Size and timestamp change when the printer finishes a transfer, so a print is
        # identified by its job id and file.
        identity = f"{job.get('id')}|{file.get('path')}/{file.get('name')}|{name}"
        key = hashlib.sha1(identity.encode()).hexdigest()[:16]
        return Job(key=key, name=name, download=download, size=file.get("size") or None)

    async def _async_download(self, build: Build) -> bytes:
        host, auth = self._printer()
        url = build.job.download if build.job.download.startswith("http") else f"{host}{build.job.download}"
        try:
            return await self._async_fetch(build, url, auth)
        except FileNotFoundError:
            pass
        # A file sent from Prusa Connect stays a transfer directory, with the data in
        # its "p" file, until the printer tidies it up (not while printing it).
        try:
            return await self._async_fetch(build, f"{url}/p", auth)
        except FileNotFoundError:
            raise PrinterError("Printer answered 404 for the file") from None

    async def _async_fetch(self, build: Build, url: str, auth: httpx.DigestAuth) -> bytes:
        chunks = []
        client = get_async_client(self.hass)
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                async with client.stream("GET", url, auth=auth, timeout=JOB_TIMEOUT) as response:
                    if response.status_code == 404:
                        raise FileNotFoundError(url)
                    if response.status_code != 200:
                        raise PrinterError(f"Printer answered {response.status_code} for the file")
                    if length := response.headers.get("Content-Length"):
                        build.size = int(length)
                    async for chunk in response.aiter_bytes():
                        build.received += len(chunk)
                        if build.received > MAX_FILE_SIZE:
                            raise PrinterError("The G-code file is too large")
                        chunks.append(chunk)
        except (httpx.HTTPError, TimeoutError) as err:
            raise PrinterError(f"Downloading the G-code failed: {err!r}") from err
        return b"".join(chunks)

    # --- building --------------------------------------------------------------

    async def _async_build(self, build: Build) -> None:
        job = build.job
        try:
            data = await self._async_download(build)
            build.stage = "building"
            started = time.monotonic()
            model = await self.hass.async_add_executor_job(gcode.build_model, data, job.name)
            await self.hass.async_add_executor_job(self._store, job, model)
            _LOGGER.debug(
                "Built %s (%d bytes of G-code) in %.1fs", job.name, len(data), time.monotonic() - started
            )
            del self._builds[job.key]
        except (PrinterError, gcode.GCodeError) as err:
            build.error, build.failed_at = str(err), time.monotonic()
            _LOGGER.warning("Can't build a model of %s: %s", job.name, err)
        except Exception:
            build.error, build.failed_at = "Unexpected error, see the log", time.monotonic()
            _LOGGER.exception("Building a model of %s failed", job.name)

    def _store(self, job: Job, model: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(job.key)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(gzip.compress(model, 6))
        target.with_suffix(".name").write_text(job.name)
        os.replace(tmp, target)
        files = sorted(self.directory.glob(f"*{SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[CACHE_KEEP:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".name").unlink(missing_ok=True)

    def read(self, key: str) -> bytes | None:
        """The gzipped model, or None (run in an executor)."""
        if not KEY.match(key):
            return None
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None
