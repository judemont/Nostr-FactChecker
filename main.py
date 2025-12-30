import logging
import datetime
import uuid
import sys
import os
from cachetools import TTLCache
from tornado import gen
from tornado.queues import Queue

from pynostr.event import EventKind, Event
from pynostr.filters import Filters, FiltersList
from pynostr.message_type import RelayMessageType
from pynostr.relay_list import RelayList
from pynostr.relay_manager import RelayManager

from factchecker import FactChecker


# -------------------- LOGGING --------------------

logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="[%(asctime)s - %(levelname)s] %(message)s"
)
log = logging.getLogger("FactCheckerNostrBot")
log.setLevel(logging.INFO)


# -------------------- CONFIG --------------------

small_cache = TTLCache(maxsize=200, ttl=15)

hex_pk = os.environ["FACTCHECKER_PRIVATE_KEY"]
mistral_api_key = os.environ["MISTRAL_API_KEY"]

if not mistral_api_key:
    raise ValueError("MISTRAL_API_KEY environment variable not set.")

agent_id = "ag_019b704bddcc72079c3a26f9cb4891fa"

factcheckerNprofile = (
    "nprofile1qyxhwumn8ghj7mn0wvhxcmmvqywhwumn8ghj7mn0wd68yttsw43zuam9d3kx7unyv4ezumn9wsqzqsf4rcckdeakmhwufs9dfrnfx50vx3gzwudzzmd7pzafc6puflsu6lvzjz"
)

factchecker = FactChecker(
    api_key=mistral_api_key,
    agent_id=agent_id
)

relays = [
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

last_sent_message_time = datetime.datetime.now()

# Pending fetch-by-id requests
pending_event_requests: dict[str, Queue] = {}


# -------------------- HELPERS --------------------

def get_images_from_content(content: str) -> list[str]:
    images = []
    for word in content.split():
        if word.startswith(("http://", "https://")):
            if word.lower().endswith((
                ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"
            )):
                images.append(word)
    return images


@gen.coroutine
def fetch_event_by_id(event_id: str, timeout=2.0):
    """
    Fetch a single event by ID using the already-connected RelayManager.
    """
    q = Queue()
    pending_event_requests[event_id] = q

    sub_id = uuid.uuid4().hex
    filters = FiltersList([Filters(ids=[event_id])])
    relay_manager.add_subscription_on_all_relays(sub_id, filters)

    try:
        ev = yield gen.with_timeout(
            datetime.timedelta(seconds=timeout),
            q.get()
        )
        return ev
    except TimeoutError:
        return None
    finally:
        pending_event_requests.pop(event_id, None)


# -------------------- MESSAGE CALLBACK --------------------

@gen.coroutine
def check_message(message_json, url):
    global last_sent_message_time

    if message_json[0] != RelayMessageType.EVENT:
        return

    event: Event = Event.from_dict(message_json[2])

    # Resolve fetch-by-id requests FIRST
    if event.id in pending_event_requests:
        pending_event_requests[event.id].put(event)
        return

    # Dedup
    if event.id in small_cache:
        return
    small_cache[event.id] = 1

    content = (event.content or "").strip().lower()

    if factcheckerNprofile not in content and "@factchecker" not in content:
        return

    log.info(f"Factcheck request from {event.pubkey}")

    # Rate limit (≈1 msg/sec)
    while datetime.datetime.now() - last_sent_message_time < datetime.timedelta(milliseconds=1005):
        yield gen.sleep(0.1)
    last_sent_message_time = datetime.datetime.now()

    # -------------------- REPLY LOGIC --------------------

    reply_content = None
    new_event = None

    tags = event.get_tag_dict()

    if "e" in tags:
        # Replying to another event
        claim_event_id = tags["e"][0][0]
        claim_event = yield fetch_event_by_id(claim_event_id)

        if not claim_event:
            log.warning(f"Could not fetch referenced event {claim_event_id}")
            return

        claim_content = claim_event.content or ""
        log.info(f"Checking claim: {claim_content}")

        fact_check = factchecker.check_fact(
            claim_content,
            images_URLs=get_images_from_content(claim_content)
        )

        reply_content = fact_check
        new_event = Event(reply_content)
        new_event.tags.append(["e", claim_event_id, "", "reply"])
        if event.pubkey:
            new_event.add_pubkey_ref(event.pubkey)

    else:
        # Direct mention
        fact_check = factchecker.check_fact(
            event.content or "",
            images_URLs=get_images_from_content(event.content or "")
        )

        reply_content = fact_check
        new_event = Event(reply_content)
        if event.id is not None:
            new_event.tags.append(["e", event.id, "", "reply"])

    # -------------------- SEND --------------------

    new_event.sign(hex_pk)
    relay_manager.publish_event(new_event)

    log.info(f"Sent factcheck reply")


# -------------------- STARTUP --------------------

log.info("Connecting to relays...")

relay_list = RelayList()
relay_list.append_url_list(relays)
relay_list.update_relay_information(timeout=1)
relay_list.drop_empty_metadata()

log.info(f"Using {len(relay_list.data)} relays")

relay_manager = RelayManager(error_threshold=3, timeout=0)
relay_manager.add_relay_list(
    relay_list,
    close_on_eose=False,
    message_callback=check_message,
    message_callback_url=True,
)

filters = FiltersList([
    Filters(
        since=int(datetime.datetime.now().timestamp()),
        kinds=[EventKind.TEXT_NOTE],
    )
])

subscription_id = uuid.uuid1().hex
relay_manager.add_subscription_on_all_relays(subscription_id, filters)

relay_manager.run_sync()
