#!/usr/bin/env python3
"""Microsoft Graph mail access.

Delegated device-code flow: you sign in once, the refresh token lives on the
data volume and rolls forward on use. No admin consent, no client secret, and
no ApplicationAccessPolicy to maintain in Exchange PowerShell.

The mail backend is deliberately thin and behind one class, so swapping to
plain IMAP against his own server later touches this file and nothing else.
"""

import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("jobtrack.mail")

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access Mail.Read"
UA = "jobtrack/1.0"


class MailError(Exception):
    """Anything the caller can act on - missing config, expired auth."""


def token_path():
    return os.environ.get("GRAPH_TOKEN_FILE", "/data/graph_token.json")


def _cfg():
    cid = os.environ.get("GRAPH_CLIENT_ID")
    tenant = os.environ.get("GRAPH_TENANT_ID")
    if not cid or not tenant:
        raise MailError("GRAPH_CLIENT_ID and GRAPH_TENANT_ID must be set")
    return cid, tenant


async def start_device_code(session):
    """Kick off sign-in. Returns the dict holding user_code and the URL."""
    cid, tenant = _cfg()
    url = ("https://login.microsoftonline.com/%s/oauth2/v2.0/devicecode" % tenant)
    async with session.post(url, data={"client_id": cid, "scope": SCOPES},
                            headers={"User-Agent": UA}) as r:
        body = json.loads(await r.text())
    if "device_code" not in body:
        raise MailError("device code request failed: %s" % str(body)[:200])
    return body


async def poll_device_code(session, device_code, interval=5, timeout=900):
    """Wait for the sign-in to complete, then persist the tokens."""
    cid, tenant = _cfg()
    url = "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % tenant
    deadline = time.time() + timeout
    while time.time() < deadline:
        async with session.post(url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": cid, "device_code": device_code},
                headers={"User-Agent": UA}) as r:
            body = json.loads(await r.text())
        if "access_token" in body:
            _save(body)
            return body
        err = body.get("error")
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        raise MailError("device code failed: %s" % (body.get("error_description") or err))
    raise MailError("device code timed out")


def _save(tok):
    tok = dict(tok)
    tok["obtained_at"] = int(time.time())
    path = token_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(tok, f)
    os.chmod(path, 0o600)


def _load():
    try:
        with open(token_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


async def access_token(session):
    """A valid access token, refreshing when it is close to expiry."""
    tok = _load()
    if not tok:
        raise MailError("not signed in - run the device code flow first")
    age = int(time.time()) - int(tok.get("obtained_at", 0))
    if age < int(tok.get("expires_in", 3600)) - 300:
        return tok["access_token"]

    cid, tenant = _cfg()
    url = "https://login.microsoftonline.com/%s/oauth2/v2.0/token" % tenant
    async with session.post(url, data={
            "grant_type": "refresh_token", "client_id": cid,
            "refresh_token": tok.get("refresh_token"), "scope": SCOPES},
            headers={"User-Agent": UA}) as r:
        body = json.loads(await r.text())
    if "access_token" not in body:
        raise MailError("refresh failed, sign in again: %s"
                        % (body.get("error_description") or body.get("error")))
    # Microsoft does not always return a new refresh token; keep the old one.
    body.setdefault("refresh_token", tok.get("refresh_token"))
    _save(body)
    return body["access_token"]


def is_correspondence(m):
    """Whether a Graph message is something an employer actually sent.

    Belt and braces alongside scoping the fetch to the inbox. A draft is not
    correspondence whatever folder it turns up in, and a message with no
    sender is a draft that has not been sent yet - which is exactly how six
    autosaves of one reply became six replies from the employer.
    """
    if m.get("isDraft"):
        return False
    frm = ((m.get("from") or {}).get("emailAddress") or {})
    return bool((frm.get("address") or "").strip())


async def fetch_messages(session, since_iso=None, top=50, folder="inbox"):
    """Recent messages, newest first. `since_iso` is an ISO8601 UTC timestamp.

    Defaults to the inbox. /me/messages spans every folder including Drafts
    and Sent Items, and Outlook writes a fresh message id on every autosave -
    so composing one reply to an employer produced six "replies" from them in
    the room, each a different id the seen-key could not collapse,
    each with an empty from address because a draft has no sender yet.
    """
    tok = await access_token(session)
    base = "%s/me/mailFolders/%s/messages" % (GRAPH, folder) if folder \
        else "%s/me/messages" % GRAPH
    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,webLink,"
                   "isRead,isDraft",
    }
    if since_iso:
        params["$filter"] = "receivedDateTime ge %s" % since_iso
    headers = {"Authorization": "Bearer %s" % tok, "User-Agent": UA,
               "Accept": "application/json"}
    async with session.get(base, params=params, headers=headers) as r:
        if r.status != 200:
            raise MailError("graph %s: %s" % (r.status, (await r.text())[:180]))
        data = json.loads(await r.text())

    out = []
    for m in data.get("value") or []:
        if not is_correspondence(m):
            log.debug("skipping %s", (m.get("subject") or "")[:60])
            continue
        frm = ((m.get("from") or {}).get("emailAddress") or {})
        out.append({
            "id": m.get("id"),
            "subject": (m.get("subject") or "").strip(),
            "from_name": (frm.get("name") or "").strip(),
            "from_addr": (frm.get("address") or "").lower().strip(),
            "received": m.get("receivedDateTime"),
            "preview": (m.get("bodyPreview") or "").strip(),
            "link": m.get("webLink"),
            "is_read": m.get("isRead"),
        })
    return out
