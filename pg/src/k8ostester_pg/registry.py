"""Best-effort container-registry tag listing, for the upgrade modal's version
picker. Uses the OCI/Docker registry v2 API with the standard bearer-token
challenge (works for ghcr.io, Docker Hub, quay, …). Any failure — no egress,
private repo, rate limit — returns an empty list, and the UI falls back to a
free-text tag entry. Stdlib only.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request


def _parse_ref(image: str) -> tuple[str, str]:
    """'ghcr.io/cloudnative-pg/postgresql:16.4' -> ('ghcr.io', 'cloudnative-pg/postgresql')."""
    ref = image.split("@", 1)[0]                       # drop any digest
    if ":" in ref.rsplit("/", 1)[-1]:                  # drop the tag
        ref = ref.rsplit(":", 1)[0]
    head = ref.split("/", 1)[0]
    if "." in head or ":" in head or head == "localhost":
        return head, ref.split("/", 1)[1]
    return "docker.io", ref                            # bare name -> Docker Hub


def _fetch(url: str, token: str | None = None) -> tuple[dict, object]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 (registry API)
        return json.load(r), r.headers


def _token_from_challenge(header: str) -> str | None:
    m = dict(re.findall(r'(\w+)="([^"]*)"', header))
    realm = m.get("realm")
    if not realm:
        return None
    query = {k: m[k] for k in ("service", "scope") if m.get(k)}
    url = realm + ("?" + urllib.parse.urlencode(query) if query else "")
    try:
        d, _ = _fetch(url)
        return d.get("token") or d.get("access_token")
    except Exception:
        return None


def list_tags(image: str, max_pages: int = 8) -> list[str]:
    """All tags for the image's repository, or [] on any failure. Follows the
    registry's Link pagination (large repos page the tag list) up to max_pages."""
    registry, repo = _parse_ref(image)
    host = "registry-1.docker.io" if registry in ("docker.io", "index.docker.io") else registry
    if host == "registry-1.docker.io" and "/" not in repo:
        repo = "library/" + repo                       # official images live under library/
    base = f"https://{host}"
    url: str | None = f"{base}/v2/{repo}/tags/list?n=500"
    token: str | None = None
    tags: list[str] = []
    pages = 0
    while url and pages < max_pages:
        pages += 1
        try:
            data, headers = _fetch(url, token)
        except urllib.error.HTTPError as e:
            if e.code == 401 and token is None:        # authenticate, then retry the page
                token = _token_from_challenge(e.headers.get("Www-Authenticate", ""))
                if token:
                    pages -= 1
                    continue
            break
        except Exception:
            break
        tags += data.get("tags") or []
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", "") or "")
        url = (nxt.group(1) if nxt.group(1).startswith("http") else base + nxt.group(1)) if nxt else None
    return tags


_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.manifest.v1+json"
)


def image_exists(image: str) -> bool:
    """Can this exact image ref be pulled? HEADs its manifest in the registry.
    Returns False on 404 / any error (so an unreachable registry reads as unknown)."""
    registry, repo = _parse_ref(image)
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]
    tag = last.rsplit(":", 1)[1] if ":" in last else "latest"
    host = "registry-1.docker.io" if registry in ("docker.io", "index.docker.io") else registry
    if host == "registry-1.docker.io" and "/" not in repo:
        repo = "library/" + repo
    url = f"https://{host}/v2/{repo}/manifests/{tag}"

    def _head(token: str | None = None) -> bool:
        req = urllib.request.Request(url, method="HEAD", headers={"Accept": _MANIFEST_ACCEPT})
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 (registry API)
            return 200 <= r.status < 300

    try:
        return _head()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            token = _token_from_challenge(e.headers.get("Www-Authenticate", ""))
            if token:
                try:
                    return _head(token)
                except Exception:
                    return False
        return False                                   # 404 -> not found
    except Exception:
        return False


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", tag)[:4])


# clean release tags only (16.4, 16.6, 17.2) — no beta/rc/build-suffix noise
_RELEASE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def upgrade_tags(image: str, limit: int = 40) -> list[str]:
    """Clean release tags, newest first — the version picker's options."""
    tags = {t for t in list_tags(image) if _RELEASE.match(t)}
    return sorted(tags, key=_version_key, reverse=True)[:limit]
