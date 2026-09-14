# Authenticated page widgets — design

Let a backend serve an interactive page that runs sandboxed in the client and
can call its own API with the user's credentials, so an interactive widget no
longer has to choose between being authenticated and being interactive.

## Why

The subscriptions widget added on 2026-08-27 is declared `type: "iframe"`. The
client renders an iframe widget as `<iframe src>` — a plain browser navigation
with no headers — and a backend's configured `Authorization` header is attached
only on the fetch path, which iframe widgets never take
(`IframeRenderer.tsx:88`, `lib/dataClient.ts`).

That was fine while live-grid had no authentication. Adding Basic auth to
live-grid breaks it: the iframe's initial `GET /subscriptions` receives a 401
the client cannot satisfy. So today the stack can have an authenticated
live-grid or a working subscriptions widget, not both.

The underlying gap is general, not specific to this widget: **there is no way
for a backend to ship an interactive page that is authenticated.** `type: html`
is authenticated but its page cannot call anything; `type: iframe` can call its
origin but is unauthenticated.

## What already exists

Most of the mechanism is present and does not need designing.

`type: "html"` fetches its payload through the normal authenticated data path
and renders it with `<iframe srcDoc={data} sandbox={FRAME_SANDBOX}>`
(`HtmlRenderer.tsx`). `FRAME_SANDBOX` is `"allow-scripts allow-forms
allow-popups"` — `allow-same-origin` is deliberately absent, and its docstring
says why: with `srcdoc`, granting same-origin would place the frame in the app's
own origin and defeat the sandbox.

So switching the widget from `iframe` to `html` fixes the initial load today.
What it does not fix, and what this design adds, is the page's own API calls.

## The problem this design solves

In a `srcdoc` frame the origin is opaque. `fetch("/api/subscriptions")` has no
origin to resolve a relative path against and no credentials to send. An
interactive page therefore renders but cannot function.

## Decisions

**D1. The client overrides `fetch` inside the frame; the parent performs the
real call.**
A small prelude is injected into the `srcdoc` ahead of the backend's markup. It
replaces `window.fetch` with an implementation that posts the request to the
parent, awaits a reply, and reconstructs a `Response`. The parent performs the
call with `buildWidgetUrl` and `authHeaders` from `lib/dataClient`, exactly as
every other authenticated widget request is made.

Existing pages work unchanged. `subscriptions.html` already calls
`fetch("/api/subscriptions")` with relative paths and needs no edit.

Rejected: an explicit `window.widgetFetch` the page must call. More honest about
what is happening, but it makes every interactive page client-specific, and the
pages this serves are meant to be plain backend-authored HTML.

**D2. The parent identifies the caller by `event.source`, never by
`event.origin`.**
A frame with an opaque origin reports `event.origin` as the string `"null"`.
Any other opaque-origin frame on the page reports the same value, so origin
strings cannot distinguish callers and must not be used for trust. The handler
compares `event.source` against the specific iframe's `contentWindow` and
ignores anything else.

**D3. Proxied requests are restricted to the widget's own backend.**
The parent resolves the requested path against that widget's backend base URL
and refuses anything that resolves elsewhere — a different backend, or an
external host. Without this, a page could have the client make credentialed
requests to any backend the user has configured, using that backend's
credentials.

The page may still call any path on *its own* backend. That is not a new
capability: an `iframe` widget could already do so from its own origin. What
changes is that those calls now carry the user's credentials — which is the
point, and is bounded to a backend the user chose to trust.

**D4. It is a new type, `page`, not a change to `html`.**
`type: html` means "render this payload". A `page` additionally gets the fetch
proxy and a live parent connection. Overloading `html` would silently grant the
proxy to every existing HTML widget, including ones written when no such channel
existed.

**D5. The sandbox is unchanged: `allow-scripts allow-forms allow-popups`.**
`allow-same-origin` stays absent. The proxy exists precisely so the frame does
not need same-origin to be useful, and adding it would defeat the isolation the
design depends on.

## Data flow

    widgets.json:  { "type": "page", "endpoint": "subscriptions" }

    1. client GETs {backend}/subscriptions with authHeaders   -> HTML string
    2. client renders <iframe sandbox srcDoc={prelude + html}>
    3. page calls fetch("/api/subscriptions")
    4. prelude posts {id, path, init} to parent
    5. parent verifies event.source === this frame's contentWindow
    6. parent resolves path against backend.baseUrl, refuses if it escapes
    7. parent fetches with authHeaders, posts {id, status, headers, body} back
    8. prelude resolves the page's Promise with a reconstructed Response

## Security model

The frame is opaque-origin and cannot read the parent's DOM, cookies, or
storage. The proxy is the only channel out, and it is narrow by construction:

| threat | control |
|---|---|
| page reads app state | opaque origin; no `allow-same-origin` |
| page calls another backend with its credentials | D3 path restriction |
| unrelated frame spoofs a proxy request | D2 `event.source` identity check |
| page exfiltrates the credential itself | the credential is never sent into the frame; only responses are |
| page calls its own backend as the user | permitted by design, bounded to a backend the user configured |

The honest residual: a compromised backend can make the client issue
credentialed requests to that same backend. It could already act on its own API
directly, so this grants no reach it lacked — but it does mean a malicious page
acts *as the user* rather than as itself, which matters if the backend
distinguishes them. Backends in this stack do not.

## Backend requirements

A `page` widget's endpoint must return HTML on a normal authenticated GET.
`live-grid`'s `/subscriptions` already does. No backend change is needed beyond
declaring `"type": "page"` instead of `"iframe"`.

Note this removes the absolute-URL requirement that `type: iframe` imposes: a
page widget's endpoint is fetched, not navigated to, so it is joined to the
backend's base URL like any other widget. `LIVE_GRID_PUBLIC_URL` becomes
unnecessary for this widget, though it stays for anything still framed.

## Testing

- an unauthenticated backend still works (no header configured, page loads)
- the page's `fetch` reaches the backend with the auth header attached
- a `postMessage` from a different window is ignored (D2)
- a path resolving to another backend is refused (D3)
- a path resolving to an external host is refused (D3)
- relative, absolute-path, and query-bearing paths all resolve correctly
- a non-200 response reaches the page as a `Response` with the right status, so
  existing error handling in the page keeps working
- `type: html` widgets do NOT receive the proxy (D4)
- the subscriptions widget specifically: loads, lists, adds, removes, and shows
  the cap — with auth enabled on live-grid

## Out of scope

Streaming or websockets from inside a page — the proxy is request/response only,
so live-grid's websocket widgets stay `type: live_grid`. Changing `type: html`.
Any change to how non-page widgets authenticate.

## Consequences

Once this exists, `OPENBB_API_AUTH` can be enabled on live-grid without
disabling the subscriptions widget — which is the immediate blocker it removes.

More broadly it gives the stack a way to ship an interactive, authenticated
widget without front-end work per widget, which is what `type: iframe` was
chosen for originally and could not deliver once auth was required.
