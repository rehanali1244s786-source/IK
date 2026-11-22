# Security

## Reporting a problem
Please open a private security advisory on GitHub (Security tab > Report a vulnerability) instead of a public issue.

## How the browser link is protected
| Threat | Defence |
|---|---|
| A website calls the app from the browser | Loopback only; custom `X-IK-Client` header forces a CORS preflight that is never answered; `Origin` must be an extension; `Host` must be `127.0.0.1`/`localhost` (blocks DNS rebinding) |
| Another program on the PC or another extension uses the app | Requests need a random token that is only issued after you approve the extension in a dialog; the token is bound to that extension's origin; only its SHA-256 is stored; *Disconnect all browsers* revokes it |
| Another program grabs the port | Exclusive port binding on Windows (no `SO_REUSEADDR`) |
| A web page embeds the extension's panel and drives it | The panel only works with a one-time nonce registered by the extension's own content script for that tab |
| A link or playlist points at `file://`, `ftp://` or your router | Only http(s); public-to-private-network hops are refused for redirects and HLS segments |
| A site steals your login cookie via redirects or playlist hosts | Cookies/Authorization are only sent to the original site (and its sub-domains) |
| Stolen `downloads.json` | Cookies are DPAPI-encrypted (Windows) or not stored |
| Browser asks to save into Startup / system folders | Those folders are refused |
| Truncated or malicious servers | Range replies are validated; writes never pass a segment's end; size is checked before the file is finalised |

## Known limits
* Any program running as **you** can read your own files and could also ask for pairing - the dialog is what protects you; never approve an extension you did not install.
* DRM-protected streams are not supported and are not bypassed.
* A malicious page could still make *you* download something; the app always shows a confirmation box for captured downloads.
* The extension needs access to all sites (to read cookies, detect video requests and add the video bar). It talks only to `127.0.0.1`.

## Releases
Release binaries are built by GitHub Actions from the tagged source. `SHA256SUMS.txt` is attached to every release. To enable Authenticode signing, add the repository secrets `WINDOWS_CERT_PFX_BASE64` (base64 of your .pfx) and `WINDOWS_CERT_PASSWORD`; the workflow signs both the exe and the installer when they exist. Deno (needed for YouTube) is pinned in `tools/deno-version.txt` and verified against its published SHA-256 at build time.
