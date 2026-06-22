# mxw-plugin-web-media

An MXWendler StageDesigner **media plugin** that renders a live web page into a
media surface, using [Playwright](https://playwright.dev/python/) (offscreen
Chromium).

It is the web counterpart to the OpenGL-cube media plugin and serves as a worked
example of the MXWendler Python media-plugin API.

## Usage

In MXWendler, create the media with the URI:

```
web://https://mxwendler.net
```

(or pick **Web Page** from the media create dropdown). A bare host such as
`web://example.com` is treated as `https://example.com`.

## How it works

MXWendler's media-plugin host calls three entry points in `mxw_main.py`:

| Callback | Returns | Purpose |
|----------|---------|---------|
| `onOpen(uri)` | `(width, height, length, fps, has_alpha)` | report the surface format |
| `onRenderFrame(frame)` | `H*W*4` uint8 buffer, **BGRA** byte order | produce one frame |
| `onClose()` | – | release resources |
| `onSave()` | state `str` | serialize per-instance state (persisted in project) |
| `onLoad(state)` | – | restore state from a previous `onSave()` |
| `onSizeChange(w, h)` | – | host changed the render size; resize the viewport |

Per-instance state is keyed by the integer `media_id`, which the host sets on the
module before each call.

## Requirements

```
pip install playwright numpy opencv-python
playwright install chromium
```

The plugin looks for the Chromium browser in this order: the
`PLAYWRIGHT_BROWSERS_PATH` environment variable, a `browsers/` folder next to the
plugin, an `ms-playwright/` folder in the host's `python312` directory, and
finally a per-user writable location (where it will download the browser once on
first use).

## License

MIT
