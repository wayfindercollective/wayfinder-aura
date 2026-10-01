# Licensing service: what the app expects

The Aura licensing service is its own Convex deployment
(`shiny-goshawk-432`, production). Wayfinder OS mints keys on it after a paid
order (`POST /admin/createLicense`, see Wayfinder-OS
`convex/commerce/auraLicensing.ts`). The desktop app on every platform talks to
it through `src/wayfinder/license.py`. This page is the app's half of the
contract, so the server can be changed without guessing.

One key works on Mac, Windows and Linux. The app sends only the key and a
per-computer id (`get_machine_id()`), and `maxActivations` (3 for store keys)
counts computers of any OS.

## `POST /activate`

Request: `{"key": "<KEY>", "machineId": "<id>"}`

Answer:

- `{"valid": true, "token": "<payload_b64>.<sig_b64>"}`. The token is Ed25519
  signed and verified offline with `LICENSE_PUBLIC_KEY_HEX`. Its payload must be
  `v` ≥ 2 with a `features` list, and its `machineId` must match the request.
- `{"valid": false, "reason": "<code>"}`. The codes the app explains are
  `not_found`, `activation_limit`, `revoked`, `refunded` and `missing_fields`.

## `POST /deactivate` (to be added on the server)

"Remove license" in the app (Settings → Ultra) calls this before it deletes
the local license, to free the computer's activation slot.

Request:

```json
{"key": "<KEY>", "machineId": "<id>", "token": "<this computer's signed token>"}
```

`token` is present whenever the computer has one. The server should accept the
request only if the token verifies with its signing key and names the same
`key` and `machineId`. That way someone who only knows the key can't release
other people's computers.

Answer (JSON):

| Answer | App treats it as |
| --- | --- |
| `{"released": true}`: the activation for this `machineId` is removed, so the key's count drops by one | freed |
| `{"released": false, "reason": "not_active"}`: this computer holds no activation | freed (already) |
| `{"released": false, "reason": "not_found" \| "missing_fields" \| "bad_token"}` | not freed |

The call must be idempotent: releasing twice gives `not_active` the second
time. A later `/activate` from the same computer simply takes a slot again.

Until the route exists, Convex answers `404` with a text body. The app reads
that as "unsupported", still removes the local license, and tells the user the
slot is still counted. So the app can ship before the server does.
