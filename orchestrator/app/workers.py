"""What the agent worker pods report about themselves.

LiveKit has no API for listing registered workers, so they are found through a
headless Service per agent name (DNS only; no Kubernetes API access), and each
pod is asked over its own HTTP port. Pods that are not Ready or are being
removed are not in DNS, so a draining worker is not listed.
"""

import asyncio
import logging
import socket
import time
from typing import Any

import aiohttp

from .config import Settings

logger = logging.getLogger("orchestrator.workers")

REQUEST_TIMEOUT_SECONDS = 1.0
# listings are reused for this long, so that describing many sessions at once
# asks each pod once
CACHE_SECONDS = 1.0


class WorkerDirectory:
    def __init__(self, settings: Settings):
        self._services = dict(settings.worker_services)
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._http: aiohttp.ClientSession | None = None

    def knows(self, agent: str) -> bool:
        return agent in self._services

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.close()

    async def _addresses(self, host: str, port: int) -> list[str]:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            return []  # the Service has no ready pods
        return sorted({info[4][0] for info in infos})

    async def _worker(self, agent: str, address: str, port: int) -> dict[str, Any] | None:
        if self._http is None:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            )
        try:
            async with self._http.get(f"http://{address}:{port}/worker") as res:
                info = await res.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            logger.warning(
                "worker did not answer",
                extra={"agent": agent, "address": address, "error": str(err) or repr(err)},
            )
            return None
        load = float(info.get("worker_load", 0.0))
        return {
            "agent": info.get("agent_name") or agent,
            "address": address,
            # zero values are left out of the worker's JSON
            "jobs": int(info.get("active_jobs", 0)),
            "load": round(load, 3),
            # our workers report jobs / MAX_JOBS and refuse jobs at 1.0
            "full": load >= 1.0,
        }

    async def for_agent(self, agent: str) -> list[dict[str, Any]]:
        """Ready workers for one agent name; empty if it has no Service configured."""
        if agent not in self._services:
            return []
        cached = self._cache.get(agent)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        host, _, port = self._services[agent].partition(":")
        addresses = await self._addresses(host, int(port or 8081))
        found = await asyncio.gather(
            *(self._worker(agent, address, int(port or 8081)) for address in addresses)
        )
        workers = [w for w in found if w is not None]
        self._cache[agent] = (time.monotonic(), workers)
        return workers

    async def everything(self) -> list[dict[str, Any]]:
        listings = await asyncio.gather(*(self.for_agent(agent) for agent in self._services))
        return [worker for listing in listings for worker in listing]
