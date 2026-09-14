# Authenticated page widgets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `page` widget type whose HTML is fetched authenticated and rendered sandboxed, and whose own `fetch` calls are proxied through the client so they carry the user's credentials.

**Architecture:** Reuses the existing authenticated fetch path and `srcdoc` sandbox that `type: html` already uses. The new part is a prelude injected ahead of the backend's markup that replaces `window.fetch` with a `postMessage` round-trip, and a parent-side handler that performs the real call with `authHeaders` after verifying the caller and the target.

**Tech Stack:** React + TypeScript + Vitest (bdobb-v2); Python/FastAPI (openbb-docker).

**Spec:** `docs/superpowers/specs/2026-08-27-authenticated-page-widget-design.md`

**Status: DEFERRED — do not execute.** This is not 3.0.0 work. It lands with
the live-grid/live-chart episode. Until then `live-grid/widgets.json` keeps
`type: iframe`, and the standing consequence holds: enabling
`OPENBB_API_AUTH` disables the subscriptions widget. Task 3 is the change that
lifts that, and it does not happen today.

**Why the type is general, not a subscriptions fix.** The page widget is the
intended route for bringing other services into the front end -- portfolio
management, optimization -- each serving its own interactive page from its own
backend. Two design points exist for that future, not for subscriptions:
D3 (a page reaches only its OWN backend) is what keeps an optimization page
from issuing credentialed calls against the portfolio backend, and D4 (a
distinct `page` type) is what keeps the proxy from being granted wholesale to
every existing `html` widget. Neither is optional, and neither should be
simplified away on the grounds that today there is only one page.

## Global Constraints

- **This plan spans two repositories.** Tasks 1-2 are in `~/Developer/bdobb-v2`; Task 3 is in `~/Developer/openbb-docker`. Task 3 must land only after Tasks 1-2 ship in a client build, or the widget breaks in the other direction.
- `bdobb-v2` is currently on branch `document-viewers` with a clean tree. **Create a new branch from it; do not commit onto `document-viewers`.**
- **The sandbox string is unchanged and `allow-same-origin` must stay absent.** `FRAME_SANDBOX = "allow-scripts allow-forms allow-popups"` (`src/components/renderers/HtmlRenderer.tsx:7`). With `srcdoc`, granting same-origin puts the frame in the app's own origin and defeats the isolation this whole design depends on (spec D5).
- **Identify the caller by `event.source`, never `event.origin`** (spec D2). An opaque-origin frame reports `event.origin` as the literal string `"null"`, and so does every other one on the page — origin cannot distinguish callers and must not be used for trust.
- **Proxied requests must resolve to the widget's own backend** (spec D3). Compare the resolved URL's origin against `backend.baseUrl`'s, and refuse anything else. Without it, a page could make the client issue credentialed requests to any other configured backend.
- Do **not** change `type: html` behaviour, and do not give it the proxy (spec D4).
- The credential is never sent into the frame. Only response bodies cross back.
- Run the client suite with `npm run test:run` from `~/Developer/bdobb-v2`.

## File structure

| repo | file | responsibility |
|---|---|---|
| bdobb-v2 | `src/lib/pageProxy.ts` (new) | the prelude string and the request-resolution rules — pure, no React |
| bdobb-v2 | `src/lib/pageProxy.test.ts` (new) | its unit tests |
| bdobb-v2 | `src/components/renderers/PageRenderer.tsx` (new) | the frame, the message listener, the real fetch |
| bdobb-v2 | `src/components/renderers/PageRenderer.test.tsx` (new) | component tests |
| bdobb-v2 | `src/components/WidgetCard.tsx` | one `case "page"`, plus its `TYPE_LABELS` entry (line 31) |
| bdobb-v2 | `src/lib/builtins.ts` | read-only check: `fetchesData` must NOT exclude `page` |
| openbb-docker | `live-grid/widgets.json` | `"type": "page"` |

---

### Task 1: The proxy rules and prelude

Pure logic in its own module so the security decisions are unit-testable without
rendering anything. **Work in `~/Developer/bdobb-v2` on a new branch off
`document-viewers`.**

**Files:**
- Create: `src/lib/pageProxy.ts`
- Test: `src/lib/pageProxy.test.ts`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `PAGE_PRELUDE: string` — the `<script>` injected ahead of backend markup.
  - `resolveProxyTarget(baseUrl: string, path: string): URL | null` — the resolved URL, or `null` when it escapes the backend.
  - `type ProxyRequest = { __widget: "fetch"; id: number; path: string; init: { method: string; headers?: Record<string,string>; body?: string } }`
  - `isProxyRequest(data: unknown): data is ProxyRequest`

- [ ] **Step 1: Write the failing tests**

Create `src/lib/pageProxy.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { PAGE_PRELUDE, isProxyRequest, resolveProxyTarget } from "./pageProxy";

describe("resolveProxyTarget", () => {
  const base = "https://host.example:6903";

  it("resolves a root-relative path against the backend", () => {
    expect(resolveProxyTarget(base, "/api/subscriptions")?.toString())
      .toBe("https://host.example:6903/api/subscriptions");
  });

  it("keeps the query string", () => {
    expect(resolveProxyTarget(base, "/api/x?a=1&b=2")?.search).toBe("?a=1&b=2");
  });

  it("resolves a path with an encoded segment", () => {
    expect(resolveProxyTarget(base, "/api/subscriptions/BTC-USD")?.pathname)
      .toBe("/api/subscriptions/BTC-USD");
  });

  it("refuses an absolute URL to a different host", () => {
    // Without this a page could have the client call ANY backend the user has
    // configured, using that backend's credentials.
    expect(resolveProxyTarget(base, "https://evil.example/steal")).toBeNull();
  });

  it("refuses a different port on the same host", () => {
    expect(resolveProxyTarget(base, "https://host.example:10000/keys")).toBeNull();
  });

  it("refuses a protocol-relative URL", () => {
    expect(resolveProxyTarget(base, "//evil.example/steal")).toBeNull();
  });

  it("refuses a non-http scheme", () => {
    expect(resolveProxyTarget(base, "file:///etc/passwd")).toBeNull();
  });

  it("refuses a path that climbs out of a based path", () => {
    expect(resolveProxyTarget("https://host.example/grid/", "/../../etc")).toBeNull();
  });

  it("allows an absolute URL that is the backend itself", () => {
    expect(resolveProxyTarget(base, "https://host.example:6903/api/x")?.pathname)
      .toBe("/api/x");
  });

  it("returns null rather than throwing on an unparseable path", () => {
    expect(resolveProxyTarget(base, "http://[bad")).toBeNull();
  });
});

describe("isProxyRequest", () => {
  const good = { __widget: "fetch", id: 1, path: "/x", init: { method: "GET" } };

  it("accepts a well-formed request", () => {
    expect(isProxyRequest(good)).toBe(true);
  });

  it("rejects a message without the marker", () => {
    expect(isProxyRequest({ id: 1, path: "/x" })).toBe(false);
  });

  it("rejects null and non-objects", () => {
    expect(isProxyRequest(null)).toBe(false);
    expect(isProxyRequest("fetch")).toBe(false);
  });

  it("rejects a request whose path is not a string", () => {
    expect(isProxyRequest({ ...good, path: 42 })).toBe(false);
  });
});

describe("PAGE_PRELUDE", () => {
  it("overrides fetch so existing relative-fetch pages work unchanged", () => {
    expect(PAGE_PRELUDE).toContain("window.fetch");
  });

  it("posts to the parent, since the frame cannot call out itself", () => {
    expect(PAGE_PRELUDE).toContain("postMessage");
  });

  it("carries no credential -- only responses cross back into the frame", () => {
    expect(PAGE_PRELUDE.toLowerCase()).not.toContain("authorization");
  });
});
```

- [ ] **Step 2: Run them and verify they fail**

Run: `npm run test:run -- src/lib/pageProxy.test.ts`
Expected: FAIL — cannot resolve `./pageProxy`

- [ ] **Step 3: Implement the module**

Create `src/lib/pageProxy.ts`:

```ts
/**
 * The channel a sandboxed page widget uses to reach its backend.
 *
 * A `page` widget renders in a srcdoc frame whose sandbox deliberately omits
 * `allow-same-origin`, so its origin is opaque: a relative `fetch` has no
 * origin to resolve against and no credentials to send. The prelude below
 * replaces `window.fetch` with a postMessage round-trip, and the parent makes
 * the real call with the backend's auth headers.
 *
 * Existing pages work unchanged -- live-grid's subscriptions page already
 * calls fetch("/api/subscriptions") with relative paths.
 */

export type ProxyRequest = {
  __widget: "fetch";
  id: number;
  path: string;
  init: { method: string; headers?: Record<string, string>; body?: string };
};

export function isProxyRequest(data: unknown): data is ProxyRequest {
  if (typeof data !== "object" || data === null) return false;
  const m = data as Record<string, unknown>;
  return m.__widget === "fetch" && typeof m.id === "number" && typeof m.path === "string";
}

/**
 * Resolve a page's requested path against its own backend, or null.
 *
 * Null is a refusal, not an error: a page may only reach the backend it was
 * served from. Without this a page could have the client issue credentialed
 * requests to any other backend the user has configured -- using THAT
 * backend's credentials, which the page has no claim to.
 */
export function resolveProxyTarget(baseUrl: string, path: string): URL | null {
  let base: URL;
  let target: URL;
  try {
    base = new URL(baseUrl);
    target = new URL(path, base);
  } catch {
    return null;
  }
  if (target.protocol !== "http:" && target.protocol !== "https:") return null;
  // Origin covers scheme, host AND port, so a sibling service on another port
  // of the same host is refused too.
  if (target.origin !== base.origin) return null;
  // When the backend is mounted under a path, stay inside it.
  const prefix = base.pathname.endsWith("/") ? base.pathname : base.pathname + "/";
  if (prefix !== "/" && !target.pathname.startsWith(prefix)) return null;
  return target;
}

/**
 * Injected ahead of the backend's markup. Deliberately contains no credential:
 * only response bodies cross back into the frame.
 *
 * postMessage targets "*" because an opaque-origin frame cannot know its
 * parent's origin. That is safe here -- the message carries a path, never a
 * secret -- and the PARENT is what verifies the sender, by event.source.
 */
export const PAGE_PRELUDE = `<script>
(function () {
  var pending = new Map();
  var seq = 0;
  window.addEventListener("message", function (e) {
    var m = e.data;
    if (!m || m.__widget !== "fetch-reply") return;
    var p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.error) { p.reject(new TypeError(m.error)); return; }
    p.resolve(new Response(m.body, { status: m.status, headers: m.headers || {} }));
  });
  window.fetch = function (input, init) {
    var path = typeof input === "string" ? input : (input && input.url) || "";
    var id = ++seq;
    return new Promise(function (resolve, reject) {
      pending.set(id, { resolve: resolve, reject: reject });
      parent.postMessage({
        __widget: "fetch",
        id: id,
        path: path,
        init: {
          method: (init && init.method) || "GET",
          headers: (init && init.headers) || undefined,
          body: (init && init.body) || undefined
        }
      }, "*");
    });
  };
})();
</script>`;
```

- [ ] **Step 4: Run them and verify they pass**

Run: `npm run test:run -- src/lib/pageProxy.test.ts`
Expected: PASS (18 tests)

- [ ] **Step 5: Prove the refusals bite**

Temporarily delete the `if (target.origin !== base.origin) return null;` line and
re-run. Expected: the three refusal tests (`different host`, `different port`,
`protocol-relative`) FAIL. Restore it and confirm they pass. **Report both
results** — a security check whose test cannot detect its removal is not a test.

- [ ] **Step 6: Commit**

```bash
git add src/lib/pageProxy.ts src/lib/pageProxy.test.ts
git commit -m "feat: page-widget fetch proxy rules and prelude"
```

---

### Task 2: The renderer and its wiring

**Files:**
- Create: `src/components/renderers/PageRenderer.tsx`
- Create: `src/components/renderers/PageRenderer.test.tsx`
- Modify: `src/components/WidgetCard.tsx` (the `switch` on widget type, near `case "html"` around line 249)
- Read only: `src/lib/builtins.ts` (confirm `fetchesData` does not exclude `page`)

**Interfaces:**
- Consumes: `PAGE_PRELUDE`, `resolveProxyTarget`, `isProxyRequest` (Task 1); `authHeaders(backend)` and `buildWidgetUrl(backend, widget, params)` from `src/lib/dataClient.ts`.
- Produces: `PageRenderer({ data, backend })` rendering an iframe titled `"Widget page"`.

- [ ] **Step 1: Write the failing tests**

Create `src/components/renderers/PageRenderer.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PageRenderer } from "./PageRenderer";
import type { BackendConfig } from "../../lib/types";

const backend: BackendConfig = {
  id: "b1",
  name: "live-grid",
  baseUrl: "https://host.example:6903",
  authLocation: "header",
  headerName: "Authorization",
  headerValue: "Basic abc",
  validateWidgets: false,
} as BackendConfig;

describe("PageRenderer", () => {
  it("renders the backend HTML in a srcdoc frame", () => {
    render(<PageRenderer data={"<p>hi</p>"} backend={backend} />);
    expect(screen.getByTitle("Widget page").getAttribute("srcdoc")).toContain("<p>hi</p>");
  });

  it("injects the prelude ahead of the backend markup", () => {
    render(<PageRenderer data={"<p>hi</p>"} backend={backend} />);
    const doc = screen.getByTitle("Widget page").getAttribute("srcdoc") ?? "";
    expect(doc.indexOf("window.fetch")).toBeLessThan(doc.indexOf("<p>hi</p>"));
  });

  it("sandboxes without allow-same-origin", () => {
    render(<PageRenderer data={"<p>hi</p>"} backend={backend} />);
    const sandbox = screen.getByTitle("Widget page").getAttribute("sandbox") ?? "";
    expect(sandbox).not.toContain("allow-same-origin");
    expect(sandbox).toContain("allow-scripts");
  });

  it("never puts the credential in the frame", () => {
    render(<PageRenderer data={"<p>hi</p>"} backend={backend} />);
    const doc = screen.getByTitle("Widget page").getAttribute("srcdoc") ?? "";
    expect(doc).not.toContain("Basic abc");
  });

  it("says so when the payload is not a string", () => {
    render(<PageRenderer data={{ not: "html" }} backend={backend} />);
    expect(screen.getByText(/not a page/i)).toBeInTheDocument();
  });

  it("ignores a message from a window that is not its frame", async () => {
    // An opaque-origin frame reports event.origin as "null", and so does every
    // other one -- so identity must come from event.source. This message
    // carries a valid-looking request from the wrong window.
    const spy = vi.spyOn(globalThis, "fetch");
    render(<PageRenderer data={"<p>hi</p>"} backend={backend} />);
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { __widget: "fetch", id: 1, path: "/api/subscriptions", init: { method: "GET" } },
        source: window,
      }),
    );
    await new Promise((r) => setTimeout(r, 0));
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});
```

- [ ] **Step 2: Run them and verify they fail**

Run: `npm run test:run -- src/components/renderers/PageRenderer.test.tsx`
Expected: FAIL — cannot resolve `./PageRenderer`

- [ ] **Step 3: Implement the renderer**

Create `src/components/renderers/PageRenderer.tsx`:

```tsx
import { useEffect, useRef } from "react";
import { authHeaders } from "../../lib/dataClient";
import { logError } from "../../lib/logger";
import { PAGE_PRELUDE, isProxyRequest, resolveProxyTarget } from "../../lib/pageProxy";
import type { BackendConfig } from "../../lib/types";
import { FRAME_SANDBOX } from "./HtmlRenderer";

/**
 * A backend-authored interactive page, fetched authenticated and run sandboxed.
 *
 * Unlike an iframe widget -- whose `src` is a plain navigation carrying no
 * headers -- a page widget's HTML arrives over the normal authenticated data
 * path. The page's OWN calls are proxied through here so they carry the same
 * credentials, which is the whole reason this type exists.
 */
export function PageRenderer({ data, backend }: { data: unknown; backend: BackendConfig }) {
  const frame = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    async function onMessage(event: MessageEvent) {
      // Identity comes from the window, NOT event.origin: an opaque-origin
      // frame reports origin as the string "null", and so does every other
      // one on the page, so origin cannot tell callers apart.
      if (event.source !== frame.current?.contentWindow) return;
      if (!isProxyRequest(event.data)) return;

      const { id, path, init } = event.data;
      const reply = (payload: Record<string, unknown>) =>
        frame.current?.contentWindow?.postMessage(
          { __widget: "fetch-reply", id, ...payload },
          "*",
        );

      const target = resolveProxyTarget(backend.baseUrl, path);
      if (!target) {
        reply({ error: `refused: ${path} is outside this widget's backend` });
        return;
      }

      try {
        const res = await fetch(target.toString(), {
          method: init.method,
          headers: { ...(init.headers ?? {}), ...authHeaders(backend) },
          body: init.body,
        });
        reply({
          status: res.status,
          headers: { "content-type": res.headers.get("content-type") ?? "" },
          body: await res.text(),
        });
      } catch (err) {
        await logError("page widget proxy failed for %s: %s", path, err);
        reply({ error: String(err) });
      }
    }

    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [backend]);

  if (typeof data !== "string") {
    return <p className="empty-state">This payload is not a page.</p>;
  }

  return (
    <iframe
      ref={frame}
      className="widget-frame"
      title="Widget page"
      sandbox={FRAME_SANDBOX}
      srcDoc={PAGE_PRELUDE + data}
    />
  );
}
```

- [ ] **Step 4: Wire it into WidgetCard**

In `src/components/WidgetCard.tsx`, add beside `case "html"` (around line 249):

```tsx
      case "page":
        return <PageRenderer data={data} backend={backend} />;
```

and import it: `import { PageRenderer } from "./renderers/PageRenderer";`

Add to `TYPE_LABELS` in the same file (`src/components/WidgetCard.tsx:31`):

```ts
  page: "Page",
```

Do NOT add `page` to `fetchesData`'s exclusion list in `src/lib/builtins.ts` —
a page widget MUST fetch, which is the whole point. Confirm by reading that
function that `page` is not excluded.

- [ ] **Step 5: Run the renderer tests**

Run: `npm run test:run -- src/components/renderers/PageRenderer.test.tsx`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the whole client suite**

Run: `npm run test:run`
Expected: all pass. `type: html` and `type: iframe` behaviour must be unchanged —
if any of their tests fail, the switch statement was edited wrongly.

- [ ] **Step 7: Commit**

```bash
git add src/components/renderers/PageRenderer.tsx src/components/renderers/PageRenderer.test.tsx src/components/WidgetCard.tsx
git commit -m "feat: page widget type with an authenticated fetch proxy"
```

---

### Task 3: Switch the subscriptions widget

**Only after Tasks 1-2 ship in a client build the user is running.** A `page`
widget rendered by a client that does not know the type falls through to the
table renderer and shows nothing useful.

**Work in `~/Developer/openbb-docker`** — a different repository, and a shared
checkout other sessions use. Use `git worktree add`, do not switch its branch.

**Files:**
- Modify: `live-grid/widgets.json`
- Modify: `live-grid/README.md`
- Test: `live-grid/tests/test_subscriptions.py`

**Interfaces:**
- Consumes: the `page` type from Tasks 1-2.
- Produces: nothing later depends on.

- [ ] **Step 1: Write the failing test**

In `live-grid/tests/test_subscriptions.py`, replace these three existing
tests, which describe iframe behaviour that no longer applies:

- `test_the_widget_is_declared_as_an_iframe_pointing_at_an_absolute_url` (line 167)
- `test_a_trailing_slash_on_the_public_url_does_not_double_up` (line 177)
- `test_the_widget_is_omitted_when_no_public_url_is_configured` (line 184)

with:

```python
def test_the_widget_is_declared_as_a_page(tmp_path, monkeypatch):
    """`page`, not `iframe`: an iframe is a plain navigation carrying no
    headers, so with auth enabled its initial load 401s. A page widget's HTML
    is fetched over the authenticated path instead."""
    widgets = _client(tmp_path, monkeypatch).get("/widgets.json").json()
    w = widgets["subscriptions"]
    assert w["type"] == "page"
    assert w["endpoint"] == "subscriptions"


def test_the_page_widget_endpoint_is_relative(tmp_path, monkeypatch):
    """A page widget is FETCHED, so its endpoint is joined to the backend base
    URL like any other widget -- unlike an iframe, whose endpoint had to be an
    absolute URL because it was navigated to."""
    widgets = _client(tmp_path, monkeypatch).get("/widgets.json").json()
    assert widgets["subscriptions"]["endpoint"] == "subscriptions"
```

- [ ] **Step 2: Run them and verify the first fails**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q -k "declared_as_a_page or endpoint_is_relative"`
Expected: `test_the_widget_is_declared_as_a_page` FAILS with
`assert 'iframe' == 'page'`.

- [ ] **Step 3: Change the declaration**

In `live-grid/widgets.json`, change the `subscriptions` entry's `"type"` from
`"iframe"` to `"page"`.

- [ ] **Step 4: Remove the absolute-URL rewrite for this widget**

`app/main.py`'s `/widgets.json` handler rewrites `subscriptions.endpoint` to an
absolute URL from `LIVE_GRID_PUBLIC_URL`, and omits the widget when that is
unset. Both existed only because an iframe endpoint must be absolute. A page
widget is fetched and joined to the backend base URL, so remove that rewrite and
the omission for this widget.

**Check whether `LIVE_GRID_PUBLIC_URL` is used anywhere else before deleting the
variable itself** — the origin check on `/api/subscriptions` uses it. Leave the
variable and its other use intact; only the widgets.json rewrite goes.

Update the tests that asserted the absolute endpoint and the omit-when-unset
behaviour, which no longer describe the widget.

- [ ] **Step 5: Run the live-grid suite**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass.

- [ ] **Step 6: Update the README**

Replace the paragraph added on 2026-08-27 that says enabling `OPENBB_API_AUTH`
disables the subscriptions widget. It is no longer true. Say instead that the
widget is `type: page`, that its HTML and its API calls both travel the
authenticated path, and that it therefore works with auth enabled.

- [ ] **Step 7: Commit**

```bash
git add live-grid/widgets.json live-grid/app/main.py live-grid/tests/test_subscriptions.py live-grid/README.md
git commit -m "feat(live-grid): serve the subscriptions widget as an authenticated page"
```
