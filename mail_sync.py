"""Parallel per-account IMAP workers backed by the local cache."""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from mail_cache import CacheSettings, CacheStore
from mail_html import HTML_POLICY_VERSION


_PREFETCH_DETAIL_PRIORITY = 20
_PREFETCH_IMAGE_PRIORITY = 21


@dataclass(order=True)
class _Job:
    priority: int
    sequence: int
    key: str = field(compare=False)
    kind: str = field(compare=False)
    unread_only: bool = field(default=False, compare=False)
    limit: int = field(default=30, compare=False)
    uid: str = field(default="", compare=False)
    prefer_html: bool = field(default=False, compare=False)
    image_resource: object = field(default=None, compare=False)
    event: threading.Event = field(default_factory=threading.Event, compare=False)
    result: object = field(default=None, compare=False)
    error: Exception | None = field(default=None, compare=False)


class _AccountWorker:
    def __init__(
        self,
        account,
        cache: CacheStore,
        client_factory: Callable,
        cache_changed: Callable[[str], None],
    ) -> None:
        self.account = account
        self.cache = cache
        self.client_factory = client_factory
        self.cache_changed = cache_changed
        self.jobs: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._pending: dict[str, _Job] = {}
        self._pending_lock = threading.Lock()
        self._sequence = itertools.count()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"mail-sync-{account.name}",
            daemon=True,
        )
        self._client = None
        self._thread.start()

    def submit(
        self,
        kind: str,
        *,
        priority: int,
        unread_only: bool = False,
        limit: int = 30,
        uid: str = "",
        prefer_html: bool = False,
        image_resource: object = None,
    ) -> _Job:
        resource_id = ""
        if isinstance(image_resource, dict):
            resource_id = str(image_resource.get("id", ""))
        key = f"{kind}:{int(unread_only)}:{limit}:{uid}:{int(prefer_html)}:{resource_id}"
        with self._pending_lock:
            existing = self._pending.get(key)
            if existing is not None:
                return existing
            job = _Job(
                priority=priority,
                sequence=next(self._sequence),
                key=key,
                kind=kind,
                unread_only=unread_only,
                limit=limit,
                uid=uid,
                prefer_html=prefer_html,
                image_resource=image_resource,
            )
            self._pending[key] = job
            self.jobs.put(job)
            return job

    def stop(self) -> None:
        job = self.request_stop()
        self.wait_stopped(job)

    def request_stop(self) -> _Job | None:
        if not self._thread.is_alive():
            return None
        self._stop_event.set()
        return self.submit("stop", priority=-100)

    def wait_stopped(self, job: _Job | None) -> None:
        if job is None:
            return
        job.event.wait(35)
        self._thread.join(timeout=35)

    def _run(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job.kind == "stop":
                    self._close_client()
                    return
                self.cache.mark_sync_attempt(self.account.name)
                for attempt in range(3):
                    try:
                        client = self._connected_client()
                        if job.kind == "seed":
                            job.result = self._seed(client, job.unread_only, job.limit)
                        elif job.kind in {"sync", "sync_urgent"}:
                            job.result = self._sync(client)
                        elif job.kind in {"detail", "detail_prefetch"}:
                            job.result = self._detail(
                                client, job.uid, prefer_html=job.prefer_html
                            )
                        elif job.kind in {"image", "image_prefetch"}:
                            job.result = client.fetch_inline_image(
                                job.uid, job.image_resource
                            )
                        else:
                            raise RuntimeError(f"unknown sync job: {job.kind}")
                        if job.kind in {"seed", "sync", "sync_urgent"}:
                            self.cache_changed(self.account.name)
                        break
                    except Exception:
                        self._close_client()
                        if attempt == 2:
                            raise
                        if self._stop_event_wait(0.25 * (2**attempt)):
                            raise RuntimeError("同步已停止。")
            except Exception as exc:
                job.error = exc
                if job.kind in {"seed", "sync", "sync_urgent"}:
                    self.cache.mark_sync_error(self.account.name, str(exc))
                self._close_client()
            finally:
                with self._pending_lock:
                    self._pending.pop(job.key, None)
                job.event.set()
                self.jobs.task_done()

    def _stop_event_wait(self, seconds: float) -> bool:
        return self._stop_event.wait(seconds)

    def _connected_client(self):
        if self._client is not None:
            try:
                self._client.noop()
                return self._client
            except Exception:
                self._close_client()
        self._client = self.client_factory(self.account)
        self._client.connect()
        return self._client

    def _close_client(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        finally:
            self._client = None

    def _seed(self, client, unread_only: bool, limit: int) -> int:
        uids = client.priority_uids(unread_only=unread_only, limit=limit)
        if self._stop_event.is_set():
            return 0
        summaries = client.fetch_summaries(uids, unread_uids=set(uids) if unread_only else None)
        self.cache.upsert_messages(self.account.name, summaries)
        self.cache.mark_seeded(self.account.name, unread_only)
        state = self.cache.sync_state(self.account.name)
        highest = max((int(uid) for uid in uids), default=state.highest_uid)
        self.cache.mark_sync_success(
            self.account.name,
            uidvalidity=client.uidvalidity(),
            highest_uid=max(state.highest_uid, highest),
            full_sync_complete=state.full_sync_complete,
        )
        return len(summaries)

    def _sync(self, client) -> int:
        if self._stop_event.is_set():
            return 0
        uidvalidity = client.uidvalidity()
        state = self.cache.sync_state(self.account.name)
        if state.uidvalidity and uidvalidity and state.uidvalidity != uidvalidity:
            self.cache.clear_account(self.account.name)
        all_uids = client.search_uids(unread_only=False)
        unread_uids = client.search_uids(unread_only=True)
        cached = self.cache.account_uids(self.account.name)
        missing = [uid for uid in all_uids if uid not in cached]
        for start in range(0, len(missing), 200):
            if self._stop_event.is_set():
                return 0
            batch = missing[start : start + 200]
            summaries = client.fetch_summaries(batch, unread_uids=set(unread_uids))
            self.cache.upsert_messages(self.account.name, summaries)
        if self._stop_event.is_set():
            return 0
        self.cache.reconcile_account(self.account.name, all_uids, unread_uids)
        self.cache.mark_seeded(self.account.name, True)
        self.cache.mark_seeded(self.account.name, False)
        highest = max((int(uid) for uid in all_uids), default=0)
        self.cache.mark_sync_success(
            self.account.name,
            uidvalidity=uidvalidity,
            highest_uid=highest,
            full_sync_complete=True,
        )
        return len(missing)

    def _detail(self, client, uid: str, *, prefer_html: bool = False):
        if self._stop_event.is_set():
            raise RuntimeError("读取邮件正文已停止。")
        if self.cache.message(self.account.name, uid) is None:
            summaries = client.fetch_summaries([uid])
            self.cache.upsert_messages(self.account.name, summaries)
        detail = client.get_message(uid, prefer_html=prefer_html)
        body_format = str(getattr(detail, "body_format", "plain"))
        text = str(detail.text)
        safe_html = str(getattr(detail, "safe_html", ""))
        html_policy = str(getattr(detail, "html_policy", ""))
        raw_blocked_images = getattr(detail, "blocked_images", 0)
        blocked_images = (
            raw_blocked_images
            if type(raw_blocked_images) is int and raw_blocked_images >= 0
            else 0
        )
        cacheable = bool(
            getattr(detail, "cacheable", body_format in {"plain", "html"})
        )
        self.cache.store_detail(
            self.account.name,
            uid,
            recipients=detail.recipients,
            text=text,
            attachments=detail.attachments,
            body_format=body_format,
            safe_html=safe_html,
            blocked_images=blocked_images,
            html_policy=html_policy,
            image_resources=getattr(detail, "image_resources", ()),
            cacheable=cacheable,
        )
        return detail


class SyncManager:
    """Coordinates account workers without sharing IMAP connections across threads."""

    def __init__(
        self,
        accounts: Iterable,
        cache: CacheStore,
        settings: CacheSettings,
        client_factory: Callable,
        *,
        periodic: bool,
        prefetch_image: Callable[[str, str, object, float], None] | None = None,
    ) -> None:
        self.accounts = tuple(accounts)
        self.cache = cache
        self.settings = settings
        self.prefetch_image = prefetch_image
        self._stop_event = threading.Event()
        self._stopped = False
        self._prefetch_lock = threading.RLock()
        self._prefetch_active = False
        self._prefetch_events = {
            account.name: threading.Event() for account in self.accounts
        }
        self._prefetch_processed = {
            account.name: set() for account in self.accounts
        }
        self._prefetch_failed = {
            account.name: set() for account in self.accounts
        }
        self._prefetch_threads: dict[str, threading.Thread] = {}
        self.workers = {
            account.name: _AccountWorker(
                account,
                cache,
                client_factory,
                self._wake_prefetch,
            )
            for account in self.accounts
        }
        self._scheduler: threading.Thread | None = None
        if periodic and settings.refresh_minutes > 0:
            self._scheduler = threading.Thread(
                target=self._schedule_loop,
                name="mail-sync-scheduler",
                daemon=True,
            )
            self._scheduler.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        for event in self._prefetch_events.values():
            event.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=5)
        stopping = [
            (worker, worker.request_stop()) for worker in self.workers.values()
        ]
        for worker, job in stopping:
            worker.wait_stopped(job)
        for thread in self._prefetch_threads.values():
            thread.join(timeout=20)

    def start_prefetch(self) -> None:
        """Prefetch every cached message newest-first without blocking the caller."""

        if self._stopped or not self.cache.body_cache_enabled:
            return
        with self._prefetch_lock:
            self._prefetch_active = True
            for account in self.accounts:
                failed = self._prefetch_failed[account.name]
                self._prefetch_processed[account.name].difference_update(failed)
                failed.clear()
                if account.name not in self._prefetch_threads:
                    thread = threading.Thread(
                        target=self._prefetch_account,
                        args=(account.name,),
                        name=f"mail-prefetch-{account.name}",
                        daemon=True,
                    )
                    self._prefetch_threads[account.name] = thread
                    thread.start()
                self._prefetch_events[account.name].set()

    def restart_prefetch(self) -> None:
        """Forget completed work after a cache clear and start again."""

        with self._prefetch_lock:
            for processed in self._prefetch_processed.values():
                processed.clear()
            for failed in self._prefetch_failed.values():
                failed.clear()
        self.start_prefetch()

    def _wake_prefetch(self, account_name: str) -> None:
        with self._prefetch_lock:
            active = self._prefetch_active
            event = self._prefetch_events.get(account_name)
            failed = self._prefetch_failed.get(account_name)
            if failed:
                self._prefetch_processed[account_name].difference_update(failed)
                failed.clear()
        if active and event is not None:
            event.set()

    def _prefetch_account(self, account_name: str) -> None:
        event = self._prefetch_events[account_name]
        while not self._stop_event.is_set():
            event.wait()
            event.clear()
            if self._stop_event.is_set():
                return
            while not self._stop_event.is_set():
                messages = self.cache.query_messages(
                    (account_name,), unread_only=False, limit=None
                )
                with self._prefetch_lock:
                    processed = self._prefetch_processed[account_name]
                    pending = [item for item in messages if item.uid not in processed]
                if not pending:
                    break
                restart_order = False
                for message in pending:
                    if self._stop_event.is_set():
                        return
                    succeeded = self._prefetch_message(
                        account_name, message.uid, message.received_at
                    )
                    with self._prefetch_lock:
                        self._prefetch_processed[account_name].add(message.uid)
                        if succeeded:
                            self._prefetch_failed[account_name].discard(message.uid)
                        else:
                            self._prefetch_failed[account_name].add(message.uid)
                    if event.is_set():
                        event.clear()
                        restart_order = True
                        break
                if not restart_order:
                    break

    def _prefetch_message(
        self, account_name: str, uid: str, received_at: float
    ) -> bool:
        try:
            detail = self.cache.cached_detail(account_name, uid)
            if not cached_web_body_is_current(detail):
                detail = self._fetch_prefetch_detail(account_name, uid)
        except Exception:
            return False
        if not cached_web_body_is_current(detail):
            return False
        if self.prefetch_image is None:
            return True
        succeeded = True
        for resource in getattr(detail, "image_resources", ()):
            if self._stop_event.is_set():
                return False
            try:
                self.prefetch_image(account_name, uid, resource, received_at)
            except Exception:
                succeeded = False
        return succeeded

    def _fetch_prefetch_detail(
        self, account_name: str, uid: str, timeout: float = 30.0
    ):
        worker = self.workers.get(account_name)
        if worker is None:
            raise RuntimeError(f"unknown account: {account_name}")
        job = worker.submit(
            "detail_prefetch",
            priority=_PREFETCH_DETAIL_PRIORITY,
            uid=uid,
            prefer_html=True,
        )
        self._wait_for_prefetch_job(job, timeout, "后台缓存邮件正文超时。")
        if job.error is not None:
            raise job.error
        return job.result

    def _wait_for_prefetch_job(
        self, job: _Job, timeout: float, timeout_message: str
    ) -> None:
        deadline = time.monotonic() + timeout
        while not job.event.is_set():
            if self._stop_event.is_set():
                raise RuntimeError("后台缓存已停止。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(timeout_message)
            job.event.wait(min(0.25, remaining))

    def ensure_seed(
        self,
        account_names: Iterable[str],
        *,
        unread_only: bool,
        limit: int,
        timeout: float = 8.0,
    ) -> tuple[dict[str, str], ...]:
        jobs: list[tuple[str, _Job]] = []
        for name in account_names:
            state = self.cache.sync_state(name)
            seeded = state.unread_seeded if unread_only else state.all_seeded
            if not seeded and name in self.workers:
                jobs.append(
                    (
                        name,
                        self.workers[name].submit(
                            "seed",
                            priority=0,
                            unread_only=unread_only,
                            limit=limit,
                        ),
                    )
                )
        errors = self._wait(jobs, timeout)
        for name in account_names:
            worker = self.workers.get(name)
            if worker is None:
                continue
            if unread_only:
                worker.submit("seed", priority=2, unread_only=False, limit=limit)
            worker.submit("sync", priority=5)
        return errors

    def sync_accounts(
        self,
        account_names: Iterable[str],
        *,
        wait: bool,
        timeout: float | None = None,
        force: bool = False,
    ) -> tuple[dict[str, str], ...]:
        jobs: list[tuple[str, _Job]] = []
        for name in account_names:
            worker = self.workers.get(name)
            if worker is None:
                continue
            if self.settings.refresh_minutes == 0 and not force:
                continue
            if not force and self.cache.account_is_fresh(name, self.settings.refresh_minutes):
                continue
            jobs.append(
                (
                    name,
                    worker.submit(
                        "sync_urgent" if force else "sync",
                        priority=-5 if force else 1,
                    ),
                )
            )
        if not wait:
            return ()
        return self._wait(jobs, timeout)

    def fetch_detail(
        self,
        account_name: str,
        uid: str,
        timeout: float = 30.0,
        *,
        prefer_html: bool = False,
    ):
        worker = self.workers.get(account_name)
        if worker is None:
            raise RuntimeError(f"unknown account: {account_name}")
        job = worker.submit(
            "detail", priority=-10, uid=uid, prefer_html=prefer_html
        )
        if not job.event.wait(timeout):
            raise TimeoutError("读取邮件正文超时。")
        if job.error is not None:
            raise job.error
        return job.result

    def fetch_image(
        self,
        account_name: str,
        uid: str,
        resource: object,
        timeout: float = 30.0,
        *,
        prefetch: bool = False,
    ):
        worker = self.workers.get(account_name)
        if worker is None:
            raise RuntimeError(f"unknown account: {account_name}")
        job = worker.submit(
            "image_prefetch" if prefetch else "image",
            priority=_PREFETCH_IMAGE_PRIORITY if prefetch else -9,
            uid=uid,
            image_resource=resource,
        )
        if prefetch:
            self._wait_for_prefetch_job(job, timeout, "读取邮件图片超时。")
        elif not job.event.wait(timeout):
            raise TimeoutError("读取邮件图片超时。")
        if job.error is not None:
            raise job.error
        return job.result

    def kick_background(
        self, limit: int = 30, *, incomplete_only: bool = False
    ) -> None:
        for account in self.accounts:
            state = self.cache.sync_state(account.name)
            if not state.unread_seeded:
                self.workers[account.name].submit(
                    "seed", priority=2, unread_only=True, limit=limit
                )
            if not state.all_seeded:
                self.workers[account.name].submit(
                    "seed", priority=3, unread_only=False, limit=limit
                )
            if not incomplete_only or not state.full_sync_complete:
                self.workers[account.name].submit("sync", priority=5)

    def _wait(
        self, jobs: Iterable[tuple[str, _Job]], timeout: float | None
    ) -> tuple[dict[str, str], ...]:
        deadline = time.monotonic() + timeout if timeout is not None else None
        errors: list[dict[str, str]] = []
        for name, job in jobs:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not job.event.wait(remaining):
                continue
            if job.error is not None:
                errors.append({"account": name, "error": str(job.error)})
        return tuple(errors)

    def _schedule_loop(self) -> None:
        interval = self.settings.refresh_minutes * 60
        while not self._stop_event.wait(interval):
            self.sync_accounts(
                (account.name for account in self.accounts),
                wait=False,
                force=True,
            )


def cached_web_body_is_current(detail: object | None) -> bool:
    if detail is None or getattr(detail, "html_policy", "") != HTML_POLICY_VERSION:
        return False
    body_format = getattr(detail, "body_format", "")
    return body_format == "plain" or (
        body_format == "html" and bool(getattr(detail, "safe_html", ""))
    )
