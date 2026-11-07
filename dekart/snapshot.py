import math
import platform
import re
import sys
import time
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit


def snapshot_token_from_render_url(render_url):
    """Extract snapshot_token query param from snapshot render URL."""
    try:
        parsed = urlparse(str(render_url or "").strip())
    except Exception:
        return ""
    if not parsed.query:
        return ""
    query = parse_qs(parsed.query, keep_blank_values=False)
    token_values = query.get("snapshot_token", [])
    if not token_values:
        return ""
    return str(token_values[0]).strip()


SNAPSHOT_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SNAPSHOT_QUERY_VALUE_PATTERN = re.compile(
    r"([?&][A-Za-z0-9_.~%+\-]+)=([^&\s\"'<>]+)",
    re.IGNORECASE,
)
SNAPSHOT_SECRET_FIELD_PATTERN = re.compile(
    r"([\"']?(?:snapshot_token|access_token|authorization|token|signature|sig|"
    r"x-goog-signature|x-amz-signature)[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}&]+)",
    re.IGNORECASE,
)
RETRYABLE_SNAPSHOT_ERROR_PHRASES = (
    "target page, context or browser has been closed",
    "page crashed",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def redacted_snapshot_netloc(parsed):
    """Return a URL authority with any userinfo removed."""
    if "@" not in parsed.netloc:
        return parsed.netloc
    return "[REDACTED]@" + parsed.netloc.rsplit("@", 1)[1]


def sanitize_snapshot_diagnostic(value):
    """Redact URL query values and common secret fields from snapshot stderr."""
    text = str(value or "")

    def redact_url(match):
        raw_url = match.group(0)
        trailing = ""
        while raw_url and raw_url[-1] in ".,;)]}":
            trailing = raw_url[-1] + trailing
            raw_url = raw_url[:-1]
        try:
            parsed = urlsplit(raw_url)
            query_keys = []
            for item in parsed.query.split("&"):
                key = item.split("=", 1)[0].strip()
                if key:
                    query_keys.append(key)
            redacted_query = "&".join(f"{key}=[REDACTED]" for key in query_keys)
            redacted_url = urlunsplit(
                (
                    parsed.scheme,
                    redacted_snapshot_netloc(parsed),
                    parsed.path,
                    redacted_query,
                    "",
                )
            )
            return redacted_url + trailing
        except Exception:
            return "[REDACTED_URL]" + trailing

    text = SNAPSHOT_URL_PATTERN.sub(redact_url, text)
    text = SNAPSHOT_QUERY_VALUE_PATTERN.sub(r"\1=[REDACTED]", text)
    text = SNAPSHOT_SECRET_FIELD_PATTERN.sub(r"\1[REDACTED]", text)
    return text


def snapshot_url_debug_summary(value):
    """Return a snapshot URL summary containing query names but no values."""
    try:
        parsed = urlsplit(str(value or "").strip())
        query_keys = [item.split("=", 1)[0] for item in parsed.query.split("&") if item]
        base = urlunsplit(
            (parsed.scheme, redacted_snapshot_netloc(parsed), parsed.path, "", "")
        )
        if query_keys:
            return f"{base} query_keys={','.join(query_keys)}"
        return base
    except Exception:
        return "[invalid snapshot URL]"


def print_snapshot_stderr(message):
    """Print a sanitized snapshot diagnostic."""
    print(sanitize_snapshot_diagnostic(message), file=sys.stderr)


def snapshot_expiry_deadline(expires_in, response_received_at):
    """Return a verified monotonic deadline for a positive integer lifetime."""
    if isinstance(expires_in, bool):
        return None
    seconds = None
    if isinstance(expires_in, int):
        seconds = expires_in
    elif isinstance(expires_in, float):
        if math.isfinite(expires_in) and expires_in.is_integer():
            seconds = int(expires_in)
    elif isinstance(expires_in, str):
        normalized = expires_in.strip()
        if re.fullmatch(r"\+?\d+", normalized):
            try:
                seconds = int(normalized)
            except (ValueError, OverflowError):
                return None
    if seconds is None or seconds <= 0:
        return None
    try:
        deadline = response_received_at + seconds
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(deadline):
        return None
    return deadline


def is_png_snapshot(value):
    """Return whether snapshot bytes contain the complete PNG signature."""
    return isinstance(value, (bytes, bytearray)) and value.startswith(PNG_SIGNATURE)


def normalized_snapshot_error_message(exc):
    """Normalize a Playwright error for narrow retry phrase matching."""
    return " ".join(str(exc or "").lower().split())


class LocalSnapshotRenderError(RuntimeError):
    """Classified local snapshot failure with pre-cleanup evidence."""

    def __init__(
        self,
        cause,
        stage,
        retryable=False,
        timed_out=False,
        page_crashed=False,
        page_closed=False,
        browser_disconnected=False,
        browser_version="",
        elapsed_seconds=0.0,
        cleanup_errors=None,
    ):
        super().__init__(str(cause))
        self.cause = cause
        self.stage = stage
        self.retryable = bool(retryable)
        self.timed_out = bool(timed_out)
        self.page_crashed = bool(page_crashed)
        self.page_closed = bool(page_closed)
        self.browser_disconnected = bool(browser_disconnected)
        self.browser_version = str(browser_version or "")
        self.elapsed_seconds = float(elapsed_seconds or 0.0)
        self.cleanup_errors = list(cleanup_errors or [])
        self.attempts = 1
        self.retry_skipped_reason = ""


def _safe_snapshot_state(callable_value, default=False):
    """Read Playwright connection state without replacing the primary error."""
    try:
        return bool(callable_value())
    except Exception:
        return bool(default)


def _render_local_snapshot_attempt(
    render_url,
    width,
    height,
    timeout_seconds,
    sync_playwright_factory,
    playwright_timeout_error,
    monotonic=None,
):
    """Perform one local render attempt with evidence capture and cleanup."""
    clock = monotonic or time.monotonic
    started_at = clock()
    manager = None
    playwright = None
    browser = None
    context = None
    page = None
    png_bytes = None
    primary_error = None
    stage = "launch"
    browser_version = ""
    page_crashed = False
    browser_disconnected = False
    cleanup_started = False
    elapsed_seconds = 0.0
    cleanup_errors = []

    def observe_page_crash(*_args):
        nonlocal page_crashed
        if not cleanup_started:
            page_crashed = True

    def observe_browser_disconnect(*_args):
        nonlocal browser_disconnected
        if not cleanup_started:
            browser_disconnected = True

    try:
        manager = sync_playwright_factory()
        playwright = manager.start()
        browser = playwright.chromium.launch(headless=True)
        try:
            browser_version = str(browser.version or "")
        except Exception:
            browser_version = ""
        browser.on("disconnected", observe_browser_disconnect)
        context = browser.new_context(
            viewport={"width": width, "height": height},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.on("crash", observe_page_crash)

        stage = "navigate"
        page.goto(render_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        snapshot_token = snapshot_token_from_render_url(render_url)
        stage = "ready"
        if snapshot_token:
            page.wait_for_function(
                "(expectedToken) => window.__dekartSnapshotReadyToken === expectedToken",
                arg=snapshot_token,
                timeout=timeout_seconds * 1000,
            )
        else:
            page.wait_for_timeout(1500)
            for selector in ("canvas", ".kepler-gl", "img"):
                try:
                    page.wait_for_selector(selector, timeout=5000)
                    break
                except Exception:
                    continue

        stage = "screenshot"
        png_bytes = page.screenshot(type="png", timeout=timeout_seconds * 1000)
        elapsed_seconds = clock() - started_at
    except Exception as exc:
        primary_error = exc
        elapsed_seconds = clock() - started_at

    page_closed = _safe_snapshot_state(page.is_closed) if page is not None else False
    if browser is not None:
        browser_disconnected = browser_disconnected or not _safe_snapshot_state(
            browser.is_connected,
            default=True,
        )

    cleanup_started = True
    for label, resource in (
        ("context", context),
        ("browser", browser),
        ("playwright", playwright),
    ):
        if resource is None:
            continue
        try:
            if label == "playwright":
                resource.stop()
            else:
                resource.close()
        except Exception as exc:
            cleanup_errors.append(f"{label}: {exc}")

    if primary_error is not None:
        timed_out = isinstance(primary_error, playwright_timeout_error)
        normalized_message = normalized_snapshot_error_message(primary_error)
        message_matches = any(
            phrase in normalized_message for phrase in RETRYABLE_SNAPSHOT_ERROR_PHRASES
        )
        retryable = (
            not timed_out
            and browser is not None
            and (
                page_crashed
                or page_closed
                or browser_disconnected
                or message_matches
            )
        )
        classified = LocalSnapshotRenderError(
            primary_error,
            stage=stage,
            retryable=retryable,
            timed_out=timed_out,
            page_crashed=page_crashed,
            page_closed=page_closed,
            browser_disconnected=browser_disconnected,
            browser_version=browser_version,
            elapsed_seconds=elapsed_seconds,
            cleanup_errors=cleanup_errors,
        )
        raise classified from primary_error

    return png_bytes, {
        "stage": stage,
        "browser_version": browser_version,
        "elapsed_seconds": elapsed_seconds,
        "cleanup_errors": cleanup_errors,
    }


def _print_local_snapshot_debug(error, attempt):
    """Print sanitized failure evidence for one local attempt."""
    print_snapshot_stderr(
        "[debug] local_snapshot_attempt={0} stage={1} elapsed_seconds={2:.3f} "
        "exception_type={3} page_crashed={4} page_closed={5} "
        "browser_disconnected={6} browser_version={7} platform={8}".format(
            attempt,
            error.stage,
            error.elapsed_seconds,
            type(error.cause).__name__,
            str(error.page_crashed).lower(),
            str(error.page_closed).lower(),
            str(error.browser_disconnected).lower(),
            error.browser_version or "unknown",
            platform.platform(),
        )
    )
    print_snapshot_stderr(f"[debug] local_snapshot_error={error}")
    for cleanup_error in error.cleanup_errors:
        print_snapshot_stderr(f"[debug] local_snapshot_cleanup_error={cleanup_error}")


def render_local_snapshot_png(
    render_url,
    width,
    height,
    timeout_seconds,
    expires_at=None,
    debug=False,
    monotonic=None,
):
    """Render locally and retry once after verified target termination."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        raise RuntimeError("Playwright is not installed. Run `dekart snapshot-local install`.") from exc

    clock = monotonic or time.monotonic
    for attempt in (1, 2):
        if debug:
            print_snapshot_stderr(f"[debug] local_snapshot_attempt={attempt} starting")
        try:
            png_bytes, metadata = _render_local_snapshot_attempt(
                render_url,
                width,
                height,
                timeout_seconds,
                sync_playwright_factory=sync_playwright,
                playwright_timeout_error=PlaywrightTimeoutError,
                monotonic=clock,
            )
            if debug:
                print_snapshot_stderr(
                    "[debug] local_snapshot_attempt={0} completed "
                    "elapsed_seconds={1:.3f}".format(
                        attempt,
                        metadata.get("elapsed_seconds", 0.0),
                    )
                )
                for cleanup_error in metadata.get("cleanup_errors", []):
                    print_snapshot_stderr(
                        f"[debug] local_snapshot_cleanup_error={cleanup_error}"
                    )
            return png_bytes
        except LocalSnapshotRenderError as exc:
            exc.attempts = attempt
            if debug:
                _print_local_snapshot_debug(exc, attempt)
            if attempt == 1 and exc.retryable:
                if expires_at is None:
                    exc.retry_skipped_reason = "unverified_lifetime"
                    raise
                if clock() >= expires_at:
                    exc.retry_skipped_reason = "expired"
                    raise
                print_snapshot_stderr(
                    "Local snapshot target closed; retrying once with a fresh browser."
                )
                continue
            raise

    raise RuntimeError("Local snapshot retry loop ended unexpectedly.")


