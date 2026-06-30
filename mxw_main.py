"""
MXWendler media plugin: render a live web page into a media surface.

Address a clip's media as:  web://https://mxwendler.net

The host (mxw_cachedmedia_plugin) calls:
    onOpen(uri)           -> (width, height, length, fps, has_alpha)
    onRenderFrame(frame)  -> H*W*4 uint8 numpy buffer, BGRA byte order
    onClose()             -> None
Per-instance state is keyed by the integer 'media_id', which the host sets on
this module before every call.

Rendering uses Playwright (offscreen Chromium). Install once with:
    pip install playwright numpy opencv-python
    playwright install chromium
"""

import os
import sys
import time
import json


# Decide where Chromium lives. MUST be set before playwright is imported
# (playwright reads PLAYWRIGHT_BROWSERS_PATH at import time).
#
# Two cases:
#   1) the plugin ships with a pre-staged "browsers" folder (e.g. populated at
#      build/install time) -> use it read-only. This works even under
#      C:\Program Files, where the folder is not writable at runtime.
#   2) nothing pre-staged -> use a per-user, writable location so the one-time
#      auto-download can succeed. The plugin folder itself is NOT writable under
#      Program Files, so we must not download into it.
def _user_browsers_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "MXWendler", "playwright-browsers")


def _has_browser(path):
    return bool(path) and os.path.isdir(path) and bool(os.listdir(path))


def _staged_browser_candidates():
    # places a browser may have been pre-staged. order = precedence.
    here = os.path.dirname(os.path.abspath(__file__))

    # a "browsers" folder shipped next to this plugin
    yield os.path.join(here, "browsers")

    # "<app>/python312/ms-playwright": our installer stages it there. python312
    # is a sibling of the "plugins" tree, so walk up from this file and probe.
    # we cannot rely on sys.prefix here: MXWendler's embedded interpreter does
    # not report sys.prefix as the python312 directory.
    probe = here
    for _ in range(6):
        probe = os.path.dirname(probe)
        if not probe:
            break
        yield os.path.join(probe, "python312", "ms-playwright")
        yield os.path.join(probe, "ms-playwright")

    # sys.prefix variants, in case a future build does set them
    yield os.path.join(sys.prefix, "ms-playwright")
    yield os.path.join(sys.base_prefix, "ms-playwright")


def _resolve_browsers_path():
    # honour an explicit override from the environment
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return env

    # a non-empty pre-staged folder means a browser was installed there already;
    # use it as-is (read-only is fine, e.g. under C:\Program Files).
    for candidate in _staged_browser_candidates():
        if _has_browser(candidate):
            return candidate

    # nothing pre-staged -> download into a writable per-user location
    return _user_browsers_dir()


os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _resolve_browsers_path()

import numpy as np
import cv2

import mxw_imgui  # host UI: draw controls in the clip panel (onRenderPanel)

# ----------------------------------------------------------------------------------
# per-instance storage, keyed by media_id (set by the host before each call)
class web_instance:
    def __init__(self):
        self.width = 1280
        self.height = 720
        self.fps = 30.0
        # the url shown / edited in the onRenderPanel() field; the loaded page url
        self.url = ""
        self.playwright = None
        self.browser = None
        self.page = None
        # cache the last decoded BGRA frame + when it was grabbed, so we do not
        # screenshot on every single render tick (screenshots are expensive)
        self.last_frame = None
        self.last_grab_ms = 0.0
        self.min_grab_interval_ms = 1000.0 / 10.0   # re-grab at most ~10x / second

storage = {}

# guard so the (slow, one-time) browser download is only attempted once per run
_install_attempted = False


def _install_chromium():
    # Run "playwright install chromium" in-process. We cannot shell out via
    # sys.executable here: under MXWendler's embedded interpreter that points at
    # MXW.exe, not python. playwright.__main__ drives the bundled node installer
    # directly, so importing and calling it works regardless of sys.executable.
    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["playwright", "install", "chromium"]
        import playwright.__main__ as pw_main
        try:
            pw_main.main()
        except SystemExit:
            pass   # the CLI calls sys.exit() on success
    finally:
        sys.argv = old_argv


def _launch_browser(inst):
    # launch chromium; if its binary was never downloaded, fetch it once and retry
    global _install_attempted
    try:
        return inst.playwright.chromium.launch(args=["--no-sandbox"])
    except Exception as e:
        msg = str(e)
        missing = "Executable doesn't exist" in msg or "playwright install" in msg
        if missing and not _install_attempted:
            _install_attempted = True
            _install_chromium()   # blocks while downloading (one time only)
            return inst.playwright.chromium.launch(args=["--no-sandbox"])
        raise


def _url_from_uri(uri):
    # "web://https://site" -> "https://site" ; "web://site" -> "https://site"
    rest = uri.split("://", 1)[1] if "://" in uri else uri
    if rest.startswith("http://") or rest.startswith("https://"):
        return rest
    return "https://" + rest


def _normalize_url(text):
    # a url typed into the panel: keep an explicit scheme, default to https otherwise
    text = text.strip()
    if not text:
        return text
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return "https://" + text


def _goto(inst, url):
    # navigate this instance's page to url and force a fresh screenshot next frame
    if inst.page is None or not url:
        return
    try:
        inst.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        inst.url = inst.page.url
        inst.last_frame = None
    except Exception:
        pass


# ----------------------------------------------------------------------------------
def onOpen(uri):
    inst = web_instance()
    storage[media_id] = inst

    url = _url_from_uri(uri)
    inst.url = url
    try:
        from playwright.sync_api import sync_playwright
        inst.playwright = sync_playwright().start()
        inst.browser = _launch_browser(inst)
        inst.page = inst.browser.new_page(
            viewport={"width": inst.width, "height": inst.height},
            device_scale_factor=1,
        )
        inst.page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        # surface the cause to the interpreter console via a raised exception;
        # the host catches it and reports onOpen failure.
        raise RuntimeError(
            "web_media: cannot open '%s': %s\n"
            "If the chromium browser is missing, run 'playwright install chromium' "
            "in the python environment MXWendler uses." % (url, e))

    # width, height, length(frames), fps, has_alpha
    return (inst.width, inst.height, 1, inst.fps, True)


def onRenderFrame(frame):
    inst = storage.get(media_id)
    if inst is None or inst.page is None:
        return _blank(1280, 720)

    now = time.monotonic() * 1000.0
    need_grab = inst.last_frame is None or (now - inst.last_grab_ms) >= inst.min_grab_interval_ms

    if need_grab:
        try:
            png = inst.page.screenshot(type="png")
            img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)  # BGR
            bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            # the stream texture wants exactly width*height*4 bytes in BGRA order
            if bgra.shape[1] != inst.width or bgra.shape[0] != inst.height:
                bgra = cv2.resize(bgra, (inst.width, inst.height))
            # the screenshot is top-down (row 0 = top), but the stream texture
            # treats row 0 as the bottom (GL origin is bottom-left) -> flip
            bgra = np.flipud(bgra)
            inst.last_frame = np.ascontiguousarray(bgra)
            inst.last_grab_ms = now
        except Exception:
            if inst.last_frame is None:
                inst.last_frame = _blank(inst.width, inst.height)

    return inst.last_frame


def onSave():
    # persist the *current* page url, so a page the user navigated to (rather than
    # the one in the uri) is restored. the host stores the returned string in the
    # project and hands it back to onLoad() on reload.
    inst = storage.get(media_id)
    if inst is None or inst.page is None:
        return None
    try:
        return json.dumps({"url": inst.page.url})
    except Exception:
        return None


def onLoad(state):
    # called once after onOpen() on project load, with the string from onSave().
    # navigate to the saved url if it differs from where onOpen() already went.
    inst = storage.get(media_id)
    if inst is None or inst.page is None or not state:
        return
    try:
        url = json.loads(state).get("url")
        if url and url != inst.page.url:
            inst.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            inst.last_frame = None   # force a fresh screenshot next frame
        if url:
            inst.url = url
    except Exception:
        pass


def onRenderPanel():
    # let the user type a url and load it. the host sets the module global 'media_id'
    # before the call, and we are mid-frame inside an active imgui context. the field
    # text is kept in inst.url; the "Go" button navigates the page.
    inst = storage.get(media_id)
    if inst is None:
        return
    mxw_imgui.set_next_item_width(300)
    # input_text returns (changed, new_value)
    changed, value = mxw_imgui.input_text("URL", inst.url, 1024)
    if changed:
        inst.url = value
    mxw_imgui.same_line()
    if mxw_imgui.button("Go"):
        _goto(inst, _normalize_url(inst.url))


def onSizeChange(w, h):
    # the host changed our render size: resize the browser viewport so screenshots
    # come back at the new resolution, and drop the cached frame.
    inst = storage.get(media_id)
    if inst is None:
        return
    inst.width = int(w)
    inst.height = int(h)
    try:
        if inst.page is not None:
            inst.page.set_viewport_size({"width": inst.width, "height": inst.height})
    except Exception:
        pass
    inst.last_frame = None


def onClose():
    inst = storage.pop(media_id, None)
    if inst is None:
        return
    try:
        if inst.browser is not None:
            inst.browser.close()
        if inst.playwright is not None:
            inst.playwright.stop()
    except Exception:
        pass


def _blank(w, h):
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[:, :, 3] = 255
    return img
