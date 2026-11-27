"""Read Shaw data from its public website in a dedicated Chromium context."""

import re
from contextlib import suppress
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import Error, Response, sync_playwright

from .common import SourceError, check_response

BASE_URL = "https://shaw.sg"
DATA_ROUTES = (
    "/internal/api/v2/performance-selectors",
    "/internal/api/v2/performances/",
    "/internal/get_date_selectors",
    "/internal/get_layouts",
    "/internal/get_movie_release",
    "/internal/get_show_times",
    "/internal/get_visual_status/",
)


def _request_key(url: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    parts = urlsplit(url)
    return parts.path, tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))


class ShawBrowser:
    def __init__(self, executable: Path | None, *, endpoint: str | None = None):
        self._executable = executable
        self._endpoint = endpoint
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._active_page = None
        self._responses: dict[tuple[str, tuple[tuple[str, str], ...]], Response] = {}

    def begin_poll(self) -> None:
        self._active_page = None
        self._responses.clear()

    def close(self) -> None:
        if self._context is not None:
            with suppress(Error):
                self._context.close()
        if self._browser is not None:
            with suppress(Error):
                self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None
        self.begin_poll()

    def _capture(self, response: Response) -> None:
        key = _request_key(response.url)
        if any(key[0].startswith(path) for path in DATA_ROUTES):
            self._responses[key] = response

    def _open(self) -> None:
        self._playwright = sync_playwright().start()
        chromium = self._playwright.chromium
        if self._endpoint:
            self._browser = chromium.connect(self._endpoint, timeout=10000)
        else:
            self._browser = chromium.launch(executable_path=self._executable, headless=True)
        self._context = self._browser.new_context(locale="en-SG", service_workers="block")

        def read_only(route):
            request = route.request
            if request.method == "GET" and urlsplit(request.url).netloc == "shaw.sg":
                route.continue_()
            else:
                route.abort()

        self._context.route("**/*", read_only)
        self._page = self._context.new_page()
        self._page.on("response", self._capture)

    def _navigate(self, path: str) -> str:
        self._responses.clear()
        response = self._page.goto(BASE_URL + path, wait_until="domcontentloaded", timeout=15000)
        body = response.text()
        check_response(response.status, response.headers.get("cf-mitigated", ""), body)
        self._active_page = path
        return body

    def _choose_date(self, selector_date: str) -> None:
        day = date.fromisoformat(selector_date)
        if (self._active_page or "").startswith("/movie-details/"):
            label = re.compile(rf"^0?{day.day}\s+{day:%b}\s+{day:%a}$", re.IGNORECASE)
            self._page.locator('#showtimes [data-role="carousel-date-item"]').filter(
                has_text=label
            ).click(timeout=8000)
            return

        label = re.compile(rf"^{day:%a}, 0?{day.day} {day:%b %Y}$")
        option = self._page.locator("option", has_text=label)
        selection = self._page.locator("select", has=option).first
        value = selection.locator("option", has_text=label).get_attribute("value", timeout=8000)
        if value is None:
            raise SourceError(f"Shaw did not expose a date option for {selector_date}")
        selection.select_option(value=value, timeout=8000)

    def _data_response(self, path: str, referer: str) -> Response:
        key = _request_key(path)
        if self._active_page == referer and key in self._responses:
            return self._responses[key]

        def matches(response: Response) -> bool:
            return _request_key(response.url) == key

        if key[0] == "/internal/get_show_times":
            if self._active_page != referer:
                self._navigate(referer)
            if key in self._responses:
                return self._responses[key]
            with self._page.expect_response(matches, timeout=12000) as pending:
                self._choose_date(dict(key[1])["date"])
            response = pending.value
        elif self._active_page != referer:
            with self._page.expect_response(matches, timeout=12000) as pending:
                self._navigate(referer)
            response = pending.value
        else:
            response = self._page.wait_for_event("response", predicate=matches, timeout=12000)
        return response

    def get(self, path: str, referer: str = "/") -> str:
        try:
            if self._page is None:
                self._open()
            if not path.startswith("/internal/"):
                return self._navigate(path)
            response = self._data_response(path, referer)
            body = response.text()
        except (Error, SourceError) as error:
            self.close()
            if isinstance(error, SourceError):
                raise
            reason = str(error).splitlines()[0]
            raise SourceError(f"Shaw page did not return {path} ({reason})") from None
        check_response(response.status, response.headers.get("cf-mitigated", ""), body)
        return body
