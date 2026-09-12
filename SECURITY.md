# Security Policy

## What this project touches

Hindsight Dashboard is a **read-mostly front end for a memory database**. It can display every memory you have stored, and a few of its buttons spend LLM tokens. Treat the panel itself as sensitive: whoever can open it can read your memory bank.

## Default posture

| Control | Default |
|---|---|
| Bind address | `127.0.0.1` — unreachable from the network |
| Remote access | requires an access key (`?k=<key>` → HttpOnly cookie) |
| Local access | password-free (loopback is trusted) |
| Remote writes (`retry` / `reflect` / `cancel`) | rejected with `403` unless `--allow-remote-write` |
| Dependencies | none — Python standard library only |

## Things you should know before exposing it

1. **Hindsight itself has no authentication.** If the Hindsight API port is reachable from your network, anyone can read or delete every memory regardless of what this dashboard does. Bind it to loopback (`HINDSIGHT_API_HOST=127.0.0.1`) and let the dashboard proxy locally.
2. **The access key travels in cleartext over plain HTTP.** On a shared network (campus Wi-Fi, hotel, office) someone on the same segment may capture it. If that matters, bind both services to a VPN interface (e.g. Tailscale) or terminate TLS in front of them.
3. **The key is a bearer secret.** Anyone holding it has full access. Keep it in a file with `600` permissions, don't paste it into tickets, chats or screenshots.
4. **Rotating the key** means: replace the value in the key file (and the Control Plane's `HINDSIGHT_CP_ACCESS_KEY` if you use it), restart the services, then confirm the old key is rejected.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository (Security → Report a vulnerability), or contact the maintainer through their GitHub profile.

Include: affected version/commit, steps to reproduce, and the impact you believe it has. Expect an initial response within a few days.

## Out of scope

- Anything that requires the attacker to already hold the access key or shell access to the host.
- Misconfiguration that ignores the defaults documented above (e.g. binding `0.0.0.0` with no key on an untrusted network).
- Vulnerabilities in Hindsight itself — report those upstream to [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight).
