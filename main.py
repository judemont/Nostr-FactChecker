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


# ============================================================
# LOGGING CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="[%(asctime)s - %(levelname)s] %(message)s"
)
log = logging.getLogger("NostrFactCheckerBot")
log.setLevel(logging.INFO)


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
FETCH_EVENT_TIMEOUT = 2.0


# ============================================================
# RELAYS
# ============================================================

RELAYS = [
    "wss://nos.lol",
    "wss://relay.nostr.bg",
    "wss://nostr.einundzwanzig.space",
    "wss://relay.damus.io",
    "wss://nostr.mom/",
    "wss://nostr-pub.wellorder.net/",
    "wss://relay.nostr.jabber.ch",
    "wss://relay.pleb.to",
    "wss://relay.primal.net",
]


# ============================================================
# GLOBAL STATE
# ============================================================

event_dedup_cache = TTLCache(maxsize=500, ttl=60)
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

def extract_image_urls(content: str) -> List[str]:
    """Extract image URLs from text content."""
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
    """
    Fetch a single Nostr event by ID using existing relay connections.
    """
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
        log.warning(f"Timeout while fetching event {event_id}")
        return None
    finally:
        pending_event_requests.pop(event_id, None)


def should_handle_event(event: Event) -> bool:
    """Determine whether this event is a fact-check request."""
    content = (event.content or "").lower()
    ptags = event.get_tag_list("p")
   # print(ptags)
    mentioned_explicitly = "@factchecker" in content
    
    tagged_directly = any(
        ptag[0] in {FACTCHECKER_NPUB, FACTCHECKER_PUBKEY} and "nostr:" in content
        for ptag in ptags
    )
    
    # Mention detection should be improved 
    #tagged_directly = False # (
    #     tags["p"][0][0] in {FACTCHECKER_NPUB, FACTCHECKER_PUBKEY} and
    #     "nostr:" in content
    # )

    return mentioned_explicitly or tagged_directly


# ============================================================
# CORE MESSAGE HANDLER
# ============================================================

@gen.coroutine
def on_message(message_json, relay_url):
    global last_sent_message_time

    if message_json[0] != RelayMessageType.EVENT:
        return

    event = Event.from_dict(message_json[2])

    # Resolve pending fetch-by-id requests
    if event.id in pending_event_requests:
        pending_event_requests[event.id].put(event)
        return

    # Deduplication
    if event.id in event_dedup_cache:
        return
    event_dedup_cache[event.id] = True

    if not should_handle_event(event):
        return

    log.info(f"Fact-check request from {event.pubkey}")

    # Rate limiting
    while datetime.datetime.now() - last_sent_message_time < RATE_LIMIT_DELAY:
        yield gen.sleep(0.1)

    last_sent_message_time = datetime.datetime.now()

    reply_event: Optional[Event] = None
    etags = event.get_tag_list("e")
   
    reply_to_ids = [etag[0] for etag in etags if len(etag) >= 3 and (etag[2] == "reply") ]
    if len(reply_to_ids) == 0:
        reply_to_ids = [etag[0] for etag in etags if len(etag) >= 3 and (etag[2] == "root") ]

    is_reply = len(reply_to_ids) > 0
    reply_to_id = reply_to_ids[0] if is_reply else None
   
    try:
        if is_reply:
            # Reply to referenced event
            target_event_id = reply_to_id
            target_event = yield fetch_event_by_id(target_event_id)
            print(target_event)
            if not target_event:
                return

            claim_text = target_event.content or ""
            image_urls = extract_image_urls(claim_text)
            for image_url in image_urls:
                claim_text = claim_text.replace(image_url, "")
            factcheck_result = factchecker.check_fact(
                claim_text,
                image_urls=image_urls
            )

            reply_event = Event(f"{factcheck_result}\n\n\nnostr:{event.pubkey}")
            reply_event.tags.append(["e", str(target_event_id), "", "reply"])
            reply_event.tags.append(["p", str(event.pubkey), "mention"])
            reply_event.tags.append(["p", str(target_event.pubkey), "mention"])


            # Send reply
            reply_event.sign(str(FACTCHECKER_PRIVATE_KEY))
            relay_manager.publish_event(reply_event)
            
        # else:
        #     # Direct mention
        #     content = event.content or ""
        #     factcheck_result = factchecker.check_fact(
        #         content,
        #         images_URLs=extract_image_urls(content)
        #     )

        #     reply_event = Event(factcheck_result)
        #     reply_event.tags.append(["e", str(event.id), "", "reply"])

    except Exception as exc:
        log.error(f"Fact-checking failed: {exc}")
        return



    log.info("Fact-check reply sent")


# ============================================================
# STARTUP
# ============================================================

def start():
    global relay_manager

    log.info("Connecting to relays...")

    relay_list = RelayList()
    relay_list.append_url_list(RELAYS)
    relay_list.update_relay_information(timeout=1)
    relay_list.drop_empty_metadata()

    log.info(f"Connected to {len(relay_list.data)} relays")

    relay_manager = RelayManager(error_threshold=3, timeout=0)
    relay_manager.add_relay_list(
        relay_list,
        close_on_eose=False,
        message_callback=on_message,
        message_callback_url=True,
    )

    filters = FiltersList([
        Filters(
            since=int(datetime.datetime.now().timestamp()),
            kinds=[EventKind.TEXT_NOTE],
            pubkey_refs=[FACTCHECKER_PUBKEY,]
        )
    ])

    subscription_id = uuid.uuid4().hex
    relay_manager.add_subscription_on_all_relays(subscription_id, filters)

    relay_manager.run_sync()


if __name__ == "__main__":
    start()
