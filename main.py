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


logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="[%(asctime)s - %(levelname)s] %(message)s"
)
log = logging.getLogger("NostrFactCheckerBot")
log.setLevel(logging.INFO)


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


MUTED_PUBKEYS ={"d52a3260bba32caf47a9f8e09b8be31a790bc00ba9668556f7d319d74dd4206c"}


event_dedup_cache = TTLCache(maxsize=1000, ttl=60)
pending_event_requests: Dict[str, Queue] = {}

last_sent_message_time = datetime.datetime.min

factchecker = FactChecker(
    api_key=MISTRAL_API_KEY,
    agent_id=FACTCHECKER_AGENT_ID
)

relay_manager: RelayManager


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
        return None
    finally:
        pending_event_requests.pop(event_id, None)


def should_handle_event(event: Event) -> bool:
    if event.pubkey in MUTED_PUBKEYS:
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


@gen.coroutine
def on_message(message_json, relay_url):
    global last_sent_message_time

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

    while datetime.datetime.now() - last_sent_message_time < RATE_LIMIT_DELAY:
        yield gen.sleep(0.1)

    last_sent_message_time = datetime.datetime.now()

    etags = event.get_tag_list("e")
    reply_to_ids = [e[0] for e in etags if len(e) >= 3 and e[2] == "reply"]
    if not reply_to_ids:
        reply_to_ids = [e[0] for e in etags if len(e) >= 3 and e[2] == "root"]

    if not reply_to_ids:
        return

    target_event = yield fetch_event_by_id(reply_to_ids[0])
    if not target_event:
        return

    claim_text = target_event.content or ""
    image_urls = extract_image_urls(claim_text)
    for url in image_urls:
        claim_text = claim_text.replace(url, "")

    result = factchecker.check_fact(
        claim_text,
        image_urls=image_urls
    )

    tagger_npub = pubkey_to_npub(event.pubkey)

    reply_event = Event(f"{result}\n\n\nnostr:{tagger_npub}")
    reply_event.tags.append(["e", target_event.id, "", "reply"])
    reply_event.tags.append(["p", event.pubkey, "mention"])
    reply_event.tags.append(["p", target_event.pubkey, "mention"])

    reply_event.sign(str(FACTCHECKER_PRIVATE_KEY))
    relay_manager.publish_event(reply_event)


def start():
    global relay_manager

    relay_list = RelayList()
    relay_list.append_url_list(RELAYS)
    relay_list.update_relay_information(timeout=1)
    relay_list.drop_empty_metadata()

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
        )
    ])

    subscription_id = uuid.uuid4().hex
    relay_manager.add_subscription_on_all_relays(subscription_id, filters)

    relay_manager.run_sync()


if __name__ == "__main__":
    start()
