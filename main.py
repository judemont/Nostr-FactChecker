import logging
import datetime
import uuid
import sys
import os
from typing import Dict, List, Optional

from cachetools import TTLCache
from tornado import gen
from tornado.queues import Queue

from pynostr.event import EventKind, Event
from pynostr.filters import Filters, FiltersList
from pynostr.message_type import RelayMessageType
from pynostr.relay_list import RelayList
from pynostr.relay_manager import RelayManager

from factchecker import FactChecker
from pynostr.key import PublicKey 
from pynostr.bech32 import bech32_encode


# ============================================================
# LOGGING CONFIGURATION
# ============================================================
logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stdout,
    format="[%(asctime)s - %(levelname)s] %(message)s"
)
log = logging.getLogger("NostrFactCheckerBot")
log.setLevel(logging.INFO)


def short_id(hex_id: Optional[str], length: int = 8) -> str:
    """Truncate a hex ID for readable logs."""
    return hex_id[:length] if hex_id else "???"


# ============================================================
# ENVIRONMENT & CONSTANTS
# ============================================================

FACTCHECKER_PRIVATE_KEY = os.environ.get("FACTCHECKER_PRIVATE_KEY")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")

if FACTCHECKER_PRIVATE_KEY is None:
    raise ValueError("FACTCHECKER_PRIVATE_KEY environment variable not set")

if MISTRAL_API_KEY is None:
    raise ValueError("MISTRAL_API_KEY environment variable not set")

FACTCHECKER_AGENT_ID = "ag_019b704bddcc72079c3a26f9cb4891fa"

FACTCHECKER_NPUB = "npub1gy63uvtxu7mdmhwyczk53e5n28krg5p8wx3pdklq3w5udq7ylcwqvrwygj"
FACTCHECKER_PUBKEY = "41351e3166e7b6ddddc4c0ad48e69351ec34502771a216dbe08ba9c683c4fe1c"

RATE_LIMIT_DELAY = datetime.timedelta(milliseconds=5000)
FETCH_EVENT_TIMEOUT = 10.0

PUBLISH_VERIFY_TIMEOUT = 5.0
PUBLISH_MAX_RETRIES = 3
PUBLISH_INITIAL_BACKOFF = 2.0

RELAY_RECONNECT_INTERVAL = 30 * 60  # Reconnect to relays every 30 minutes


# ============================================================
# RELAYS
# ============================================================

RELAYS = [
    "wss://nos.lol",
    "wss://relay.damus.io",
    "wss://nostr.mom",
    "wss://relay.pleb.to",
    "wss://relay.primal.net",
    "wss://relay.nostr.band",
    "wss://relay.nostr.pub",
    "wss://nostr.rocks",
    "wss://relay.snort.social"
]


# ============================================================
# MUTED USERS
# ============================================================

MUTED_PUBKEYS = {
    "d52a3260bba32caf47a9f8e09b8be31a790bc00ba9668556f7d319d74dd4206c"
}


# ============================================================
# GLOBAL STATE
# ============================================================

event_dedup_cache = TTLCache(maxsize=1000, ttl=60)
pending_event_requests: Dict[str, Queue] = {}

last_sent_message_time = datetime.datetime.min

factchecker = FactChecker(
    api_key=MISTRAL_API_KEY,
    agent_id=FACTCHECKER_AGENT_ID
)

relay_manager: RelayManager


# ============================================================
# HELPERS
# ============================================================

def pubkey_to_npub(pubkey: str) -> str:
    return PublicKey(bytes.fromhex(pubkey)).bech32()

def extract_image_urls(content: str) -> List[str]:
    if not content:
        return []

    return [
        word for word in content.split()
        if word.startswith(("http://", "https://"))
        and word.lower().endswith((
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"
        ))
    ]


@gen.coroutine
def fetch_event_by_id(event_id: str, timeout: float = FETCH_EVENT_TIMEOUT):
    queue = Queue()
    pending_event_requests[event_id] = queue

    subscription_id = uuid.uuid4().hex
    filters = FiltersList([Filters(ids=[event_id])])

    relay_manager.add_subscription_on_all_relays(subscription_id, filters)

    try:
        event = yield gen.with_timeout(
            datetime.timedelta(seconds=timeout),
            queue.get()
        )
        return event
    except TimeoutError:
        log.warning(f"Timeout fetching event {short_id(event_id)} after {timeout}s")
        return None
    finally:
        pending_event_requests.pop(event_id, None)


@gen.coroutine
def publish_and_verify(event: Event, max_retries: int = PUBLISH_MAX_RETRIES):
    """Publish an event and verify it was received by at least one relay.
    Retries with exponential backoff if the event is not found."""
    backoff = PUBLISH_INITIAL_BACKOFF

    for attempt in range(1, max_retries + 1):
        log.info(f"  Publish attempt {attempt}/{max_retries} for {short_id(event.id)} (wait {backoff:.0f}s)")
        relay_manager.publish_event(event)

        # Give relays time to process before verifying
        yield gen.sleep(backoff)

        # Try to fetch the event back to confirm it landed
        verified_event = yield fetch_event_by_id(
            event.id, timeout=PUBLISH_VERIFY_TIMEOUT
        )

        if verified_event is not None:
            log.info(f"  Event {short_id(event.id)} verified (attempt {attempt}/{max_retries})")
            return True

        log.warning(f"  Event {short_id(event.id)} not found, will retry...")
        backoff = min(backoff * 2, 30.0)  # exponential backoff, cap at 30s

    log.error(f"  DELIVERY FAILED: {short_id(event.id)} not verified after {max_retries} attempts")
    return False


def should_handle_event(event: Event) -> bool:
    if event.pubkey in MUTED_PUBKEYS:
        log.debug(f"Muted user {short_id(event.pubkey)}, skipping")
        return False

    content = (event.content or "").lower()
    ptags = event.get_tag_list("p")

    mentioned_explicitly = "@factchecker" in content
    
    tagged_directly = any(
        ptag[0] in {FACTCHECKER_NPUB, FACTCHECKER_PUBKEY}
        and "nostr:" in content
        and event.pubkey != FACTCHECKER_PUBKEY
        for ptag in ptags
    )

    return mentioned_explicitly or tagged_directly


# ============================================================
# CORE MESSAGE HANDLER
# ============================================================

@gen.coroutine
def on_message(message_json, relay_url):
    global last_sent_message_time

    if message_json[0] == RelayMessageType.OK:
        event_id = message_json[1]
        accepted = message_json[2]
        reason = message_json[3] if len(message_json) > 3 else ""
        relay_name = relay_url.replace("wss://", "").rstrip("/")
        if accepted:
            log.info(f"  [OK] {relay_name} accepted {short_id(event_id)}")
        else:
            log.warning(f"  [REJECTED] {relay_name} rejected {short_id(event_id)}: {reason}")
        return

    if message_json[0] != RelayMessageType.EVENT:
        return

    event = Event.from_dict(message_json[2])

    if event.id in pending_event_requests:
        pending_event_requests[event.id].put(event)
        return

    if event.id in event_dedup_cache:
        return
    event_dedup_cache[event.id] = True

    if not should_handle_event(event):
        return

    requester_npub = pubkey_to_npub(event.pubkey or "")
    log.info(f"--- Fact-check request from {requester_npub} (event {short_id(event.id)})")

    while datetime.datetime.now() - last_sent_message_time < RATE_LIMIT_DELAY:
        yield gen.sleep(0.1)

    last_sent_message_time = datetime.datetime.now()

    reply_event: Optional[Event] = None
    etags = event.get_tag_list("e")
   
    reply_to_ids = [etag[0] for etag in etags if len(etag) >= 3 and etag[2] == "reply"]
    if len(reply_to_ids) == 0:
        reply_to_ids = [etag[0] for etag in etags if len(etag) >= 3 and etag[2] == "root"]

    is_reply = len(reply_to_ids) > 0
    reply_to_id = reply_to_ids[0] if is_reply else None
   
    try:
        if is_reply:
            target_event_id = reply_to_id
            log.info(f"  Fetching target event {short_id(target_event_id)}...")
            target_event = yield fetch_event_by_id(target_event_id)
            if not target_event:
                log.warning(f"  Target event {short_id(target_event_id)} not found, aborting")
                return

            if target_event.pubkey == FACTCHECKER_PUBKEY:
                log.info(f"  Target {short_id(target_event_id)} is our own event, skipping")
                return

            target_npub = pubkey_to_npub(target_event.pubkey or "")
            claim_text = target_event.content or ""
            image_urls = extract_image_urls(claim_text)
            for image_url in image_urls:
                claim_text = claim_text.replace(image_url, "")

            claim_preview = claim_text.strip()[:120].replace("\n", " ")
            log.info(
                f"  Claim by {target_npub}: \"{claim_preview}{'...' if len(claim_text.strip()) > 120 else ''}\""
            )
            if image_urls:
                log.info(f"  Images attached: {len(image_urls)}")

            log.info("  Running fact-check via Mistral...")
            fc_start = datetime.datetime.now()
            factcheck_result = factchecker.check_fact(
                claim_text,
                image_urls=image_urls
            )
            fc_duration = (datetime.datetime.now() - fc_start).total_seconds()
            log.info(f"  Fact-check completed in {fc_duration:.1f}s")

            tagger_npub = pubkey_to_npub(event.pubkey or "")
            reply_event = Event(f"{factcheck_result}\n\n\nnostr:{tagger_npub}")
            reply_event.tags.append(["e", str(target_event_id), "", "reply"])
            reply_event.tags.append(["p", str(event.pubkey), "mention"])
            reply_event.tags.append(["p", str(target_event.pubkey), "mention"])

            reply_event.sign(str(FACTCHECKER_PRIVATE_KEY))
            log.info(f"  Publishing reply {short_id(reply_event.id)}...")

            delivered = yield publish_and_verify(reply_event)
            if delivered:
                log.info(f"  Reply {short_id(reply_event.id)} confirmed on relay")
            else:
                log.error(f"  Reply {short_id(reply_event.id)} could NOT be confirmed on any relay")
        else:
            log.info("  No target event in tags, skipping")

    except Exception as exc:
        log.error(f"  Fact-check failed: {exc}", exc_info=True)
        return


# ============================================================
# STARTUP
# ============================================================

def connect_relays() -> RelayManager:
    """Create a fresh RelayManager with active connections."""
    relay_list = RelayList()
    relay_list.append_url_list(RELAYS)
    relay_list.update_relay_information(timeout=1)
    relay_list.drop_empty_metadata()

    connected_relays = [r.url for r in relay_list.data]
    log.info(f"Connected to {len(connected_relays)} relays: {', '.join(r.replace('wss://', '') for r in connected_relays)}")

    manager = RelayManager(error_threshold=3, timeout=0)
    manager.add_relay_list(
        relay_list,
        close_on_eose=False,
        message_callback=on_message,
        message_callback_url=True,
    )

    filters = FiltersList([
        Filters(
            since=int(datetime.datetime.now().timestamp()),
            kinds=[EventKind.TEXT_NOTE],
        )
    ])

    subscription_id = uuid.uuid4().hex
    manager.add_subscription_on_all_relays(subscription_id, filters)

    return manager


def start():
    global relay_manager

    while True:
        log.info("Connecting to relays...")

        try:
            relay_manager = connect_relays()
        except Exception as e:
            log.error(f"Failed to connect to relays: {e}")
            log.info("Retrying in 30s...")
            import time
            time.sleep(30)
            continue

        # Schedule a disconnect after the reconnect interval
        # This causes run_sync() to return so we can rebuild connections
        relay_manager.io_loop.call_later(
            RELAY_RECONNECT_INTERVAL,
            relay_manager.close_all_relay_connections
        )

        log.info("Bot is now listening for mentions...")

        try:
            relay_manager.run_sync()
        except Exception as e:
            log.warning(f"Relay connection interrupted: {e}")

        log.info(f"Reconnecting to relays (periodic refresh every {RELAY_RECONNECT_INTERVAL // 60}min)...")


if __name__ == "__main__":
    start()
