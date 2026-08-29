import asyncio
import logging
import os
import site
import subprocess
import sys
import time
import urllib.request
from typing import Optional, Dict, Any, List

# Ensure user site-packages are discovered across all execution environments
user_site = site.getusersitepackages()
if os.path.exists(user_site) and user_site not in sys.path:
    sys.path.insert(0, user_site)

appdata = os.environ.get("APPDATA")
if appdata:
    py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
    roaming_site = os.path.join(appdata, "Python", py_ver, "site-packages")
    if os.path.exists(roaming_site) and roaming_site not in sys.path:
        sys.path.insert(0, roaming_site)

logger = logging.getLogger("browser_automator")


class BrowserAutomator:
    """
    Playwright-based browser automator.
    Supports:
    1. Connecting directly to your existing, currently-opened Chrome browser via Chrome DevTools Protocol (CDP port 9222).
    2. Persistent browser context in ./browser_profile with system Chrome.
    """

    # Prioritized ChatGPT response container selectors
    CHATGPT_SELECTORS = [
        "div[data-message-author-role='assistant'] div.markdown",
        "article[data-testid^='conversation-turn'] div.markdown",
        "main article:last-of-type div.markdown",
        "div.agent-turn div.markdown",
        "div.markdown.prose",
        "div[data-message-author-role='assistant']",
        "article[data-testid^='conversation-turn']:last-of-type",
        "main article:last-of-type",
    ]

    # ChatGPT Copy button selectors for clipboard fallback
    CHATGPT_COPY_BUTTON_SELECTORS = [
        "button:has(use[href*='conversation-copy'])",
        "button:has(use[href='#lightweight-conversation-copy'])",
        "button:has(svg._wdUoQG_messageActionIcon)",
        "article[data-testid^='conversation-turn']:last-of-type button:has(use[href*='copy'])",
        "div[data-message-author-role='assistant']:last-of-type button:has(use[href*='copy'])",
        "article[data-testid^='conversation-turn']:last-of-type button[aria-label*='Copy']",
        "div[data-message-author-role='assistant']:last-of-type button[aria-label*='Copy']",
        "article:last-of-type button[data-testid*='copy']",
        "button[data-testid*='copy']",
        "button[aria-label*='Copy to clipboard']",
        "button[aria-label*='Copy response']",
        "button[aria-label*='Copy']",
    ]

    # ChatGPT Streaming / Stop generation button selectors
    CHATGPT_STOP_SELECTORS = [
        "button[data-testid='stop-button']",
        "button[aria-label*='Stop streaming']",
        "button[aria-label*='Stop generating']",
        "button[aria-label='Stop']",
        "button.stop-button",
    ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        browser_cfg = cfg.get("browser", {})
        services_cfg = cfg.get("services", {})

        self.user_data_dir = os.path.abspath(browser_cfg.get("user_data_dir", "./browser_profile"))
        self.headless = browser_cfg.get("headless", False)
        self.slow_mo = browser_cfg.get("slow_mo_ms", 50)
        self.timeout = browser_cfg.get("default_timeout_ms", 60000)
        self.stream_poll_interval = browser_cfg.get("stream_poll_interval_ms", 1000) / 1000.0
        self.stream_settle_timeout = browser_cfg.get("stream_settle_timeout_ms", 3000) / 1000.0

        # Existing Chrome CDP settings
        self.use_existing_chrome = browser_cfg.get("use_existing_chrome", True)
        self.cdp_url = browser_cfg.get("cdp_url", "http://127.0.0.1:9222").rstrip("/")

        self.chatgpt_url = services_cfg.get("chatgpt", {}).get("url", "https://chatgpt.com")
        self.gemini_url = services_cfg.get("gemini", {}).get("url", "https://gemini.google.com/app")
        self.notebooklm_url = services_cfg.get("notebooklm", {}).get("url", "https://notebooklm.google.com")
        self.grok_url = services_cfg.get("grok", {}).get("url", "https://grok.com/project/3a4c5217-9801-44e6-bb35-60f7e17eca21")

        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.chatgpt_page: Any = None
        self.gemini_page: Any = None
        self.notebooklm_page: Any = None
        self.grok_page: Any = None
        self.is_cdp_connected: bool = False


    @staticmethod
    def is_cdp_port_open(url: str = "http://127.0.0.1:9222") -> bool:
        """Check if Chrome DevTools Protocol port is active."""
        test_urls = [url]
        if "localhost" in url:
            test_urls.append(url.replace("localhost", "127.0.0.1"))
        elif "127.0.0.1" in url:
            test_urls.append(url.replace("127.0.0.1", "localhost"))

        for u in test_urls:
            try:
                endpoint = f"{u.rstrip('/')}/json/version"
                req = urllib.request.Request(endpoint, method="GET")
                with urllib.request.urlopen(req, timeout=1.5) as res:
                    if res.status == 200:
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    def launch_chrome_with_cdp(
        port: int = 9222,
        profile_dir: Optional[str] = None,
        urls: Optional[List[str]] = None,
    ) -> bool:
        """Attempt to launch system Chrome with remote debugging and a dedicated user data directory visible on desktop."""
        if BrowserAutomator.is_cdp_port_open(f"http://127.0.0.1:{port}"):
            logger.info(f"Chrome is already actively running on port {port}.")
            return True

        if profile_dir is None:
            profile_dir = os.path.abspath("./browser_profile")
        os.makedirs(profile_dir, exist_ok=True)

        target_urls = urls or [
            "https://chatgpt.com/c/6a918785-fca8-83ee-8bb6-422f629090d0",
            "https://notebook.google.com/notebook/b7a81a2b-5485-493a-bbfb-cbe58808dfc3",
        ]

        urls_formatted = " ".join([f'"{u}"' for u in target_urls])

        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in chrome_paths:
            if os.path.exists(path):
                logger.info(
                    f"Launching visible Chrome with remote debugging port {port} and profile '{profile_dir}'..."
                )
                if sys.platform == "win32":
                    cmd = f'start "" "{path}" --remote-debugging-port={port} --user-data-dir="{profile_dir}" --no-first-run --no-default-browser-check {urls_formatted}'
                    os.system(cmd)
                else:
                    args = [
                        path,
                        f"--remote-debugging-port={port}",
                        f"--user-data-dir={profile_dir}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        *target_urls,
                    ]
                    subprocess.Popen(args)
                time.sleep(3.0)
                return True
        return False




    async def start(self, override_headless: Optional[bool] = None, force_new_profile: bool = False):
        """
        Initialize Playwright.
        If Chrome is running on CDP (port 9222) and force_new_profile is False, connects directly to existing Chrome.
        Otherwise launches persistent context.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            found = False
            for p in sys.path:
                candidate = os.path.join(p, "playwright", "async_api")
                if os.path.exists(candidate):
                    from playwright.async_api import async_playwright
                    found = True
                    break
            if not found:
                raise ImportError(
                    f"Playwright is not found in Python path ({sys.executable}).\n"
                    f"Please run:\n"
                    f"python -m pip install playwright --trusted-host pypi.org --trusted-host files.pythonhosted.org\n"
                    f"python -m playwright install chromium"
                )

        self.playwright = await async_playwright().start()

        # Option 1: Connect to existing open Chrome via CDP (Port 9222)
        if not force_new_profile and self.use_existing_chrome:
            if not self.is_cdp_port_open(self.cdp_url):
                logger.info(f"Chrome is not yet running on {self.cdp_url}. Launching Chrome with CDP...")
                self.launch_chrome_with_cdp(9222)
                # Wait up to 10 seconds for Chrome CDP server to open
                for _ in range(10):
                    if self.is_cdp_port_open(self.cdp_url):
                        break
                    await asyncio.sleep(1.0)

            if self.is_cdp_port_open(self.cdp_url):
                try:
                    logger.info(f"Connecting to your Chrome browser at {self.cdp_url} via CDP...")
                    self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
                    if self.browser.contexts:
                        self.context = self.browser.contexts[0]
                    else:
                        self.context = await self.browser.new_context()

                    self.is_cdp_connected = True
                    logger.info("[SUCCESS] Connected to Chrome browser! Reusing persistent session.")

                    # Look for existing open tabs
                    for page in self.context.pages:
                        if "chatgpt.com" in page.url:
                            self.chatgpt_page = page
                            logger.info(f"Found existing open ChatGPT tab: {page.url}")
                        elif "gemini.google.com" in page.url:
                            self.gemini_page = page
                            logger.info(f"Found existing open Gemini tab: {page.url}")
                        elif "notebook" in page.url:
                            self.notebooklm_page = page
                            logger.info(f"Found existing open NotebookLM tab: {page.url}")

                    self.context.set_default_timeout(self.timeout)
                    return
                except Exception as e_cdp:
                    logger.warning(f"CDP connection failed ({e_cdp}). Falling back to persistent context...")


        # Option 2: Persistent context launch
        os.makedirs(self.user_data_dir, exist_ok=True)
        for root, dirs, files in os.walk(self.user_data_dir):
            for f in files:
                if f.lower() in ["lock", "singletonlock", "singletoncookie", "singletonsocket", "parent.lock"]:
                    try:
                        os.remove(os.path.join(root, f))
                    except Exception:
                        pass

        logger.info(f"Launching persistent browser context from: {self.user_data_dir}")

        headless_mode = self.headless if override_headless is None else override_headless


        launch_kwargs = {
            "user_data_dir": self.user_data_dir,
            "headless": headless_mode,
            "slow_mo": self.slow_mo,
            "viewport": {"width": 1366, "height": 850},
            "permissions": ["clipboard-read", "clipboard-write"],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
            "ignore_default_args": ["--enable-automation"],
        }

        try:
            # Use bundled standalone Chromium to guarantee complete isolation from running browsers
            launch_kwargs.pop("channel", None)
            self.context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            logger.info("Launched isolated Chromium persistent context.")
        except Exception as e_chromium:
            logger.warning(f"Chromium launch retry ({e_chromium})...")
            self.context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)


        # Stealth script: remove webdriver flag
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            window.navigator.chrome = {
                runtime: {}
            };
        """)

        self.context.set_default_timeout(self.timeout)

    async def stop(self):
        """Close context and terminate Playwright instance."""
        if self.is_cdp_connected and self.browser:
            # For CDP, disconnect without killing the user's personal browser
            await self.browser.close()
            self.browser = None
            self.context = None
        else:
            if self.context:
                await self.context.close()
                self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        logger.info("Browser session disconnected.")

    async def _safe_input_text(self, page: Any, selector: str, text: str):
        """
        Safely inject text into ProseMirror/Slate contenteditable or textarea without truncation or hanging on disabled fallbacks.
        """
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=8000)
        except Exception:
            pass

        try:
            if await locator.is_enabled():
                await locator.click(timeout=3000)
            else:
                await locator.click(force=True, timeout=3000)
        except Exception:
            pass

        # Clear any existing text
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
        except Exception:
            pass

        inserted = await page.evaluate(
            """({ sel, text }) => {
                let el = document.querySelector(sel);
                if (!el) {
                    el = document.querySelector("div[contenteditable='true']#prompt-textarea, #prompt-textarea, textarea:not([disabled]), textarea");
                }
                if (!el) return false;
                if (el.hasAttribute('disabled')) {
                    el.removeAttribute('disabled');
                }
                el.focus();
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                    el.value = text;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                const ok = document.execCommand('insertText', false, text);
                if (!ok || !el.innerText.trim()) {
                    el.innerText = text;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return true;
            }""",
            {"sel": selector, "text": text},
        )

        if not inserted:
            try:
                await locator.fill(text)
            except Exception:
                try:
                    await locator.type(text, delay=1)
                except Exception:
                    pass


    # -------------------------------------------------------------------------
    # ChatGPT Automation & Extraction
    # -------------------------------------------------------------------------
    async def _extract_chatgpt_dom_response(self, page: Any) -> str:
        """
        Extract the latest assistant response from ChatGPT using a prioritized list of fallback selectors.
        """
        try:
            if page.is_closed():
                return ""
            return await page.evaluate(
                """(selectors) => {
                    for (const sel of selectors) {
                        const nodes = document.querySelectorAll(sel);
                        if (nodes.length > 0) {
                            const lastNode = nodes[nodes.length - 1];
                            const text = lastNode.innerText ? lastNode.innerText.trim() : "";
                            if (text.length > 0) {
                                return text;
                            }
                        }
                    }
                    return "";
                }""",
                self.CHATGPT_SELECTORS,
            )
        except Exception:
            return ""


    async def _extract_chatgpt_clipboard_fallback(self, page: Any) -> str:
        """
        Fallback: Click the native Copy button on the latest assistant turn and read from clipboard.
        """
        logger.info("Attempting clipboard extraction fallback via ChatGPT native Copy button...")
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.3)

            for copy_sel in self.CHATGPT_COPY_BUTTON_SELECTORS:
                buttons = page.locator(copy_sel)
                count = await buttons.count()
                if count > 0:
                    last_button = buttons.last
                    try:
                        await last_button.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await last_button.click(timeout=2000, force=True)
                    await asyncio.sleep(0.3)

                    clipboard_text = await page.evaluate("async () => await navigator.clipboard.readText()")
                    if clipboard_text and len(clipboard_text.strip()) > 0:
                        logger.info(f"Successfully extracted {len(clipboard_text)} characters via clipboard.")
                        return clipboard_text.strip()

            # Attempt SVG closest button
            clicked = await page.evaluate("""() => {
                const svgs = document.querySelectorAll('svg');
                if (svgs.length > 0) {
                    const lastSvg = svgs[svgs.length - 1];
                    const btn = lastSvg.closest('button');
                    if (btn) {
                        btn.scrollIntoView();
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                await asyncio.sleep(0.3)
                clipboard_text = await page.evaluate("async () => await navigator.clipboard.readText()")
                if clipboard_text and len(clipboard_text.strip()) > 0:
                    logger.info(f"Successfully extracted {len(clipboard_text)} characters via SVG copy button.")
                    return clipboard_text.strip()
        except Exception as e:
            logger.debug(f"Clipboard fallback attempt did not succeed: {e}")
        return ""

    async def query_chatgpt(
        self, prompt: str, new_chat: bool = False, chatgpt_url: Optional[str] = None
    ) -> str:
        """
        Send a prompt to ChatGPT (specifically supporting dedicated conversation URLs like https://chatgpt.com/c/<id>)
        and extract the completed response with multi-selector & clipboard fallbacks.
        """
        if not self.context:
            await self.start()

        target_url = (chatgpt_url or self.chatgpt_url).strip()

        if not self.chatgpt_page or self.chatgpt_page.is_closed():
            # Check if there is already an open ChatGPT tab in the browser
            for p in self.context.pages:
                if "chatgpt.com" in p.url and "library" not in p.url:
                    self.chatgpt_page = p
                    break
            # If not found, reuse any available blank tab
            if not self.chatgpt_page:
                for p in self.context.pages:
                    if p != self.notebooklm_page and ("about:blank" in p.url or "chrome://" in p.url or "newtab" in p.url):
                        self.chatgpt_page = p
                        break
            if not self.chatgpt_page:
                self.chatgpt_page = await self.context.new_page()

        norm_target = target_url.rstrip("/")
        norm_current = self.chatgpt_page.url.rstrip("/")

        # Navigate to target_url if different from current page or if fresh chat is requested
        if norm_target != norm_current:
            logger.info(f"Navigating ChatGPT to target URL: {target_url} (was {norm_current})...")
            await self.chatgpt_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
        elif new_chat:
            logger.info(f"Starting new chat at {target_url}...")
            await self.chatgpt_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)




        page = self.chatgpt_page
        await page.bring_to_front()

        # Always refresh ChatGPT page before querying as instructed
        logger.info(f"Refreshing ChatGPT page ({page.url})...")
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:
            if target_url:
                await page.goto(target_url, wait_until="domcontentloaded")

        # Wait for the editable input area to hydrate and become active
        logger.info("Waiting for ChatGPT composer hydration...")
        target_input = None
        for _ in range(30):
            loc = page.locator("div[contenteditable='true']#prompt-textarea, #prompt-textarea, div[contenteditable='true'], textarea:not([disabled])")
            if await loc.count() > 0:
                first = loc.first
                if await first.is_visible() and await first.is_enabled():
                    target_input = "div[contenteditable='true']#prompt-textarea, #prompt-textarea, div[contenteditable='true']"
                    break
            await asyncio.sleep(0.3)

        if not target_input:
            target_input = "div[contenteditable='true']#prompt-textarea, #prompt-textarea"

        # Check for login / auth redirect
        if "auth" in page.url or "login" in page.url:
            logger.warning("ChatGPT login screen detected. Please complete login in the opened Chrome window...")
            for _ in range(120):
                if "auth" not in page.url and "login" not in page.url and "chatgpt.com" in page.url:
                    break
                await asyncio.sleep(1.0)
            await asyncio.sleep(2.0)

        # Record prior state before sending prompt so we extract ONLY the new turn
        prior_turn_count = await page.evaluate(
            "() => document.querySelectorAll(\"div[data-message-author-role='assistant'], article[data-testid^='conversation-turn']\").length"
        )
        prior_text = await self._extract_chatgpt_dom_response(page)

        logger.info(f"Injecting prompt into ChatGPT (Prior turns: {prior_turn_count})...")
        await self._safe_input_text(page, target_input, prompt)



        # Click send or press Enter
        send_btn_selectors = [
            "button[data-testid='send-button']",
            "button[aria-label*='Send prompt']",
            "button[aria-label*='Send message']",
            "button[aria-label*='Send']",
            "button:has(svg)",
        ]
        for s_sel in send_btn_selectors:
            btn = page.locator(s_sel)
            if await btn.count() > 0:
                try:
                    await btn.first.click(force=True, timeout=2000)
                    break
                except Exception:
                    pass

        # Also trigger direct DOM click & Enter
        await page.evaluate("""() => {
            const btn = document.querySelector("button[data-testid='send-button'], button[aria-label*='Send'], button[aria-label*='prompt']");
            if (btn) {
                btn.removeAttribute('disabled');
                btn.click();
            }
        }""")
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")

        logger.info("Watching ChatGPT response stream...")
        
        # Step 1: Fast reactive watch for generation of the NEW turn to start
        for attempt in range(30):
            cur_turn_count = await page.evaluate(
                "() => document.querySelectorAll(\"div[data-message-author-role='assistant'], article[data-testid^='conversation-turn']\").length"
            )
            is_streaming = False
            for stop_sel in self.CHATGPT_STOP_SELECTORS:
                if await page.locator(stop_sel).count() > 0 and await page.locator(stop_sel).first.is_visible():
                    is_streaming = True
                    break
            cur_text = await self._extract_chatgpt_dom_response(page)
            if is_streaming or cur_turn_count > prior_turn_count or (cur_text and cur_text != prior_text):
                break

            # If prompt is still sitting in the input box, retry submitting
            if attempt in [3, 7, 12, 18, 25]:
                logger.info(f"Retrying prompt submission action (attempt {attempt})...")
                for s_sel in send_btn_selectors:
                    btn = page.locator(s_sel)
                    if await btn.count() > 0:
                        try:
                            await btn.first.click(force=True, timeout=1500)
                            break
                        except Exception:
                            pass
                await page.evaluate("""() => {
                    const btn = document.querySelector("button[data-testid='send-button'], button[aria-label*='Send'], button[aria-label*='prompt']");
                    if (btn) {
                        btn.removeAttribute('disabled');
                        btn.click();
                    }
                }""")
                await page.keyboard.press("Enter")

            await asyncio.sleep(0.5)

        # 2. Wait for Stop Generating / Stop Streaming button to disappear
        for stop_sel in self.CHATGPT_STOP_SELECTORS:
            try:
                stop_locator = page.locator(stop_sel)
                if await stop_locator.count() > 0 and await stop_locator.first.is_visible():
                    logger.info(f"Active streaming in progress ({stop_sel}), waiting for completion...")
                    await stop_locator.first.wait_for(state="detached", timeout=180000)
                    break
            except Exception:
                pass

        # 3. Stabilization loop for NEW turn (must be strictly distinct from prior turn)
        start_time = time.time()
        last_text = ""
        stable_since: Optional[float] = None
        timeout_seconds = 180

        while time.time() - start_time < timeout_seconds:
            current_text = await self._extract_chatgpt_dom_response(page)
            # Ensure text is from the new turn (distinct from prior turn)
            if current_text and len(current_text.strip()) > 0 and current_text != prior_text:
                if current_text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= self.stream_settle_timeout:
                        logger.info(f"Successfully extracted new ChatGPT response ({len(current_text)} chars).")
                        return current_text.strip()
                else:
                    last_text = current_text
                    stable_since = None
            else:
                stable_since = None

            await asyncio.sleep(self.stream_poll_interval)

        # 4. Fallback: Try clipboard copy ONLY if it yields new text distinct from prior turn
        clip_text = await self._extract_chatgpt_clipboard_fallback(page)
        if clip_text and len(clip_text.strip()) > 0 and clip_text != prior_text and len(clip_text.strip()) > len(last_text):
            logger.info(f"Extracted {len(clip_text)} chars via clipboard fallback.")
            return clip_text.strip()

        if last_text and last_text != prior_text:
            return last_text.strip()

        logger.error("ChatGPT prompt was not processed or did not produce a new response.")
        raise RuntimeError("ChatGPT prompt submission failed: no new turn response was received from ChatGPT.")


    # -------------------------------------------------------------------------
    # Gemini Automation
    # -------------------------------------------------------------------------
    async def _extract_gemini_latest_response(self, page: Any) -> str:
        """Extract latest assistant response from Gemini and Gemini Notebook UI."""
        try:
            if page.is_closed():
                return ""
            return await page.evaluate("""() => {
                const selectors = [
                    'message-content',
                    '.model-response-text',
                    '.response-container-content',
                    'div.response-container',
                    'div.model-response',
                    '.model-message',
                    'div.chat-message-content',
                    'div.rendered-markdown',
                    'div.markdown',
                    'article'
                ];
                for (const sel of selectors) {
                    const containers = document.querySelectorAll(sel);
                    if (containers.length > 0) {
                        for (let i = containers.length - 1; i >= 0; i--) {
                            const node = containers[i];
                            if (node.closest('.user-message, .from-user-message, [data-message-author="user"]')) {
                                continue;
                            }
                            const txt = node.innerText ? node.innerText.trim() : "";
                            if (txt.length > 0) {
                                return txt;
                            }
                        }
                    }
                }
                return "";
            }""")
        except Exception:
            return ""


    async def query_gemini(
        self, prompt: str, new_chat: bool = False, gemini_url: Optional[str] = None
    ) -> str:
        """
        Send a prompt to Gemini for architecture/plan critique and extract feedback.
        """
        if not self.context:
            await self.start()

        target_url = (gemini_url or self.gemini_url).strip()

        if not self.gemini_page or self.gemini_page.is_closed():
            # Check if there is already an open Gemini tab
            for p in self.context.pages:
                if "gemini.google.com" in p.url:
                    self.gemini_page = p
                    break
            if not self.gemini_page:
                for p in self.context.pages:
                    if p != self.chatgpt_page and p != self.notebooklm_page and ("about:blank" in p.url or "chrome://" in p.url or "newtab" in p.url):
                        self.gemini_page = p
                        break
            if not self.gemini_page:
                self.gemini_page = await self.context.new_page()

            if target_url not in self.gemini_page.url or target_url == "https://gemini.google.com/app":
                await self.gemini_page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
        elif new_chat or (target_url and target_url not in self.gemini_page.url and target_url != "https://gemini.google.com/app"):
            await self.gemini_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)

        page = self.gemini_page
        await page.bring_to_front()

        input_selectors = [
            "textarea[placeholder*='Ask Gemini']",
            "input[placeholder*='Ask Gemini']",
            "div[aria-label*='Ask Gemini']",
            "div[placeholder*='Ask Gemini']",
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true'][role='textbox']",
            "div[contenteditable='true']",
            "div[role='textbox']",
            "textarea[aria-label*='prompt']",
            "textarea[placeholder*='Ask']",
            "div.ql-editor",
            "textarea",
            "input[type='text']",
        ]

        target_input = None
        for sel in input_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0:
                first = loc.first
                if await first.is_visible():
                    target_input = sel
                    break

        if not target_input:
            target_input = "textarea[placeholder*='Ask Gemini'], input[placeholder*='Ask Gemini'], rich-textarea div[contenteditable='true'], div[contenteditable='true'], textarea"

        # Record prior state before sending prompt so we extract ONLY the NEW reply
        prior_turn_count = await page.evaluate(
            "() => document.querySelectorAll('message-content, div.response-container, div.model-response-text, div.markdown, div.rendered-markdown').length"
        )
        prior_text = await self._extract_gemini_latest_response(page)

        logger.info(f"Injecting critique prompt into Gemini (Prior text: {len(prior_text)} chars)...")
        await self._safe_input_text(page, target_input, prompt)

        send_selectors = [
            "button[aria-label*='Send']",
            "button[aria-label*='Submit']",
            "button.send-button",
            "button mat-icon:has-text('send')",
            "button:has(svg)",
        ]
        sent = False
        for s_sel in send_selectors:
            btn = page.locator(s_sel)
            if await btn.count() > 0 and await btn.is_enabled():
                await btn.click()
                sent = True
                break

        if not sent:
            await page.keyboard.press("Enter")

        # Step 1: Wait for Gemini to start generating NEW response
        logger.info("Waiting for Gemini response generation to start...")
        for _ in range(25):
            cur_turn_count = await page.evaluate(
                "() => document.querySelectorAll('message-content, div.response-container, div.model-response-text, div.markdown, div.rendered-markdown').length"
            )
            cur_text = await self._extract_gemini_latest_response(page)
            if cur_turn_count > prior_turn_count or (cur_text and cur_text != prior_text):
                break
            await asyncio.sleep(0.4)

        # Step 2: Wait for Gemini response to fully stream and stabilize
        logger.info("Waiting for Gemini response to stabilize...")
        start_time = time.time()
        last_text = ""
        stable_since: Optional[float] = None
        timeout_seconds = 180

        while time.time() - start_time < timeout_seconds:
            current_text = await self._extract_gemini_latest_response(page)
            if current_text and len(current_text.strip()) > 0 and current_text != prior_text:
                if current_text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= self.stream_settle_timeout:
                        logger.info(f"Extracted new Gemini critique ({len(current_text)} chars).")
                        return current_text.strip()
                else:
                    last_text = current_text
                    stable_since = None
            else:
                stable_since = None

            await asyncio.sleep(self.stream_poll_interval)

        if last_text and last_text != prior_text:
            return last_text.strip()
        return current_text.strip() if (current_text and current_text != prior_text) else ""



    # -------------------------------------------------------------------------
    # Grok Automation (Project Chat & Architecture Critique)
    # -------------------------------------------------------------------------
    async def _extract_grok_latest_response(self, page: Any) -> str:
        """Extract latest assistant response from Grok web."""
        try:
            if page.is_closed():
                return ""
            return await page.evaluate("""() => {
                const selectors = [
                    'div[data-message-author="assistant"]',
                    'div.message-bubble',
                    '.response-bubble',
                    '.prose',
                    'div.markdown',
                    'div[class*="response"]',
                    'div[class*="message"]'
                ];
                for (const sel of selectors) {
                    const containers = document.querySelectorAll(sel);
                    if (containers.length > 0) {
                        const last = containers[containers.length - 1];
                        const txt = last.innerText ? last.innerText.trim() : "";
                        if (txt.length > 5) return txt;
                    }
                }
                return "";
            }""")
        except Exception:
            return ""

    async def query_grok(
        self, prompt: str, new_chat: bool = False, grok_url: Optional[str] = None
    ) -> str:
        """
        Send a prompt to Grok for architecture critique and extract feedback.
        """
        if not self.context:
            await self.start()

        target_url = (grok_url or self.grok_url).strip()

        if not self.grok_page or self.grok_page.is_closed():
            # Check if there is already an open Grok tab
            for p in self.context.pages:
                if "grok.com" in p.url:
                    self.grok_page = p
                    break
            if not self.grok_page:
                for p in self.context.pages:
                    if p != self.chatgpt_page and p != self.notebooklm_page and p != self.gemini_page and ("about:blank" in p.url or "chrome://" in p.url or "newtab" in p.url):
                        self.grok_page = p
                        break
            if not self.grok_page:
                self.grok_page = await self.context.new_page()

            if target_url not in self.grok_page.url:
                await self.grok_page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
        elif new_chat or (target_url and target_url not in self.grok_page.url):
            await self.grok_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)

        page = self.grok_page
        await page.bring_to_front()

        input_selectors = [
            "textarea[placeholder*='Ask']",
            "textarea[placeholder*='Grok']",
            "textarea",
            "div[contenteditable='true']",
            "rich-textarea div[contenteditable='true']",
        ]

        target_input = None
        for sel in input_selectors:
            if await page.locator(sel).count() > 0:
                target_input = sel
                break

        # Record prior state before sending prompt so we extract ONLY the NEW reply
        prior_turn_count = await page.evaluate(
            "() => document.querySelectorAll('div[data-message-author=\"assistant\"], div.message-bubble, .response-bubble').length"
        )
        prior_text = await self._extract_grok_latest_response(page)

        logger.info(f"Injecting critique prompt into Grok (Prior text: {len(prior_text)} chars)...")
        await self._safe_input_text(page, target_input, prompt)

        send_selectors = [
            "button[aria-label*='Send']",
            "button[type='submit']",
            "button:has(svg)",
        ]
        sent = False
        for s_sel in send_selectors:
            btn = page.locator(s_sel)
            if await btn.count() > 0 and await btn.is_enabled():
                await btn.click()
                sent = True
                break

        if not sent:
            await page.keyboard.press("Enter")

        # Step 1: Wait for Grok to start generating NEW response
        logger.info("Waiting for Grok response generation to start...")
        for _ in range(25):
            cur_turn_count = await page.evaluate(
                "() => document.querySelectorAll('div[data-message-author=\"assistant\"], div.message-bubble, .response-bubble').length"
            )
            cur_text = await self._extract_grok_latest_response(page)
            if cur_turn_count > prior_turn_count or (cur_text and cur_text != prior_text):
                break
            await asyncio.sleep(0.4)

        # Step 2: Wait for Grok response to fully stream and stabilize
        logger.info("Waiting for Grok critique response to stabilize...")
        start_time = time.time()
        last_text = ""
        stable_since: Optional[float] = None
        timeout_seconds = 180

        while time.time() - start_time < timeout_seconds:
            current_text = await self._extract_grok_latest_response(page)
            if current_text and len(current_text.strip()) > 0 and current_text != prior_text:
                if current_text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= self.stream_settle_timeout:
                        logger.info(f"Extracted new Grok critique ({len(current_text)} chars).")
                        return current_text.strip()
                else:
                    last_text = current_text
                    stable_since = None
            else:
                stable_since = None

            await asyncio.sleep(self.stream_poll_interval)

        if last_text and last_text != prior_text:
            return last_text.strip()
        return current_text.strip() if (current_text and current_text != prior_text) else ""


    async def _extract_notebooklm_latest_response(self, page: Any) -> str:
        """Extract latest assistant response from NotebookLM chat, ignoring temporary thinking placeholders and user messages."""
        try:
            if page.is_closed():
                return ""
            return await page.evaluate("""() => {
                const selectors = [
                    '.to-user-message-card-content',
                    'mat-card.to-user-message-card-content',
                    '.to-user-message',
                    'div.to-user-message-card',
                    'div[data-message-author="model"]',
                    'div.model-response',
                    '.chat-response-container',
                    'div.chat-message-content',
                    'div.message-content',
                    'div.markdown-renderer',
                    'div.markdown',
                    'article'
                ];
                for (const sel of selectors) {
                    const containers = document.querySelectorAll(sel);
                    if (containers.length > 0) {
                        for (let i = containers.length - 1; i >= 0; i--) {
                            const node = containers[i];
                            if (node.closest('.from-user-message, .from-user-message-card-content, .user-message')) {
                                continue;
                            }
                            const txt = node.innerText ? node.innerText.trim() : "";
                            if (txt.length > 0) {
                                const lower = txt.toLowerCase();
                                const isThinkingPlaceholder = (
                                    txt.length < 250 && (
                                        lower.includes('consulting') || 
                                        lower.includes('sources') || 
                                        lower.includes('defining strategy') || 
                                        lower.includes('validating assumptions') || 
                                        lower.includes('thinking') ||
                                        lower.includes('thoughts') || 
                                        lower.includes('finding') || 
                                        lower.includes('searching') || 
                                        lower.includes('reading') || 
                                        lower.includes('analyzing') || 
                                        lower.includes('working') ||
                                        lower.includes('loading') ||
                                        lower.includes('generating') ||
                                        lower.endsWith('...')
                                    )
                                );
                                if (!isThinkingPlaceholder && txt.length >= 40) {
                                    return txt;
                                }
                            }
                        }
                    }
                }
                return "";
            }""")
        except Exception:
            return ""

    async def _is_notebooklm_generating(self, page: Any) -> bool:
        """Checks if NotebookLM is actively generating a response by checking red stop button & 'Responding...' text."""
        try:
            if page.is_closed():
                return False
            return await page.evaluate("""() => {
                // 1. Check ALL textareas for 'responding' / 'thinking' / 'consulting' in placeholder
                const textareas = document.querySelectorAll('textarea');
                for (const ta of textareas) {
                    const ph = (ta.getAttribute('placeholder') || '').toLowerCase();
                    const val = (ta.value || '').toLowerCase();
                    if (ph.includes('responding') || val.includes('responding') || ph.includes('thinking') || ph.includes('consulting')) {
                        return true;
                    }
                }
                
                // 2. Check for stop button (aria-label containing stop / red background / stop text)
                const allButtons = document.querySelectorAll('button');
                for (const b of allButtons) {
                    if (b.offsetParent !== null) { // visible
                        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        const txt = (b.innerText || '').toLowerCase();
                        if (aria.includes('stop') || txt === 'stop') {
                            return true;
                        }
                        const icon = b.querySelector('mat-icon');
                        if (icon && (icon.innerText || '').trim().toLowerCase() === 'stop') {
                            return true;
                        }
                        const bg = window.getComputedStyle(b).backgroundColor;
                        if (bg.includes('217, 48, 37') || bg.includes('234, 67, 53') || bg.includes('197, 34, 31')) {
                            return true;
                        }
                    }
                }
                
                // 3. Check for generating progress bars / spinners
                const spinners = document.querySelectorAll('mat-progress-spinner, mat-progress-bar, .thinking-chain__generating-icon, .thinking-indicator, div[role="progressbar"]');
                for (const s of spinners) {
                    if (s.offsetParent !== null) {
                        return true;
                    }
                }
                
                return false;
            }""")
        except Exception:
            return False


    async def enforce_notebooklm_single_source(self, page: Any, target_source_name: str):

        """
        Hardened Source Selection Invariant:
        1. Wait for processing spinners/progress bars to finish indexing.
        2. Ensure the authoritative document is checked in NotebookLM source list.
        """

        try:
            if page.is_closed():
                return
            # 1. Wait for processing spinners to disappear
            spinners = page.locator("mat-progress-spinner, mat-progress-bar, .spinner, .loading, div[role='progressbar']")
            if await spinners.count() > 0:
                try:
                    await spinners.first.wait_for(state="detached", timeout=12000)
                except Exception:
                    pass

            # 2. Check if source checkboxes are present in the Sources panel
            cb_loc = page.locator("mat-checkbox")
            count = await cb_loc.count()
            if count > 0:
                logger.info(f"Inspecting NotebookLM source checkboxes ({count} total) for '{target_source_name}'...")
                # Search for target checkbox
                clean_target = os.path.splitext(target_source_name)[0]
                target_cb = page.locator(f"mat-checkbox:has-text('{target_source_name}'), mat-checkbox:has-text('{clean_target}')")
                if await target_cb.count() > 0:
                    first_cb = target_cb.first
                    cls = await first_cb.get_attribute("class") or ""
                    if "mat-mdc-checkbox-checked" not in cls and "checked" not in cls:
                        logger.info(f"Activating NotebookLM checkbox for target source '{target_source_name}'...")
                        await first_cb.click(force=True)
                        await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug(f"enforce_notebooklm_single_source non-fatal: {e}")

    async def remove_old_notebooklm_sources(self, page: Any, new_doc_name: str, delete_existing_exact: bool = True) -> int:
        """Scans NotebookLM Sources list and deletes previous versions/duplicates of this document."""
        import re
        base_prefix = re.sub(r"[-_\.]v\d+(?:\.\d+)*.*$", "", new_doc_name, flags=re.IGNORECASE).strip()
        if not base_prefix or len(base_prefix) < 3:
            return 0

        logger.info(f"Checking NotebookLM for outdated versions/duplicates of '{base_prefix}'...")
        removed_count = 0
        try:
            for _ in range(15):
                # 1. Locate and click more options button for duplicate / older version
                result = await page.evaluate("""(data) => {
                    const prefix = data.prefix.toLowerCase();
                    const newName = data.newName.toLowerCase();
                    const deleteExact = data.deleteExact;
                    const containers = Array.from(document.querySelectorAll('.single-source-container'));
                    
                    let target = null;
                    for (const c of containers) {
                        const txt = (c.innerText || "").toLowerCase();
                        if (txt.includes(prefix)) {
                            // If deleteExact is True, delete ANY matching container of this document
                            // Otherwise, delete if it is an older version OR duplicate
                            if (deleteExact || !txt.includes(newName)) {
                                target = c;
                                break;
                            }
                        }
                    }
                    
                    // Fallback: check if multiple identical copies exist
                    if (!target) {
                        const sameMatches = containers.filter(c => (c.innerText || "").toLowerCase().includes(newName));
                        if (sameMatches.length > 1) {
                            target = sameMatches[1];
                        }
                    }
                    
                    if (!target) return { found: false };
                    target.scrollIntoView();
                    
                    const btn = target.querySelector('button.source-item-more-button, button.mat-mdc-menu-trigger, button[aria-label*="More"], button[aria-label*="more"]');
                    if (btn) {
                        btn.click();
                        return { found: true, clicked: true, text: target.innerText.slice(0, 40) };
                    }
                    return { found: true, clicked: false, text: target.innerText.slice(0, 40) };
                }""", {"prefix": base_prefix, "newName": new_doc_name, "deleteExact": delete_existing_exact})


                if not result.get("found") or not result.get("clicked"):
                    break

                await asyncio.sleep(0.5)

                # 2. Click Delete / Remove source from menu
                del_clicked = await page.evaluate("""() => {
                    const menuItems = Array.from(document.querySelectorAll('div[role="menu"] button, .mat-mdc-menu-item, button'));
                    for (const item of menuItems) {
                        const txt = (item.innerText || "").toLowerCase();
                        if (txt.includes('delete') || txt.includes('remove')) {
                            item.click();
                            return { clicked: true, text: item.innerText.trim() };
                        }
                    }
                    return { clicked: false };
                }""")

                if del_clicked.get("clicked"):
                    await asyncio.sleep(0.5)
                    # 3. Confirm dialog
                    await page.evaluate("""() => {
                        const dialogBtns = Array.from(document.querySelectorAll('mat-dialog-container button, div[role="dialog"] button, button'));
                        for (const btn of dialogBtns) {
                            const txt = (btn.innerText || "").toLowerCase();
                            if (txt === 'delete' || txt === 'remove' || txt.includes('delete')) {
                                btn.click();
                                return { confirmed: true };
                            }
                        }
                        return { confirmed: false };
                    }""")
                    removed_count += 1
                    logger.info(f"Successfully deleted outdated source #{removed_count} from NotebookLM.")
                    await asyncio.sleep(1.2)
                else:
                    await page.keyboard.press("Escape")
                    break
        except Exception as e_del:
            logger.debug(f"NotebookLM old version removal non-fatal: {e_del}")
        return removed_count





    async def remove_old_chatgpt_library_files(self, lib_page: Any, new_doc_name: str) -> int:
        """Scans ChatGPT Project / Library folder and removes previous versions/duplicates of this document."""
        import re
        base_prefix = re.sub(r"[-_\.]v\d+(?:\.\d+)*.*$", "", new_doc_name, flags=re.IGNORECASE).strip()
        if not base_prefix or len(base_prefix) < 3:
            return 0

        logger.info(f"Checking ChatGPT Library for outdated versions/duplicates of '{base_prefix}'...")
        removed_count = 0
        try:
            for _ in range(12):
                # Search for matching action buttons in ChatGPT library table
                buttons = lib_page.locator(f"button[aria-label*='Open actions menu for {base_prefix}']")
                count = await buttons.count()
                if count <= 1:
                    break

                target_btn = None
                for i in range(count):
                    b = buttons.nth(i)
                    aria = (await b.get_attribute("aria-label") or "").lower()
                    if "(" in aria or new_doc_name.lower() not in aria:
                        target_btn = b
                        break

                if not target_btn:
                    target_btn = buttons.nth(1)

                await target_btn.scroll_into_view_if_needed()
                await target_btn.click(force=True)
                await asyncio.sleep(0.5)

                del_option = lib_page.locator("div[role='menuitem']:has-text('Delete'), [role='menuitem']:has-text('Delete'), button:has-text('Delete')")
                if await del_option.count() > 0 and await del_option.first.is_visible():
                    await del_option.first.click(force=True)
                    await asyncio.sleep(0.5)

                    confirm_btn = lib_page.locator("div[role='dialog'] button:has-text('Delete'), button.btn-danger, button:has-text('Delete')")
                    if await confirm_btn.count() > 0 and await confirm_btn.first.is_visible():
                        await confirm_btn.first.click(force=True)
                    removed_count += 1
                    logger.info(f"Successfully deleted outdated ChatGPT library file #{removed_count}.")
                    await asyncio.sleep(2.0)
                else:
                    await lib_page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
                    break
        except Exception as e_del:
            logger.debug(f"ChatGPT library old version removal non-fatal: {e_del}")
        return removed_count



    async def upload_source_to_chatgpt_library(
        self,
        library_url: str,
        file_path: str,
        doc_title: str = "",
    ) -> bool:
        """Uploads or replaces a source document directly into ChatGPT Project / Library folder."""
        if not self.context:
            await self.start()

        target_url = (library_url or "https://chatgpt.com/library/d/6a723b6fe06c819199240f5a593f7ab4").strip()
        abs_file_path = os.path.abspath(file_path)

        if not os.path.exists(abs_file_path):
            logger.error(f"File to upload does not exist at: {abs_file_path}. Cannot upload to ChatGPT.")
            return False

        # Find or open a page for ChatGPT Library
        lib_page = None
        for p in self.context.pages:
            if "library" in p.url or "project" in p.url:
                lib_page = p
                break

        if not lib_page:
            lib_page = await self.context.new_page()
            await lib_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.5)
        elif target_url not in lib_page.url:
            await lib_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.5)

        await lib_page.bring_to_front()

        # Check for login / auth redirect
        if "auth" in lib_page.url or "login" in lib_page.url:
            logger.warning("ChatGPT login screen detected. Please complete login in the opened Chrome window...")
            for _ in range(120):
                if "auth" not in lib_page.url and "login" not in lib_page.url and "chatgpt.com" in lib_page.url:
                    break
                await asyncio.sleep(1.0)
            await asyncio.sleep(2.0)

        doc_name = doc_title or os.path.basename(abs_file_path)

        # Step 0: Remove old versions of this document before uploading new version
        await self.remove_old_chatgpt_library_files(lib_page, doc_name)

        logger.info(f"Updating ChatGPT library at {target_url} with '{doc_name}'...")


        try:
            await lib_page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # 1. Look for direct file input
            file_inputs = lib_page.locator("input[type='file']")
            if await file_inputs.count() > 0:
                await file_inputs.first.set_input_files(abs_file_path)
                logger.info(f"Attached '{doc_name}' directly to ChatGPT file input. Waiting for upload...")
                await asyncio.sleep(4.0)
                return True

            # 2. Look for Add / Upload buttons
            upload_btn_selectors = [
                "button:has-text('Add files')",
                "button:has-text('Upload files')",
                "button:has-text('Add file')",
                "button:has-text('Upload')",
                "button:has-text('Add')",
                "button[aria-label*='Add']",
                "button[aria-label*='Upload']",
                "button[data-testid*='upload']",
            ]
            target_btn = None
            for sel in upload_btn_selectors:
                loc = lib_page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    target_btn = loc.first
                    break

            if target_btn:
                async with lib_page.expect_file_chooser(timeout=10000) as fc_info:
                    await target_btn.click(force=True)
                file_chooser = await fc_info.value
                await file_chooser.set_files(abs_file_path)
                logger.info(f"Attached '{doc_name}' to ChatGPT library via file chooser.")
                await asyncio.sleep(5.0)
                return True
            else:
                logger.warning("No direct upload button found in ChatGPT library view.")
                return False
        except Exception as e_up:
            logger.warning(f"ChatGPT library file upload exception: {e_up}. Continuing...")
            return False

    async def upload_source_to_notebooklm(


        self,
        notebook_url: str,
        file_path: str,
        doc_title: str = "",
    ) -> bool:
        """Uploads or replaces a source document directly in NotebookLM sources panel."""
        if not self.context:
            await self.start()

        target_url = notebook_url or self.notebooklm_url
        abs_file_path = os.path.abspath(file_path)

        if not os.path.exists(abs_file_path):
            logger.error(f"File to upload does not exist at: {abs_file_path}. Cannot upload.")
            return False

        if not self.notebooklm_page or self.notebooklm_page.is_closed():
            for p in self.context.pages:
                if "notebook" in p.url:
                    self.notebooklm_page = p
                    break
            if not self.notebooklm_page:
                for p in self.context.pages:
                    if p != self.chatgpt_page and p != self.gemini_page and p != self.grok_page and ("about:blank" in p.url or "chrome://" in p.url or "newtab" in p.url):
                        self.notebooklm_page = p
                        break
            if not self.notebooklm_page:
                self.notebooklm_page = await self.context.new_page()

            if target_url not in self.notebooklm_page.url:
                await self.notebooklm_page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)
        elif target_url and target_url not in self.notebooklm_page.url:
            await self.notebooklm_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

        page = self.notebooklm_page
        await page.bring_to_front()

        # Check for Google account sign-in redirect
        if "accounts.google.com" in page.url or "signin" in page.url:
            logger.warning("Google sign-in page detected. Please complete sign-in in the opened Chrome window...")
            for _ in range(120):
                if "accounts.google.com" not in page.url and "notebook" in page.url:
                    break
                await asyncio.sleep(1.0)
            await asyncio.sleep(2.0)

        doc_name = doc_title or os.path.basename(abs_file_path)
        doc_content = ""
        try:
            with open(abs_file_path, "r", encoding="utf-8", errors="replace") as f:
                doc_content = f.read()
        except Exception as e_read:
            logger.warning(f"Failed to read file {abs_file_path}: {e_read}")

        # Step 0: Remove all previous versions and existing duplicates of this document before uploading
        await self.remove_old_notebooklm_sources(page, doc_name, delete_existing_exact=True)

        logger.info(f"Updating source in NotebookLM: '{doc_name}' ({abs_file_path})...")

        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # Check if Add Sources modal is already open
            upload_btn = page.locator("button:has-text('Upload files'), button:has-text('Upload')")
            if not (await upload_btn.count() > 0 and await upload_btn.first.is_visible()):
                add_source_btn = page.locator("button[aria-label='Add source'], button:has-text('Add sources')")
                if await add_source_btn.count() > 0 and await add_source_btn.first.is_visible():
                    await add_source_btn.first.click(force=True)
                    await asyncio.sleep(1.5)


            upload_btn = page.locator("button:has-text('Upload files'), button:has-text('Upload')")
            if await upload_btn.count() > 0 and await upload_btn.first.is_visible():
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    await upload_btn.first.click(force=True)
                file_chooser = await fc_info.value
                await file_chooser.set_files(abs_file_path)
                logger.info(f"Attached '{doc_name}' via file chooser. Waiting for indexing...")
                
                # Wait for upload modal to close automatically or dismiss overlay
                for _ in range(12):
                    await asyncio.sleep(1.0)
                    if await upload_btn.count() == 0 or not (await upload_btn.first.is_visible()):
                        break
                await page.keyboard.press("Escape")
                await asyncio.sleep(2.0)
                return True
            else:
                # Copied text fallback
                copied_btn = page.locator("button:has-text('Copied text')")
                if await copied_btn.count() > 0 and await copied_btn.first.is_visible():
                    await copied_btn.first.click(force=True)
                    await asyncio.sleep(1.0)
                    title_input = page.locator("input[placeholder*='Title'], input[aria-label*='Title']")
                    if await title_input.count() > 0:
                        await title_input.first.fill(doc_name)
                    txt_area = page.locator("textarea")
                    if await txt_area.count() > 0:
                        await txt_area.fill(doc_content)
                        await asyncio.sleep(0.5)
                    insert_btn = page.locator("button:has-text('Insert')")
                    if await insert_btn.count() > 0:
                        await insert_btn.first.click(force=True)
                        logger.info(f"Attached '{doc_name}' as copied text source.")
                        await asyncio.sleep(6.0)
                await page.keyboard.press("Escape")
                await asyncio.sleep(1.5)
                return True
        except Exception as e_src:
            logger.warning(f"Source addition exception in NotebookLM: {e_src}")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            return False

    async def upload_source_and_review_in_notebooklm(
        self,
        notebook_url: str,
        file_path: str,
        doc_title: str = "architecture_v1.md",

        doc_prefix_or_num: str = "",
        custom_review_prompt: str = "",
    ) -> str:
        """
        1. Navigates to the specified NotebookLM URL.
        2. Uploads the newly extracted ChatGPT markdown file directly into the notebook sources.
        3. Prompts NotebookLM with the exact user prompt + document text.
        """
        if not self.context:
            await self.start()

        target_url = notebook_url or self.notebooklm_url
        abs_file_path = os.path.abspath(file_path)

        if not os.path.exists(abs_file_path):
            logger.error(f"File to upload does not exist at: {abs_file_path}. Falling back to prompt.")
            return await self.query_notebooklm(f"Please review architecture spec: {doc_title}")

        if not self.notebooklm_page or self.notebooklm_page.is_closed():
            for p in self.context.pages:
                if "notebook" in p.url:
                    self.notebooklm_page = p
                    break
            if not self.notebooklm_page:
                for p in self.context.pages:
                    if p != self.chatgpt_page and ("about:blank" in p.url or "chrome://" in p.url or "newtab" in p.url):
                        self.notebooklm_page = p
                        break
            if not self.notebooklm_page:
                self.notebooklm_page = await self.context.new_page()

            if target_url not in self.notebooklm_page.url:
                await self.notebooklm_page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(3.0)
        else:
            if target_url not in self.notebooklm_page.url:
                await self.notebooklm_page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(3.0)


        page = self.notebooklm_page
        await page.bring_to_front()

        # Check for Google account sign-in redirect
        if "accounts.google.com" in page.url or "signin" in page.url:
            logger.warning("Google sign-in page detected. Please complete sign-in in the opened Chrome window...")
            for _ in range(120):
                if "accounts.google.com" not in page.url and "notebook" in page.url:
                    break
                await asyncio.sleep(1.0)
            await asyncio.sleep(3.0)



        # Read the document content
        doc_content = ""
        try:
            with open(abs_file_path, "r", encoding="utf-8", errors="replace") as f:
                doc_content = f.read()
        except Exception as e_read:
            logger.warning(f"Failed to read file {abs_file_path}: {e_read}")

        # STEP 1: Attempt to add as a source in NotebookLM
        logger.info(f"Uploading '{doc_title}' ({abs_file_path}) to NotebookLM sources panel...")
        try:
            # Dismiss any prior stray dialogs or escape keys
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # Check if Add Sources modal is already open
            upload_btn = page.locator("button:has-text('Upload files'), button:has-text('Upload')")
            if not (await upload_btn.count() > 0 and await upload_btn.first.is_visible()):
                add_source_btn = page.locator("button[aria-label='Add source'], button:has-text('Add sources')")
                if await add_source_btn.count() > 0 and await add_source_btn.first.is_visible():
                    await add_source_btn.first.click(force=True)
                    await asyncio.sleep(1.5)

            upload_btn = page.locator("button:has-text('Upload files'), button:has-text('Upload')")
            if await upload_btn.count() > 0 and await upload_btn.first.is_visible():
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    await upload_btn.first.click(force=True)
                file_chooser = await fc_info.value
                await file_chooser.set_files(abs_file_path)
                logger.info(f"Attached '{doc_title}' via file chooser. Waiting for indexing...")
                
                # Wait for upload modal to close automatically or dismiss overlay
                for _ in range(8):
                    await asyncio.sleep(1.0)
                    if await upload_btn.count() == 0 or not (await upload_btn.first.is_visible()):
                        break
                await page.keyboard.press("Escape")
                await asyncio.sleep(1.5)
            else:
                # Copied text fallback
                copied_btn = page.locator("button:has-text('Copied text')")
                if await copied_btn.count() > 0 and await copied_btn.first.is_visible():
                    await copied_btn.first.click(force=True)
                    await asyncio.sleep(1.0)
                    title_input = page.locator("input[placeholder*='Title'], input[aria-label*='Title']")
                    if await title_input.count() > 0:
                        await title_input.first.fill(doc_title)
                    txt_area = page.locator("textarea")
                    if await txt_area.count() > 0:
                        await txt_area.first.fill(doc_content)
                        await asyncio.sleep(0.5)
                    insert_btn = page.locator("button:has-text('Insert')")
                    if await insert_btn.count() > 0:
                        await insert_btn.first.click(force=True)
                        logger.info(f"Attached '{doc_title}' as copied text source.")
                        await asyncio.sleep(6.0)
                await page.keyboard.press("Escape")
                await asyncio.sleep(1.0)
        except Exception as e_src:
            logger.debug(f"Source addition in NotebookLM UI: {e_src}")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)



        # STEP 2: Formulate Cross-Document Review Prompt (Exact User Prompt + Document content)
        if custom_review_prompt and len(custom_review_prompt.strip()) > 0:
            review_prompt = f"""{custom_review_prompt.strip()}

### Document Under Review: '{doc_title}'
---
{doc_content}
---"""
        else:
            ref_text = f"(specifically cross-referencing with document '{doc_prefix_or_num}')" if doc_prefix_or_num else ""
            review_prompt = f"""### Document Under Review: '{doc_title}'
---
{doc_content}
---

Please perform a comprehensive cross-document architectural evaluation of the above '{doc_title}' specification against all existing reference documents and engineering roadmaps in this notebook {ref_text}:

1. Contradictions & Conflicts: Identify any discrepancies or conflicting assumptions between this '{doc_title}' specification and the existing engineering documents in this notebook.
2. Gaps & Missing Capabilities: Highlight unaddressed edge cases, missing components, lifecycle gaps, or security/scalability risks.
3. Project Roadmap Alignment: Evaluate adherence to the Phase 4 architectural standards, naming conventions, and design patterns established across the other sources.
4. Actionable Refinement Recommendations: Provide concrete, prioritized modifications to improve and stabilize '{doc_title}' in the next iteration."""



        # STEP 3: Send Prompt to NotebookLM Chat
        logger.info("Dismissing any modal dialog backdrops in NotebookLM...")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)

        chat_input_selectors = [
            "textarea.query-box-input",
            "textarea[aria-label='Query box']",
            "textarea[placeholder*='Ask a question']",
            "textarea[placeholder*='Ask']",
            "textarea[aria-label*='chat']",
            "textarea[placeholder*='chat']",
            "textarea",
        ]
        query_loc = None
        for sel in chat_input_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0:
                query_loc = loc.first
                break

        # Record prior state before sending prompt so we extract ONLY the NEW reply
        prior_turn_count = await page.evaluate("""() => {
            return document.querySelectorAll('mat-card.to-user-message-card-content, div.to-user-message-card-content, .to-user-message, div.model-response, div.chat-message-content').length;
        }""")
        prior_text = await self._extract_notebooklm_latest_response(page)

        logger.info(f"Injecting cross-document comparative review prompt into NotebookLM chat (Prior text: {len(prior_text)} chars)...")
        if query_loc:
            try:
                await query_loc.click(force=True)
                await query_loc.fill(review_prompt)
                await asyncio.sleep(0.5)
                await query_loc.press("Enter")
            except Exception as e_fill:
                logger.debug(f"Direct fill attempt: {e_fill}. Using _safe_input_text...")
                await self._safe_input_text(page, "textarea.query-box-input, textarea", review_prompt)
                await page.keyboard.press("Enter")
        else:
            await self._safe_input_text(page, "textarea", review_prompt)
            await page.keyboard.press("Enter")

        submit_btn = page.locator(
            "button[aria-label*='Submit'], button[aria-label*='submit'], button.submit-button, button[aria-label*='Send'], button:has-text('send')"
        )
        if await submit_btn.count() > 0 and await submit_btn.first.is_enabled():
            try:
                await submit_btn.first.click(force=True)
            except Exception:
                pass

        # Step 1: Wait for NotebookLM to start generating NEW response
        logger.info("Waiting for NotebookLM review generation to start...")
        for _ in range(25):
            cur_turn_count = await page.evaluate("""() => {
                return document.querySelectorAll('mat-card.to-user-message-card-content, div.to-user-message-card-content, .to-user-message, div.model-response, div.chat-message-content').length;
            }""")
            cur_text = await self._extract_notebooklm_latest_response(page)
            if cur_turn_count > prior_turn_count or (cur_text and cur_text != prior_text):
                break
            await asyncio.sleep(0.4)

        # Step 2: Wait for NotebookLM response to fully stream and stabilize
        logger.info("Waiting for NotebookLM response to stabilize...")
        start_time = time.time()
        last_text = ""
        stable_since: Optional[float] = None
        timeout_seconds = 180

        while time.time() - start_time < timeout_seconds:
            current_text = await self._extract_notebooklm_latest_response(page)
            if (
                current_text
                and len(current_text.strip()) >= 60
                and current_text != prior_text
                and not any(
                    w in current_text.lower()
                    for w in ["consulting your sources", "searching sources", "reading sources", "thinking..."]
                )
            ):
                if current_text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= self.stream_settle_timeout:
                        logger.info(f"Extracted {len(current_text)} characters of critique from NotebookLM.")
                        return current_text.strip()
                else:
                    last_text = current_text
                    stable_since = None
            else:
                stable_since = None

            await asyncio.sleep(self.stream_poll_interval)

        if last_text and last_text != prior_text:
            return last_text.strip()

        return current_text.strip() if (current_text and current_text != prior_text) else ""



    async def query_notebooklm(self, prompt: str, notebook_url: Optional[str] = None) -> str:
        """Direct chat query to NotebookLM without file upload."""
        if not self.context:
            await self.start()

        target_url = (notebook_url or self.notebooklm_url).strip()

        if not self.notebooklm_page or self.notebooklm_page.is_closed():
            for p in self.context.pages:
                if "notebook" in p.url:
                    self.notebooklm_page = p
                    break
            if not self.notebooklm_page:
                for p in self.context.pages:
                    if p != self.chatgpt_page and p != self.gemini_page and p != self.grok_page and ("about:blank" in p.url or "chrome://" in p.url or "newtab" in p.url):
                        self.notebooklm_page = p
                        break
            if not self.notebooklm_page:
                self.notebooklm_page = await self.context.new_page()

            if target_url not in self.notebooklm_page.url:
                await self.notebooklm_page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
        elif target_url and target_url not in self.notebooklm_page.url:
            await self.notebooklm_page.goto(target_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)

        page = self.notebooklm_page
        await page.bring_to_front()

        # Dismiss any overlays
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

        # Enforce source selection and indexing completion for the target document
        import re
        match_doc = re.search(r"Document Under Review:\s*([^\n\r]+)", prompt, re.IGNORECASE)
        if match_doc:
            target_doc_name = match_doc.group(1).strip()
            await self.enforce_notebooklm_single_source(page, target_doc_name)

        # Record prior state before sending prompt so we extract ONLY the NEW reply
        prior_turn_count = await page.evaluate("""() => {
            return document.querySelectorAll('mat-card.to-user-message-card-content, div.to-user-message-card-content, .to-user-message, div.model-response, div.chat-message-content').length;
        }""")
        prior_text = await self._extract_notebooklm_latest_response(page)


        chat_input_selectors = [
            "textarea.query-box-input",
            "textarea[aria-label='Query box']",
            "textarea[placeholder*='Ask a question']",
            "textarea[placeholder*='Ask']",
            "textarea[aria-label*='chat']",
            "textarea[placeholder*='chat']",
            "textarea",
        ]
        query_loc = None
        for sel in chat_input_selectors:
            loc = page.locator(sel)
            if await loc.count() > 0:
                query_loc = loc.first
                break

        logger.info(f"Injecting prompt into NotebookLM chat (Prior text: {len(prior_text)} chars)...")
        if query_loc:
            try:
                await query_loc.click(force=True)
                await query_loc.fill(prompt)
                await asyncio.sleep(0.3)
                await query_loc.press("Enter")
            except Exception:
                await self._safe_input_text(page, "textarea.query-box-input, textarea", prompt)
                await page.keyboard.press("Enter")
        else:
            await self._safe_input_text(page, "textarea", prompt)
            await page.keyboard.press("Enter")

        submit_btn = page.locator(
            "button[aria-label*='Submit'], button[aria-label*='submit'], button.submit-button, button[aria-label*='Send'], button:has-text('send')"
        )
        if await submit_btn.count() > 0 and await submit_btn.first.is_enabled():
            try:
                await submit_btn.first.click(force=True)
            except Exception:
                pass

        # Step 1: Wait for NotebookLM to start generating NEW response
        logger.info("Waiting for NotebookLM response generation to start...")
        for _ in range(25):
            cur_turn_count = await page.evaluate("""() => {
                return document.querySelectorAll('mat-card.to-user-message-card-content, div.to-user-message-card-content, .to-user-message, div.model-response, div.chat-message-content').length;
            }""")
            cur_text = await self._extract_notebooklm_latest_response(page)
            if cur_turn_count > prior_turn_count or (cur_text and cur_text != prior_text):
                break
            await asyncio.sleep(0.4)

        # Step 2: Wait for NotebookLM response to fully stream and stabilize
        logger.info("Waiting for NotebookLM response generation and stabilization...")
        start_time = time.time()
        last_text = ""
        stable_since: Optional[float] = None
        timeout_seconds = 240

        while time.time() - start_time < timeout_seconds:
            is_generating = await self._is_notebooklm_generating(page)
            current_text = await self._extract_notebooklm_latest_response(page)

            # If NotebookLM is still generating (red stop button visible or 'Responding...' in box), keep waiting!
            if is_generating:
                stable_since = None
                last_text = current_text
                await asyncio.sleep(self.stream_poll_interval)
                continue

            # When generation has finished, ensure we have the complete, stable text
            if (
                current_text
                and len(current_text.strip()) >= 200
                and current_text != prior_text
                and not any(
                    w in current_text.lower()
                    for w in [
                        "consulting your sources",
                        "searching sources",
                        "reading sources",
                        "thinking...",
                        "defining strategy",
                        "validating assumptions",
                    ]
                )
            ):
                if current_text == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= max(2.5, self.stream_settle_timeout):
                        logger.info(f"Extracted authoritative NotebookLM evaluation ({len(current_text)} chars, stop button cleared).")
                        return current_text.strip()
                else:
                    last_text = current_text
                    stable_since = None
            else:
                stable_since = None

            await asyncio.sleep(self.stream_poll_interval)

        if last_text and len(last_text.strip()) >= 150 and last_text != prior_text:
            return last_text.strip()


        logger.error("NotebookLM failed to produce a complete critique within timeout.")
        raise RuntimeError("NotebookLM critique extraction failed: no complete evaluation response received from NotebookLM.")





