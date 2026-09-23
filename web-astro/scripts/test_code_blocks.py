"""Browser regressions for the built Astro reader (no paid/external services).

After `npm run build` and `python -m playwright install chromium webkit`:
    python scripts/test_code_blocks.py
    BROWSER=webkit python scripts/test_code_blocks.py

The suite serves dist on an ephemeral loopback port. ASTRO_BASE selects a
prefixed build; CHROMIUM_EXECUTABLE_PATH optionally selects a local Chromium.
"""

from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import os
import unittest

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class CodeBlocksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dist = Path(__file__).resolve().parents[1] / "dist"
        base = os.environ.get("ASTRO_BASE", "/").strip("/")
        if base:
            # A Pages artifact places dist under the configured URL prefix.
            # Strip that prefix when mapping HTTP requests to the build root.
            class Handler(QuietHandler):
                def translate_path(self, path):
                    prefix = f"/{base}/"
                    if path.startswith(prefix):
                        path = "/" + path[len(prefix):]
                    return super().translate_path(path)
        else:
            Handler = QuietHandler
        if not (dist / "book/chapter1/index.html").is_file():
            raise RuntimeError("Build the Astro site with npm run build first.")
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(Handler, directory=str(dist))
        )
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.server.shutdown)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        if base:
            cls.url += base + "/"
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser_name = os.environ.get("BROWSER", "chromium")
        if cls.browser_name not in ("chromium", "webkit"):
            raise ValueError("BROWSER must be chromium or webkit")
        options = {}
        if cls.browser_name == "chromium" and os.environ.get("CHROMIUM_EXECUTABLE_PATH"):
            options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE_PATH"]
        cls.browser = getattr(cls.playwright, cls.browser_name).launch(**options)
        cls.addClassCleanup(cls.browser.close)

    @contextmanager
    def reader(self, width=393, *, theme="light", javascript=True, scale=1):
        context = self.browser.new_context(
            viewport={"width": width, "height": 900},
            is_mobile=width < 768,
            has_touch=width < 768,
            color_scheme=theme,
            java_script_enabled=javascript,
        )
        try:
            context.add_init_script(f"localStorage.setItem('book-text-size', '{scale}');")
            context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: { writeText: async text => { window.__copiedCode = text; } }
            });""")
            page = context.new_page()
            page.goto(self.url + "book/chapter1/", wait_until="load")
            pre = page.locator("#chapter-content pre").filter(has_text="get_weather").filter(
                has_text="tool_call_id"
            )
            expect(pre).to_have_count(1)
            if javascript:
                expect(pre.locator("..").locator(".code-toolbar")).to_be_visible()
            page.evaluate("document.fonts.ready")
            page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            yield page, pre
        finally:
            context.close()

    def assert_frame(self, pre):
        bounds = pre.evaluate("""pre => {
            const rect = node => {
                const {left, right} = node.getBoundingClientRect();
                return {left, right};
            };
            return {pre: rect(pre), frame: rect(pre.parentElement),
                toolbar: rect(pre.parentElement.querySelector('.code-toolbar'))};
        }""")
        for edge in ("left", "right"):
            for other in ("frame", "toolbar"):
                self.assertAlmostEqual(
                    bounds["pre"][edge], bounds[other][edge], delta=1,
                    msg=f"Code body and {other} must share the {edge} edge: {bounds}",
                )

    def assert_no_page_overflow(self, page):
        self.assertTrue(page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        ), "Only the code viewport, not the whole page, should scroll sideways")

    def assert_can_scroll(self, pre):
        metrics = pre.evaluate("""pre => {
            pre.scrollLeft = pre.scrollWidth;
            return {left: pre.scrollLeft, max: pre.scrollWidth - pre.clientWidth,
                overflow: getComputedStyle(pre).overflowX};
        }""")
        self.assertIn(metrics["overflow"], ("auto", "scroll"))
        self.assertGreater(metrics["max"], 20, "The two-column example must remain wider than a phone")
        self.assertAlmostEqual(metrics["left"], metrics["max"], delta=1)
        pre.evaluate("pre => { pre.scrollLeft = 0; }")
        self.assertEqual(pre.evaluate("pre => pre.scrollLeft"), 0)

    def test_toolbar_and_body_share_edges_at_all_breakpoints(self):
        for width in (320, 375, 393, 430, 760, 768, 1000, 1280, 1600):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme), self.reader(width, theme=theme) as (page, pre):
                    self.assert_frame(pre)
                    self.assert_no_page_overflow(page)

    def test_columns_are_preserved_and_scroll_to_both_ends_by_default(self):
        for width in (320, 393, 430):
            with self.subTest(width=width), self.reader(width) as (page, pre):
                wrap = pre.locator("..").locator("button[aria-pressed]")
                expect(wrap).to_have_attribute("aria-pressed", "false")
                expect(pre).to_have_css("white-space", "pre")
                expect(pre.locator("code")).to_have_css("white-space", "pre")
                self.assert_can_scroll(pre)
                self.assert_frame(pre)
                self.assert_no_page_overflow(page)

    def test_wrapping_is_reversible_and_copy_keeps_the_complete_source(self):
        with self.reader() as (page, pre):
            original = pre.text_content()
            for label in ("第一步", "第二步", "第三步", "第四步"):
                self.assertIn(label, original)
            toolbar = pre.locator("..").locator(".code-toolbar")
            wrap = toolbar.locator("button[aria-pressed]")
            copy = toolbar.locator("button:not([aria-pressed])")
            for wrapped in (True, False):
                wrap.click()
                expect(wrap).to_have_attribute("aria-pressed", str(wrapped).lower())
                expect(pre).to_have_css("white-space", "pre-wrap" if wrapped else "pre")
                if wrapped:
                    self.assertTrue(pre.evaluate("pre => pre.scrollWidth <= pre.clientWidth + 1"))
                else:
                    self.assert_can_scroll(pre)
                    pre.evaluate("pre => { pre.scrollLeft = pre.scrollWidth; }")
                self.assert_frame(pre)
                page.evaluate("window.__copiedCode = null")
                copy.click()
                page.wait_for_function("text => window.__copiedCode === text", arg=original)
                self.assertEqual(pre.text_content(), original)
                self.assert_no_page_overflow(page)

    def test_larger_reading_text_still_scrolls_without_clipping(self):
        with self.reader(scale=1.3) as (page, pre):
            self.assert_frame(pre)
            self.assert_can_scroll(pre)
            self.assert_no_page_overflow(page)

    def test_without_javascript_code_is_still_readable_and_scrollable(self):
        with self.reader(javascript=False) as (page, pre):
            expect(page.locator(".code-toolbar")).to_have_count(0)
            expect(pre).to_have_css("white-space", "pre")
            self.assert_can_scroll(pre)
            self.assert_no_page_overflow(page)

    def test_native_touch_swipe_moves_code_not_the_toolbar(self):
        if self.browser_name != "chromium":
            self.skipTest("Native touch injection uses Chromium CDP; WebKit runs the layout/scroll tests")
        with self.reader() as (page, pre):
            pre.evaluate("pre => pre.scrollIntoView({block: 'center', behavior: 'instant'})")
            rect = pre.bounding_box()
            y = max(150, rect["y"] + 60)
            start_x = rect["x"] + rect["width"] - 30
            end_x = rect["x"] + 30
            session = page.context.new_cdp_session(page)
            try:
                session.send("Input.dispatchTouchEvent", {
                    "type": "touchStart", "touchPoints": [{"x": start_x, "y": y}],
                })
                for step in range(1, 9):
                    session.send("Input.dispatchTouchEvent", {
                        "type": "touchMove", "touchPoints": [{
                            "x": start_x + (end_x - start_x) * step / 8, "y": y,
                        }],
                    })
                    page.wait_for_timeout(20)
                session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                expect(pre).not_to_have_js_property("scrollLeft", 0)
                self.assertGreater(pre.evaluate("pre => pre.scrollLeft"), 0)
                self.assert_frame(pre)
                self.assert_no_page_overflow(page)
            finally:
                session.detach()


if __name__ == "__main__":
    unittest.main(verbosity=2)
